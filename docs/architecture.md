# Architecture & Technical Reference

Detailed technical documentation for Task Ninja internals. For setup and usage, see the [README](../README.md).

---

## Architecture Overview

```
+---------------------------------------------------+
|              Web UI (Kanban Dashboard)             |
|                                                   |
|  +--------+--------+------+------+------+------+  |
|  |Pending |Queued  |Plan  |Dev   |Review|Done  |  |
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
+------------------------+--------------------------+
                         | SSE (real-time)
+------------------------v--------------------------+
|           FastAPI Server + Orchestrator            |
|                                                   |
|  HTTP API:    /api/runs, /api/tickets, /api/stream|
|  MCP Tools:   load_epic, start_run, get_status    |
|  Engine:      Worker pool, dependency resolver     |
|  Scheduler:   APScheduler (cron/one-time)         |
|  State:       SQLite (autonomous_task.db)         |
+------------------------+--------------------------+
                         | spawns per ticket
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
  A) Epic Key (e.g., MC-9056)
     -> Fetch all child tickets from Jira
     -> Display in UI with checkboxes
     -> User selects which tickets to work on
     -> Selected tickets go to Queued

  B) Multiple Jira Keys (e.g., MC-9173, MC-9174, MC-9177)
     -> Validate keys exist in Jira
     -> All tickets go directly to Queued
```

### 2. Orchestration Phase

```
Orchestrator loop (runs continuously):
  1. Check available worker slots (max_parallel - active_workers)
  2. If slots available:
     a. Pick next ticket from Queued (by rank order)
     b. Check dependency graph — skip if blocked
     c. Create git worktree for the ticket
     d. Spawn AI agent worker in worktree
     e. Move ticket to Planning
     f. Sync Jira status -> In Progress
  3. Monitor active workers:
     a. Stream stdout to logs table + SSE
     b. Detect phase transitions (Planning -> Developing)
     c. On completion: open draft PR, move to Review
     d. On failure: mark as failed, trigger watchdog
  4. Repeat every 5 seconds
```

### 3. Worker Phase (per ticket)

```
AI Agent Worker lifecycle:
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
     - Notify Google Chat (optional)
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

## Ticket Lifecycle

```
Todo -----> Queued -----> Planning -----> Developing -----> Review -----> Done
  ^                                          ^                |
  |                                          |                |
  +---- user drags back --------------------+---- feedback --+
```

| State | Description | Jira Status | Draggable | Worker |
|-------|-------------|-------------|-----------|--------|
| Todo | Loaded but not selected for work | (no change) | Yes | None |
| Queued | Waiting for available worker slot | (no change) | Yes | None |
| Planning | Worker reading ticket, creating plan | In Progress | Pause first | Active |
| Developing | Worker implementing, testing, committing | In Progress | Pause first | Active |
| Review | Draft PR opened, awaiting human review | In Review | Yes | None |
| Done | PR approved/merged | Done | Yes | None |
| Failed | Worker errored out | (no change) | Yes | None |

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
|   |-- jira_client.py         # Direct Jira REST API client
|   |-- claude_helper.py       # Claude CLI helper for Jira fallback
|   |-- mcp_client.py          # MCP client for mcp-atlassian-with-bitbucket
|   |-- git_manager.py         # Git worktree create/cleanup
|   |-- broadcaster.py         # SSE event broadcaster
|   |-- terminal.py            # Terminal/PTY manager
|   `-- state.py               # SQLite state manager
|
|-- models/
|   |-- __init__.py
|   `-- ticket.py              # All Pydantic models (Ticket, Run, Schedule, etc.)
|
|-- static/
|   `-- index.html             # Single-file UI (Tailwind + Alpine + Sortable + xterm)
|
`-- docs/
    `-- architecture.md        # This file
```

---

## Database Schema

```sql
-- Runs: a collection of tickets to execute (from an epic or manual input)
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    epic_key TEXT,
    max_parallel INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'idle',  -- idle | running | paused | completed
    project_path TEXT,                    -- absolute path to the git repo
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tickets: individual work items within a run
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    jira_key TEXT NOT NULL,
    summary TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    rank INTEGER NOT NULL DEFAULT 0,
    branch_name TEXT,
    worktree_path TEXT,
    pr_url TEXT,
    pr_number INTEGER,
    worker_pid INTEGER,
    paused BOOLEAN DEFAULT FALSE,
    log_file TEXT,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, jira_key)
);

-- Schedules: timed execution of runs
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    schedule_type TEXT NOT NULL,            -- one-time | recurring
    cron_expression TEXT,                   -- e.g., "0 9 * * 1-5" (weekdays 9am)
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    next_run TIMESTAMP,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Logs: append-only terminal output per ticket
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
| `POST` | `/api/runs` | Create a new run `{name, project_path, max_parallel}` |
| `GET` | `/api/runs` | List all runs |
| `GET` | `/api/runs/:id` | Get run with all tickets |
| `DELETE` | `/api/runs/:id` | Delete run (kills all workers) |
| `PUT` | `/api/runs/:id/config` | Update `{max_parallel}` |
| `POST` | `/api/runs/:id/start` | Start orchestrator |
| `POST` | `/api/runs/:id/pause` | Pause (finish current, stop picking new) |
| `POST` | `/api/runs/:id/resume` | Resume orchestrator |

### Tickets

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/runs/:id/load-epic` | Load tickets from Jira epic `{epic_key}` |
| `POST` | `/api/runs/:id/add-tickets` | Add tickets by keys `{keys: ["MC-9173", ...]}` |
| `PUT` | `/api/tickets/:id/state` | Move ticket `{state: "queued"}` |
| `PUT` | `/api/tickets/:id/rank` | Reorder `{rank: 3}` |
| `POST` | `/api/tickets/:id/pause` | Pause ticket (kill worker) |
| `POST` | `/api/tickets/:id/resume` | Resume ticket (new worker) |
| `DELETE` | `/api/tickets/:id` | Remove from board (kill worker if running) |

### Schedules

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/schedules` | Create schedule `{run_id, type, cron, start_time, end_time}` |
| `GET` | `/api/schedules` | List all schedules |
| `DELETE` | `/api/schedules/:id` | Delete schedule |

### Auth & Config

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/auth/login` | Validate token `{token}` |
| `GET` | `/api/auth/status` | Check if auth is required |
| `GET` | `/api/env` | Get `.env` config (secrets masked) |
| `PUT` | `/api/env` | Update `.env` config `{KEY: value}` |

### Repositories & Profiles

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/repositories` | List registered repositories |
| `POST` | `/api/repositories` | Add repository `{name, path, default_branch, jira_label}` |
| `PUT` | `/api/repositories/:id` | Update repository |
| `DELETE` | `/api/repositories/:id` | Delete repository |
| `GET` | `/api/profiles` | List agent profiles |
| `POST` | `/api/profiles` | Add profile `{name, command, args_template}` |
| `PUT` | `/api/profiles/:id` | Update profile |
| `DELETE` | `/api/profiles/:id` | Delete profile |

### Notifications & Watchdog

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/notifications/vapid-key` | Get VAPID public key + enabled status |
| `POST` | `/api/notifications/subscribe` | Store Web Push subscription |
| `DELETE` | `/api/notifications/subscribe` | Remove Web Push subscription |
| `GET` | `/api/watchdog/status` | Get watchdog status (timers, retries, working hours) |

### Streaming

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/stream/:run_id` | SSE stream for real-time board updates |
| `GET` | `/api/logs/:ticket_id` | Get terminal logs `?tail=500` |
| `WS` | `/ws/terminal/:ticket_id` | WebSocket for live interactive terminal |

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

This allows Claude Code to orchestrate ticket execution conversationally:

```
User: "Load the PDAM epic and start working on all BE tickets"
Claude: [calls load_epic] -> [calls start_run]
        "Started 2 parallel workers on MC-9173 and MC-9174.
         6 more tickets queued. Dashboard: http://localhost:8420"
```

---

## Configuration Reference

### .env (Secrets & Feature Flags)

Auto-generated on first run. All keys are optional — features are disabled by default.

```env
# Server
TASK_NINJA_SECRET=auto-generated-token
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

# Scheduler / Watchdog
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
  path: "autonomous_task.db"
```

---

## Dependencies

### System Requirements

| Requirement | Version | Purpose |
|-------------|---------|---------|
| Python | >= 3.11 | Server runtime |
| Git | >= 2.20 | Worktree support |
| AI CLI agent | any | Claude Code, Gemini CLI, or custom |

### Python Packages

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | >= 0.115 | HTTP API server |
| `uvicorn[standard]` | >= 0.34 | ASGI server |
| `mcp[cli]` | >= 1.0 | MCP SDK |
| `aiosqlite` | >= 0.20 | Async SQLite |
| `apscheduler` | >= 3.10 | Job scheduling |
| `sse-starlette` | >= 2.0 | Server-Sent Events |
| `pydantic` | >= 2.0 | Data validation |
| `pyyaml` | >= 6.0 | Config parsing |
| `httpx` | >= 0.27 | HTTP client |
| `pywebpush` | >= 2.0 | Web Push (optional) |
| `python-dotenv` | >= 1.0 | .env loading |

### Frontend (CDN, no build step)

| Library | Version | Purpose |
|---------|---------|---------|
| Tailwind CSS | 3.x | Styling |
| Alpine.js | 3.x | Reactive UI |
| SortableJS | 1.15 | Drag-and-drop |
| xterm.js | 5.x | Terminal emulator |
