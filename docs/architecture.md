# Architecture & Technical Reference

Detailed technical documentation for Task Ninja internals. For setup and usage, see the [README](../README.md).

---

## Architecture Overview

```
+---------------------------------------------------+
|              Web UI (Kanban Dashboard)             |
|                                                   |
|  +--------+--------+------+------+------+------+  |
|  |Todo    |Queued  |Plan  |Dev   |Review|Done  |  |
|  |        |        |      |      |      |      |  |
|  |MC-9180 |MC-9177 |      |MC-917|MC-917|MC-917|  |
|  |MC-9181 |MC-9178 |      |  4   |  3   |  2   |  |
|  +--------+--------+------+------+------+------+  |
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
|  Worker 1: worktree-mc-9174/                      |
|    claude --print "/execute-jira-task MC-9174"    |
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
     f. Resolve agent profile: ticket > repo default > global default
     g. Spawn AI agent worker in worktree (PTY-backed)
     h. Move ticket to Planning, sync Jira -> In Progress
  3. Monitor active workers:
     a. Stream PTY output -> parse -> logs table + SSE broadcast
     b. Detect phase transitions (Planning -> Developing)
     c. On completion: open draft PR, move to Review
     d. On failure: mark failed, trigger watchdog (auto-retry if enabled)
  4. Working hours check: only spawn during configured hours/days
  5. Repeat every 5 seconds
```

### 3. Worker Phase (per ticket)

```
AI Agent Worker lifecycle (PTY-backed process):
  1. PLANNING
     - Read Jira ticket description
     - Analyze codebase for affected areas
     - Create implementation plan
     - Broadcast: state -> planning

  2. DEVELOPING
     - Create feature branch (feat/MC-XXXX)
     - Implement code changes
     - Run smart blast radius tests
     - Fix any test failures
     - Commit changes
     - Broadcast: state -> developing

  3. PR CREATION
     - Push branch to remote
     - Open draft PR on Bitbucket/GitHub
     - Broadcast: state -> review

  4. CLEANUP
     - Remove git worktree (keep branch)
     - Update Jira status -> In Review
     - Worker slot freed for next ticket
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

### Bottom Panel (Log View)
- Shows parsed, timestamped log lines per ticket
- Tab-switchable between active workers
- Fetches history from `/api/logs/:id?tail=500`
- Receives live updates via SSE `log` events
- Read-only, lightweight

### Live Process Overlay (Fullscreen)
- Opens from "Live Terminal" button on active ticket cards
- Shows same clean parsed log output as bottom panel
- Includes **input bar** at the bottom for sending text to the worker's PTY
- Input flow: `POST /api/tickets/:id/terminal-input` -> `worker.write_input()` -> `os.write(master_fd)` -> process stdin
- Useful when the AI agent needs user confirmation or input

### Log Storage & Performance
- **SQLite**: Capped at 500 lines per ticket (auto-trimmed on every insert)
- **Disk**: Raw log file in worktree directory (unbounded, cleaned with worktree)
- **In-memory**: Bottom panel 500 lines/ticket, overlay 2000 lines, worker PTY buffer 256KB
- **Impact**: Minimal — 1 INSERT + 1 DELETE per log line, SQLite handles this efficiently

---

## Ticket Lifecycle

```
Todo -----> Queued -----> Planning -----> Developing -----> Review -----> Done
  ^           |                              ^                |
  |           v                              |                |
  |         Failed ---(auto-retry)---------->+                |
  +---- user drags back --------------------+---- feedback --+
```

| State | Description | Jira Status | Worker |
|-------|-------------|-------------|--------|
| Todo | Loaded but not selected for work | (no change) | None |
| Queued | Waiting for available worker slot | (no change) | None |
| Planning | Worker reading ticket, creating plan | In Progress | Active (PTY) |
| Developing | Worker implementing, testing, committing | In Progress | Active (PTY) |
| Review | Draft PR opened, awaiting human review | In Review | None |
| Done | PR approved/merged | Done | None |
| Failed | Worker errored out (watchdog may auto-retry) | (no change) | None |

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
  flags: ["--print"]
  skip_permissions: true
  execute_command: "/execute-jira-task"
  pr_command: "/open-pr --draft"

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
