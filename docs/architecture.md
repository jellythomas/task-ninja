# Architecture & Technical Reference

Detailed technical documentation for Task Ninja internals. For setup and usage, see the [README](../README.md).

---

## Architecture Overview

```
+---------------------------------------------------+
|              Web UI (Kanban Dashboard)             |
|                                                   |
|  +------+------+------+------+------+------+------+  |
|  |Todo  |Queue |Input |Plan  |Dev   |Review|Done  |  |
|  |      |      |      |      |      |      |      |  |
|  |MC-918|MC-917|      |      |MC-917|MC-917|MC-917|  |
|  |MC-918|MC-917|      |      |  4   |  3   |  2   |  |
|  +------+------+------+------+------+------+------+  |
|                                                   |
|  [Max Parallel: 2]  [> Start]  [|| Pause]         |
|                                                   |
|  +--- Live Terminal (tab-switchable) -----------+  |
|  | [MC-9174] [MC-9173]                          |  |
|  | > Running specs: 42/67 passing...            |  |
|  +----------------------------------------------+  |
|                                                   |
|  +--- Live Process Overlay (fullscreen) --------+  |
|  | Clean parsed logs + input bar                 |  |
|  | $ [type to send input to the process]   [Send]|  |
|  +----------------------------------------------+  |
+------------------------+--------------------------+
                         | SSE (real-time)
+------------------------v--------------------------+
|           FastAPI Server + Orchestrator            |
|                                                   |
|  HTTP API:    /api/runs, /api/tickets, /api/stream|
|  Terminal:    /api/tickets/:id/terminal-input      |
|  MCP Tools:   load_epic, start_run, get_status    |
|  Engine:      Worker pool, PTY-backed processes   |
|  Scheduler:   APScheduler (cron/one-time)         |
|  Watchdog:    Auto-retry, stale detect, hours     |
|  State:       SQLite (task_ninja.db)         |
|  Startup:     Python version check + auto-install |
+------------------------+--------------------------+
                         | spawns per ticket (PTY)
+------------------------v--------------------------+
|           AI Agent Workers (git worktrees)        |
|                                                   |
|  Worker 1: worktree-mc-9174/ (interactive mode)   |
|    claude --dangerously-skip-permissions           |
|    > /planning-task MC-9174                        |
|    > /developing-task MC-9174                      |
|    > /open-pr --draft                              |
|                                                   |
|  Worker 2: worktree-mc-9173/                      |
|    gemini -p "implement MC-9173"                  |
+---------------------------------------------------+
```

---

## Execution Flow

### 1. Input Phase

```
User provides input (one of):
  A) Epic Key or Jira URL (e.g., MC-9056 or https://company.atlassian.net/browse/MC-9056)
     -> Jira URLs auto-parsed to clean keys
     -> Fetch all child tickets from Jira via REST API
     -> Display in unified selection modal with summaries
     -> Tickets auto-grouped by [bracket] tags matching registered repos
     -> 3-tier assignment: Global > Prefix Group > Per-ticket override
     -> Selected tickets go to Queued

  B) Multiple Jira Keys / URLs (paste comma-separated)
     -> Fetch ticket details via POST /api/runs/:id/fetch-tickets
     -> Same unified modal with grouping and assignment
     -> Selected tickets go to Queued
```

### 2. Orchestration Phase

```
Orchestrator loop (runs continuously):
  1. Check available worker slots (max_parallel - active_workers)
  2. If slots available:
     a. Pick next ticket from Queued (by rank order)
     b. Resolve project path: ticket repo > run repo > run.project_path
     c. Resolve parent branch: ticket > run > repo default > config default
     d. Sync from origin: git fetch origin, use origin/<branch> as start point
     e. Create git worktree for the ticket
     f. Branch mismatch check: if branch already exists with different parent
        -> Move ticket to Awaiting Input, broadcast SSE event
        -> Dashboard shows modal: Use As-Is / Rebase / Fresh Start
        -> User resolves -> ticket moves back to Queued
     g. Resolve agent profile: ticket > repo default > global default
     g. Spawn AI agent in interactive mode (PTY-backed)
     h. Move ticket to Planning, sync Jira -> In Progress
     i. Write phase commands to PTY sequentially
  3. Monitor active workers:
     a. Stream PTY output -> parse -> logs table + SSE broadcast
     b. Detect phase markers ([PLANNING_COMPLETE], [DEVELOPING_COMPLETE])
     c. On marker: transition to next phase, write next phase commands
     d. On completion: write PR command, move to Review
     e. On failure: mark failed, trigger watchdog (auto-retry if enabled)
     f. User chat: delivered via PTY write_input() at any time
  4. State transition safety:
     a. Moving ticket to non-active state (TODO, QUEUED) kills the worker process
     b. Prevents orphaned processes
  4. Working hours check: only spawn during configured hours/days
  5. Repeat every 5 seconds
```

### 3. Worker Phase (per ticket — Interactive Mode)

```
AI Agent Worker lifecycle (PTY-backed, interactive Claude session):

  0. SPAWN
     - Start: claude --dangerously-skip-permissions (interactive mode)
     - Wait for Claude session to be ready
     - Single session persists across all phases

  1. PLANNING
     - Worker writes configured commands to PTY (e.g., "/planning-task MC-XXXX")
     - Commands execute sequentially within the phase
     - Detect phase completion via marker (e.g., [PLANNING_COMPLETE])
     - Fallback: idle debounce timeout if no marker detected
     - User can chat at any time — messages delivered via PTY
     - Broadcast: state -> planning

  2. DEVELOPING
     - Worker writes developing commands (e.g., "/developing-task MC-XXXX")
     - Create feature branch, implement, test, commit
     - Detect completion via marker (e.g., [DEVELOPING_COMPLETE])
     - User can course-correct, answer questions mid-flight
     - Broadcast: state -> developing

  3. REVIEW (PR CREATION)
     - Worker writes PR command (e.g., "/open-pr --draft")
     - Slash command executes natively in interactive session
     - Detect completion via marker or PR URL detection
     - Broadcast: state -> review

  4. CLEANUP
     - Remove git worktree (keep branch)
     - Update Jira status -> In Review
     - Worker slot freed for next ticket

  Phase Pipeline:
     - Phases and commands are configurable via UI (agent profiles)
     - Each phase has: commands[] + marker (optional)
     - Commands sent sequentially, marker signals phase done
     - If no marker: idle debounce (configurable, default 10s) as fallback
     - User chat resets debounce timer to prevent false triggers
```

### 4. Review Phase (human)

```
Human reviews draft PR:
  - If approved -> merge PR, drag card to Done
  - If changes requested -> drag card back to Developing
    -> Orchestrator spawns new worker to address feedback
```

---

## Terminal & Live Process

Task Ninja has two terminal views:

### Bottom Panel (Inline Log View)
- Shows parsed, timestamped log lines per ticket
- Tab-switchable between active workers
- Fetches history from `/api/logs/:id?tail=500`
- Receives live updates via SSE `log` events
- Read-only, lightweight
- Each tab has a **close (×) button** — can be re-opened from the ticket card's "Terminal" badge
- "Terminal" badge on ticket cards opens/focuses the inline log tab (not the fullscreen overlay)

### Live Process Overlay (Fullscreen xterm)
- Opens from "Live" button on ticket cards
- Full interactive xterm.js terminal connected via WebSocket to the worker's PTY
- Input flow: `WebSocket /ws/terminal/:id` ↔ `worker.write_input()` ↔ `os.write(master_fd)` ↔ process stdin
- Useful when the AI agent needs user confirmation or input

**Desktop features:**
- **Resizable split panes** — drag dividers between terminals (2-pane side-by-side or 2×2 grid, up to 4 terminals)
- **Minimize to pill** — collapse overlay to a floating pill at bottom-right showing terminal count; click to restore with all state preserved
- **Minimize (─) button** in top bar hides overlay without closing terminals; **Close all (✕)** destroys all terminals

**Mobile features:**
- **Dynamic font sizing** — calculates font size to guarantee 80+ columns on any screen width (prevents Claude's spinner animation from wrapping)
- **Zero-padding layout** — removes borders and padding to maximize terminal width
- **Single-column only** — one terminal at a time on mobile

**Ad-hoc terminal sessions:**
- For tickets in **Review/Done/Failed** state with no active worker, clicking "Live" spawns a lightweight `AdHocTerminal` — an interactive Claude session in the worktree without the phase pipeline
- Multiple viewers can attach to the same ad-hoc session
- Ad-hoc terminals are tracked in `orchestrator._adhoc_terminals` and cleaned up when the last viewer detaches and the process exits
- Existing running ad-hoc terminals are reused (no duplicate sessions)

**Smart scroll:**
- Checks viewport position before writing new data
- If user scrolled up to read history, position is preserved (no auto-scroll to bottom)
- If user is already at the bottom, new output scrolls normally

### Auto-Spawn & Auto-Close Behavior

| State Transition | Fullscreen xterm | Inline Log Tab |
|---|---|---|
| → Planning | Auto-spawn | Auto-open + switch |
| → Developing | Keep open (spawn if missing) | Keep tab |
| → Review | Worker exits, Live spawns AdHocTerminal on demand | Keep tab (closeable) |
| → Done | Worker exits, Live spawns AdHocTerminal on demand | Keep tab |
| → Failed | Worker exits, Live spawns AdHocTerminal on demand | Keep tab (read errors) |
| → Awaiting Input | Keep open | Keep tab |

### Review Phase State Protection

If a process exits during the review phase, the ticket **always stays in Review** regardless of exit code. Rationale:
- Planning and developing phases completed successfully — the code is in the branch
- The review phase (`/open-pr`) is best-effort; if it fails, the user can open a PR manually
- Prevents false failures from user closing the terminal, network issues, or Claude exiting cleanly

### Phase Resume on Retry

When a worker fails mid-execution (quota exhaustion, connection lost, crash), the ticket moves to Failed.
On retry, the worker **resumes from the last completed phase** instead of starting from scratch.

- Phase completion is tracked via `last_completed_phase` column on the tickets table
- When a phase's completion marker (e.g., `[PLANNING_COMPLETE]`) is detected, the column is updated
- On retry, phases up to and including `last_completed_phase` are skipped
- Moving a ticket back to **Todo** clears `last_completed_phase` (full restart)
- Moving to **Queued** (retry) preserves it (resume)

| `last_completed_phase` | Retry starts at |
|---|---|
| `NULL` | planning |
| `planning` | developing |
| `developing` | review |

### Hidden File Copying

Git worktrees only contain tracked files. Task Ninja automatically copies hidden files/dirs from the main repo root into each new worktree:

Default list: `.env`, `.claude/`, `.tool-versions`, `.ruby-version`, `.node-version`, `.nvmrc`, `.python-version`

- Copies happen after `git worktree add` (both new and existing branches)
- Existing files in the worktree are not overwritten
- Best-effort — failures don't block worktree creation

### Log Storage & Performance
- **SQLite**: Capped at 500 lines per ticket (auto-trimmed on every insert)
- **Disk**: Raw log file in worktree directory (unbounded, cleaned with worktree)
- **In-memory**: Bottom panel 500 lines/ticket, overlay 2000 lines, worker PTY buffer 256KB
- **Impact**: Minimal — 1 INSERT + 1 DELETE per log line, SQLite handles this efficiently

---

## Ticket Lifecycle

```
Todo --> Queued --> Planning --> Developing --> Review --> Done
           |          ^             ^             |
           v          |             |             |
     Awaiting Input --+             |             |
     (branch mismatch)              |             |
           |                        |             |
           v                        |             |
         Failed ---(auto-retry)-----+             |
           +---- user drags back -----------------+
```

| State | Description | Jira Status | Worker | DB Columns Used |
|-------|-------------|-------------|--------|-----------------|
| Todo | Loaded but not selected for work | (no change) | None | — |
| Queued | Waiting for available worker slot | (no change) | None | — |
| Awaiting Input | Needs user decision (e.g., branch mismatch) | (no change) | None | `input_type`, `input_data` |
| Planning | Worker reading ticket, creating plan | In Progress | Active (PTY) | — |
| Developing | Worker implementing, testing, committing | In Progress | Active (PTY) | — |
| Review | Draft PR opened, awaiting human review | In Review | None | `pr_url`, `pr_number` |
| Done | PR approved/merged | Done | None | — |
| Failed | Worker errored out (watchdog may auto-retry) | (no change) | None | `error` |

---

## Assignment Cascade (3-Tier)

When queuing tickets, assignments resolve in priority order:

```
Per-ticket override  >  Prefix group  >  Global (modal)  >  Repo default
```

- **Global**: Parent branch and agent profile set at the top of the selection modal
- **Prefix group**: Tickets grouped by `[bracket]` tags in their summary (e.g., `[BE]`, `[Flex]`) matching registered repo labels/names. Each group can override branch and profile.
- **Per-ticket**: Individual ticket can override repository, branch, and agent profile

---

## Branch Mismatch Detection

When the orchestrator creates a worktree for a ticket, it checks if the branch already exists locally. If it does, the system verifies that the branch was actually forked from the expected parent branch (the one configured for the ticket).

### Why This Happens

A branch mismatch occurs when:
- A ticket was previously executed with a different parent branch
- A branch was manually created from the wrong base (e.g., from another feature branch instead of the EPIC branch)
- The ticket was deleted and re-added with a different parent branch setting

### Detection

`GitManager.create_worktree()` compares the `merge-base` of the existing branch with `origin/{expected_parent}`. If the merge-base doesn't match the tip of the expected parent, it's a mismatch.

### Resolution Flow

```
Orchestrator detects mismatch
  -> Ticket moves to AWAITING_INPUT (state)
  -> input_type = "branch_mismatch"
  -> input_data = {"current_parent": "feat/MC-9172", "expected_parent": "EPIC-MC-9056"}
  -> SSE broadcast to dashboard
  -> Dashboard auto-opens modal
  -> User picks one of three options:

  1. Use As-Is      — Keep existing branch, ignore mismatch
  2. Rebase          — git rebase --onto origin/{expected_parent} (keeps commits, new base)
  3. Fresh Start     — Delete branch entirely, create new from origin/{expected_parent}

  -> POST /api/tickets/:id/resolve-input {choice: "rebase"}
  -> Orchestrator executes choice
  -> Ticket moves back to QUEUED
  -> Normal execution continues
```

### Database Columns

| Column | Type | Purpose |
|--------|------|---------|
| `input_type` | TEXT | Identifies which modal to show (e.g., `branch_mismatch`) |
| `input_data` | TEXT (JSON) | Context data for the modal (survives page refresh) |

Both columns are cleared (`NULL`) when the input is resolved. This is a generic mechanism — future input types (e.g., `merge_conflict`, `test_failure_retry`) can reuse the same state and columns.

---

## Startup & Dependency Management

On `python3 server.py`, before any imports:

1. **Python version check** — requires 3.10+ (uses PEP 585 generics, PEP 604 unions)
2. **Auto-install dependencies** — checks each package in `requirements.txt` via `importlib.metadata`, installs missing ones automatically (handles Homebrew Python with `--break-system-packages` fallback)
3. **`.env` creation** — generates `.env` with auth token on first run
4. **DB initialization** — creates SQLite tables if not present
5. **State recovery** — restores running state for any active runs

---

## Project Structure

```
task-ninja/
|-- server.py                  # FastAPI app + MCP server entry point
|-- config.yaml                # Default configuration
|-- requirements.txt           # Python dependencies
|-- .env                       # Secrets & feature flags (auto-generated, chmod 600)
|-- README.md                  # User-facing docs
|-- CHANGELOG.md               # Version history
|
|-- engine/
|   |-- __init__.py
|   |-- orchestrator.py        # Worker pool manager, main loop
|   |-- worker.py              # AI agent process spawner (PTY-based)
|   |-- scheduler.py           # APScheduler integration
|   |-- auth.py                # Bearer token middleware + WebSocket auth
|   |-- env_manager.py         # .env file parsing, writing, secret masking
|   |-- notifier.py            # Push notification manager (Web Push + SSE)
|   |-- ticket_watchdog.py     # Event-driven retry, stale detection, working hours
|   |-- jira_client.py         # Direct Jira REST API v3 client
|   |-- claude_helper.py       # Claude CLI helper for Jira fallback
|   |-- mcp_client.py          # MCP client for mcp-atlassian-with-bitbucket
|   |-- git_manager.py         # Git worktree create/cleanup (origin sync)
|   |-- broadcaster.py         # SSE event broadcaster
|   |-- terminal.py            # Terminal/PTY manager
|   `-- state.py               # SQLite state manager
|
|-- models/
|   |-- __init__.py
|   `-- ticket.py              # All Pydantic models (Ticket, Run, Schedule, etc.)
|
|-- static/
|   `-- index.html             # Single-file UI (Tailwind + Alpine + Sortable)
|
`-- docs/
    |-- architecture.md        # This file
    `-- plans/                 # Design documents
```

---

## Database Schema

```sql
-- Runs: a collection of tickets to execute
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    epic_key TEXT,
    max_parallel INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'idle',
    project_path TEXT,
    parent_branch TEXT,
    repository_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tickets: individual work items within a run
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    jira_key TEXT NOT NULL,
    summary TEXT,
    state TEXT NOT NULL DEFAULT 'todo',
    rank INTEGER NOT NULL DEFAULT 0,
    branch_name TEXT,
    worktree_path TEXT,
    pr_url TEXT,
    pr_number INTEGER,
    worker_pid INTEGER,
    paused BOOLEAN DEFAULT FALSE,
    log_file TEXT,
    repository_id INTEGER,
    parent_branch TEXT,
    profile_id INTEGER,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    input_type TEXT,              -- Type of input needed (e.g., 'branch_mismatch')
    input_data TEXT,              -- JSON context for the input modal
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, jira_key)
);

-- Repositories: registered git repositories
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    default_branch TEXT DEFAULT 'main',
    jira_label TEXT,
    default_profile_id INTEGER,
    is_deleted BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Agent Profiles: configurable AI agent commands
CREATE TABLE IF NOT EXISTS agent_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    command TEXT NOT NULL,
    args_template TEXT NOT NULL,
    log_format TEXT DEFAULT 'plain-text',
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Label-Repo Mappings: Jira label to repository mapping
CREATE TABLE IF NOT EXISTS label_repo_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jira_label TEXT NOT NULL,
    repository_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Schedules: timed execution of runs
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    schedule_type TEXT NOT NULL,
    cron_expression TEXT,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    next_run TIMESTAMP,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Logs: append-only terminal output per ticket (auto-trimmed to 500 lines)
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    line TEXT NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tickets_run_id ON tickets(run_id);
CREATE INDEX IF NOT EXISTS idx_tickets_state ON tickets(state);
CREATE INDEX IF NOT EXISTS idx_logs_ticket_id ON logs(ticket_id);
CREATE INDEX IF NOT EXISTS idx_schedules_run_id ON schedules(run_id);
```

---

## API Reference

### Runs

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/runs` | Create a new run `{name, project_path, repository_id, max_parallel}` |
| `GET` | `/api/runs` | List all runs |
| `GET` | `/api/runs/:id` | Get run with all tickets |
| `DELETE` | `/api/runs/:id` | Delete run (kills all workers) |
| `PUT` | `/api/runs/:id/config` | Update `{max_parallel}` |
| `POST` | `/api/runs/:id/start` | Start orchestrator |
| `POST` | `/api/runs/:id/pause` | Pause (active workers continue, no new picks) |
| `POST` | `/api/runs/:id/resume` | Resume orchestrator |

### Tickets

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/runs/:id/load-epic` | Load tickets from Jira epic (supports URLs) |
| `POST` | `/api/runs/:id/fetch-tickets` | Fetch ticket details from Jira `{keys}` |
| `POST` | `/api/runs/:id/add-tickets` | Add tickets with assignments `{keys, assignments}` |
| `PUT` | `/api/tickets/:id/state` | Move ticket `{state: "queued"}` |
| `PUT` | `/api/tickets/:id/rank` | Reorder `{rank: 3}` |
| `PUT` | `/api/tickets/:id/assignment` | Update assignment `{repository_id, parent_branch, profile_id}` |
| `POST` | `/api/tickets/:id/pause` | Pause ticket (kill worker) |
| `POST` | `/api/tickets/:id/resume` | Resume ticket (re-queue for new worker) |
| `POST` | `/api/tickets/:id/resolve-input` | Resolve awaiting input `{choice: "use_as_is"\|"rebase"\|"fresh_start"}` |
| `DELETE` | `/api/tickets/:id` | Remove from board (kill worker if running) |

### Terminal

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/logs/:ticket_id` | Get parsed log lines `?tail=500` |
| `POST` | `/api/tickets/:id/terminal-input` | Send input to worker PTY `{input: "text"}` |
| `POST` | `/api/tickets/:id/open-terminal` | Open external terminal at worktree path |
| `WS` | `/ws/terminal/:ticket_id` | WebSocket for raw PTY stream (requires `websockets`) |

### Schedules

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/schedules` | Create schedule `{run_id, schedule_type, cron_expression}` |
| `GET` | `/api/schedules` | List schedules `?run_id=xxx` |
| `PATCH` | `/api/schedules/:id` | Update `{enabled, cron_expression, end_time}` |
| `DELETE` | `/api/schedules/:id` | Delete schedule |

### Streaming

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/stream/:run_id` | SSE stream for real-time board + log updates |

### Auth & Config

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/auth/login` | Validate token `{token}` |
| `GET` | `/api/auth/status` | Check if auth is required |
| `GET` | `/api/env` | Get `.env` config (secrets masked) |
| `PUT` | `/api/env` | Update `.env` config `{KEY: value}` |
| `POST` | `/api/settings/test-jira` | Test Jira API connection |

### Repositories & Profiles

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/repositories` | List registered repositories |
| `POST` | `/api/repositories` | Add repository `{name, path, default_branch, jira_label}` |
| `PUT` | `/api/repositories/:id` | Update repository |
| `DELETE` | `/api/repositories/:id` | Soft-delete repository |
| `GET` | `/api/profiles` | List agent profiles |
| `POST` | `/api/profiles` | Add profile `{name, command, args_template, log_format}` |
| `PUT` | `/api/profiles/:id` | Update profile |
| `DELETE` | `/api/profiles/:id` | Delete profile |

### Notifications

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/notifications/vapid-key` | Get VAPID public key + enabled status |
| `POST` | `/api/notifications/subscribe` | Store Web Push subscription |
| `DELETE` | `/api/notifications/subscribe` | Remove Web Push subscription |

---

## MCP Integration

The server can also run as an MCP server, exposing tools that Claude Code can call directly:

| Tool | Description |
|------|-------------|
| `load_epic` | Load tickets from a Jira epic into a run |
| `add_tickets` | Add specific ticket keys to a run |
| `start_run` | Start the orchestrator for a run |
| `pause_run` | Pause the orchestrator |
| `get_status` | Get current board state (all tickets + states) |
| `get_ticket_logs` | Get terminal output for a specific ticket |

---

## Configuration Reference

### .env (Secrets & Feature Flags)

Auto-generated on first run. All keys are optional — features are disabled by default.

```env
# Server
TASK_NINJA_SECRET=auto-generated-token-hash
TASK_NINJA_REMOTE_ACCESS=false
TASK_NINJA_HOST=
TASK_NINJA_PORT=

# Jira
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=your-token

# Bitbucket
BITBUCKET_USERNAME=
BITBUCKET_APP_PASSWORD=

# Notifications
NOTIFICATIONS_ENABLED=false
VAPID_PRIVATE_KEY=
VAPID_PUBLIC_KEY=
VAPID_EMAIL=

# Scheduler / Watchdog (all optional, toggle from Settings > Scheduler)
AUTO_RETRY_ENABLED=false
AUTO_RETRY_MAX=3
AUTO_RETRY_DELAY_MINUTES=15
WORKER_TIMEOUT_MINUTES=30
WORKING_HOURS_ENABLED=false
WORKING_HOURS_START=09:00
WORKING_HOURS_END=18:00
WORKING_HOURS_DAYS=mon,tue,wed,thu,fri
```

### config.yaml

```yaml
server:
  host: "127.0.0.1"
  port: 8420

orchestrator:
  max_parallel: 2
  poll_interval: 5
  worker_timeout: 1800

claude:
  command: "claude"
  skip_permissions: true
  idle_timeout: 10  # seconds, debounce fallback when no marker detected

  # Phase pipeline — configurable per phase
  # Each phase: commands[] (sequential) + marker (optional completion signal)
  phases:
    planning:
      commands:
        - "/planning-task {JIRA_KEY}"
      marker: "[PLANNING_COMPLETE]"
    developing:
      commands:
        - "/developing-task {JIRA_KEY}"
      marker: "[DEVELOPING_COMPLETE]"
    review:
      commands:
        - "/open-pr --draft"
      marker: "[PR_COMPLETE]"

  # Legacy single-command mode (used if phases not defined)
  # execute_command: "/execute-jira-task"
  # pr_command: "/open-pr --draft"

mcp:
  atlassian_server: "mcp-atlassian-with-bitbucket"
  jira_status_mapping:
    planning: "In Progress"
    developing: "In Progress"
    review: "In Review"
    done: "Done"

git:
  worktree_dir: ".worktrees"
  branch_prefix: "feat"
  cleanup_worktrees: true

database:
  path: "task_ninja.db"
```

---

## Dependencies

### System Requirements

| Requirement | Version | Purpose |
|-------------|---------|---------|
| Python | >= 3.10 (tested 3.10–3.14) | Server runtime |
| Git | >= 2.20 | Worktree support |
| AI CLI agent | any | Claude Code, Gemini CLI, or custom |

> Dependencies are auto-installed on first run from `requirements.txt`.

### Python Packages

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | >= 0.115 | HTTP API server |
| `uvicorn[standard]` | >= 0.34 | ASGI server (includes `websockets` for WS support) |
| `mcp[cli]` | >= 1.0 | MCP SDK |
| `aiosqlite` | >= 0.20 | Async SQLite |
| `apscheduler` | >= 3.10 | Job scheduling |
| `sse-starlette` | >= 2.0 | Server-Sent Events |
| `pydantic` | >= 2.0 | Data validation |
| `pyyaml` | >= 6.0 | Config parsing |
| `httpx` | >= 0.27 | HTTP client (Jira REST API) |
| `pywebpush` | >= 2.0 | Web Push notifications (optional) |
| `python-dotenv` | >= 1.0 | .env loading |

### Frontend (CDN, no build step)

| Library | Version | Purpose |
|---------|---------|---------|
| Tailwind CSS | 3.x | Styling |
| Alpine.js | 3.x | Reactive UI |
| SortableJS | 1.15 | Drag-and-drop |
