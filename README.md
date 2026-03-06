# Task Ninja

Autonomous Jira ticket execution orchestrator with a visual kanban board. Load tickets from a Jira Epic or paste individual ticket keys, configure parallel workers, and let Claude Code implement them autonomously — complete with Jira status sync, draft PR creation, and live terminal streaming.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Execution Flow](#execution-flow)
- [Ticket Lifecycle](#ticket-lifecycle)
- [Features](#features)
- [Dependencies](#dependencies)
- [Project Structure](#project-structure)
- [Database Schema](#database-schema)
- [API Reference](#api-reference)
- [Setup & Installation](#setup--installation)
- [Configuration](#configuration)
- [Usage Guide](#usage-guide)
- [MCP Integration](#mcp-integration)
- [Troubleshooting](#troubleshooting)

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
|           Claude CLI Workers (git worktrees)      |
|                                                   |
|  Worker 1: worktree-mc-9174/                      |
|    claude --print "/execute-jira-task MC-9174"    |
|                                                   |
|  Worker 2: worktree-mc-9173/                      |
|    claude --print "/execute-jira-task MC-9173"    |
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
     d. Spawn Claude CLI worker in worktree
     e. Move ticket to Planning
     f. Sync Jira status -> In Progress
  3. Monitor active workers:
     a. Stream stdout to logs table + SSE
     b. Detect phase transitions (Planning -> Developing)
     c. On completion: open draft PR, move to Review
     d. On failure: mark as failed, log error
  4. Repeat every 5 seconds
```

### 3. Worker Phase (per ticket)

```
Claude CLI Worker lifecycle:
  1. PLANNING
     - Read Jira ticket description
     - Analyze codebase for affected areas
     - Create implementation plan (docs/plans/mc-XXXX-plan.md)
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
     - Open draft PR on Bitbucket
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
Pending -----> Queued -----> Planning -----> Developing -----> Review -----> Done
   ^                                            ^                |
   |                                            |                |
   +---- user drags back -----------------------+---- feedback --+
```

| State | Description | Jira Status | Draggable | Worker |
|-------|-------------|-------------|-----------|--------|
| Pending | Loaded but not selected for work | (no change) | Yes | None |
| Queued | Waiting for available worker slot | (no change) | Yes | None |
| Planning | Worker reading ticket, creating plan | In Progress | Pause first | Active |
| Developing | Worker implementing, testing, committing | In Progress | Pause first | Active |
| Review | Draft PR opened, awaiting human review | In Review | Yes | None |
| Done | PR approved/merged | Done | Yes | None |

### Interactive Controls

- **Pause** (on Planning/Developing): Kills the Claude CLI process. Card becomes draggable.
- **Resume** (on paused cards): Spawns a fresh Claude session to continue.
- **Delete**: Removes ticket from board. Kills worker if running. Does not change Jira status.
- **Drag-and-drop**: Move cards between any columns. Running cards must be paused first.

---

## Features

### Core
- Load tickets from Jira Epic (with checkbox selection) or paste multiple ticket keys
- Visual kanban board with 7 columns and drag-and-drop
- Configurable parallel workers (1-4 concurrent)
- Per-ticket pause/resume/delete controls
- Live terminal output with tab switching between active workers
- Jira status sync (bidirectional)
- Draft PR creation on Bitbucket via existing `/open-pr` command
- Git worktree isolation per ticket (clean context, no conflicts)

### UI
- Quick Setup Wizard (3-step: Jira, Repository, Agent Profile) — re-openable from header
- Settings drawer with sidebar tabs (General, Repos, Profiles, Jira, Notifications, Remote Access)
- Credential fields with eye-toggle mask/unmask
- Dynamic Jira links using configured base URL
- Auto-scroll terminal logs to latest output

### Security & Remote Access
- Bearer token authentication (auto-generated secret in `.env`)
- Login screen for remote access from phone/tablet
- Secrets stored in `.env` file (chmod 600) instead of database
- Credential mask/unmask toggle on all sensitive fields
- Remote access toggle — bind to `0.0.0.0` when enabled, `127.0.0.1` otherwise
- Step-by-step setup guides for Tailscale, ngrok, and Cloudflare Tunnel in Settings UI
- Full Tailscale walkthrough in README (computer + phone setup)

### Ticket Watchdog (Event-Driven)
- Auto-retry failed tickets (configurable max retries and delay)
- Stale ticket detection — kills workers exceeding timeout
- Working hours enforcement — only spawn workers within configured window
- Zero overhead — per-ticket `asyncio.call_later` timers, no polling

### Push Notifications
- Browser Notification API — in-tab alerts for ticket completion/failure
- Web Push (VAPID) — notifications even when the tab is closed
- Configurable via `.env` and Settings drawer

### Multi-Repository & Agent Profiles
- Register multiple repositories with path, default branch, and Jira key prefix
- Auto-match tickets to repositories by Jira key prefix (e.g., `MC-1234` → repo with label `MC`)
- Configurable agent profiles (Claude Code, Gemini CLI, or custom)
- Per-ticket overrides for repository, parent branch, and agent profile

### Scheduler
- One-time scheduled runs (start at specific datetime)
- Recurring schedules (weekdays, daily, custom cron)
- Optional end time (auto-pause workers when window closes)
- Multiple schedules per run

### Smart Blast Radius Testing
- Analyzes what was changed (model constant? shared concern? factory?)
- Expands test scope when shared code is modified
- Runs related specs first, then broader scope if shared changes detected
- Prevents the "distant spec failure" problem

### Dependency Resolution
- Reads Jira "blocks/blocked by" issue links
- Respects Jira rank order as default execution sequence
- Skips blocked tickets, picks next available
- Manual reordering via drag-and-drop in Queued column

---

## Dependencies

### System Requirements

| Requirement | Version | Purpose |
|-------------|---------|---------|
| Python | >= 3.11 | Server runtime |
| Claude CLI | latest | `claude` command for worker sessions |
| Git | >= 2.20 | Worktree support |
| Node.js | >= 18 | For Claude CLI (if installed via npm) |

### Python Packages

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | >= 0.115 | HTTP API server |
| `uvicorn[standard]` | >= 0.34 | ASGI server for FastAPI |
| `mcp[cli]` | >= 1.0 | MCP SDK — used as both server (exposes tools) and client (calls mcp-atlassian-with-bitbucket) |
| `aiosqlite` | >= 0.20 | Async SQLite access |
| `apscheduler` | >= 3.10 | Job scheduling (cron, one-time) |
| `sse-starlette` | >= 2.0 | Server-Sent Events for FastAPI |
| `pydantic` | >= 2.0 | Data validation (comes with FastAPI) |
| `pyyaml` | >= 6.0 | Config file parsing |
| `httpx` | >= 0.27 | Direct Jira API calls |
| `pywebpush` | >= 2.0 | Web Push notifications (optional) |
| `python-dotenv` | >= 1.0 | `.env` file loading |

### MCP Servers (Required)

This project depends entirely on `mcp-atlassian-with-bitbucket` for all Jira and Bitbucket operations. Both the orchestrator and Claude workers use MCP tools — no direct REST API calls.

| MCP Server | Required | Used By | Purpose |
|------------|----------|---------|---------|
| `mcp-atlassian-with-bitbucket` | **Yes** | Orchestrator + Workers | Load epics (`jira_search`), read tickets (`jira_get_issue`), transition statuses (`jira_transition_issue`), create PRs (`bitbucket_create_pull_request`), read dependencies (`jira_get_issue` with links) |
| `gchat-mcp` | Optional | Workers | Google Chat notifications for draft PR reviews |

**How the orchestrator calls MCP tools:**

The FastAPI server communicates with `mcp-atlassian-with-bitbucket` as an MCP client, calling tools like:
- `jira_search` — load tickets from epic (`"Epic Link" = MC-9056`)
- `jira_get_issue` — read ticket details and issue links for dependencies
- `jira_get_transitions` — get available status transitions
- `jira_transition_issue` — move tickets through lifecycle states
- `bitbucket_create_pull_request` — open draft PRs

**Claude CLI workers** inherit MCP server config from `~/.claude/settings.json` and use the same tools during `/execute-jira-task`.

### Frontend (CDN, no build step)

| Library | Version | Purpose |
|---------|---------|---------|
| Tailwind CSS | 3.x | Utility-first styling |
| Alpine.js | 3.x | Reactive UI without build step |
| SortableJS | 1.15 | Drag-and-drop between columns |
| xterm.js | 5.x | Terminal emulator for live logs |

---

## Project Structure

```
task-ninja/
|-- server.py                  # FastAPI app + MCP server entry point
|-- config.yaml                # Default configuration
|-- requirements.txt           # Python dependencies
|-- .env                       # Secrets & feature flags (auto-generated, chmod 600)
|-- README.md                  # This file
|-- CHANGELOG.md               # Version history
|
|-- engine/
|   |-- __init__.py
|   |-- orchestrator.py        # Worker pool manager, main loop
|   |-- worker.py              # Claude CLI process spawner
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
`-- tests/
    |-- test_orchestrator.py
    |-- test_worker.py
    |-- test_state.py
    `-- test_api.py
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
    state TEXT NOT NULL DEFAULT 'pending',  -- pending | queued | planning | developing | review | done | failed
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
| `GET` | `/api/logs/:ticket_id` | Get terminal logs `?tail=100&follow=true` |
| `WS` | `/ws/terminal/:ticket_id` | WebSocket for live interactive terminal |

### Static

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Serve the kanban UI (index.html) |

---

## Setup & Installation

### Step 1: Clone / Create the project

```bash
cd ~/mcp-servers
mkdir task-ninja && cd task-ninja
```

### Step 2: Set up Python environment

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 3: Start the server

```bash
python server.py
```

On first run, the server will:
- Auto-create `.env` with a generated `TASK_NINJA_SECRET` (chmod 600)
- Auto-initialize the SQLite database
- Open the Quick Setup Wizard in the UI

### Step 4: Open the dashboard & complete Quick Setup

```
http://localhost:8420
```

The Quick Setup Wizard guides you through 3 steps:
1. **Jira Connection** — Base URL, email, API token ([generate here](https://id.atlassian.com/manage-profile/security/api-tokens))
2. **Repository** — Register your project repo path and Jira key prefix
3. **Agent Profile** — Configure which AI agent to use (Claude Code, Gemini CLI, etc.)

### Step 5 (Optional): Enable remote access

Access Task Ninja from your phone or another device — even when it's running on your local machine.

#### 5a. Enable remote access in Task Ninja

**Via UI:** Open Settings (gear icon) > **Remote Access** tab > toggle **Enable Remote Access**

**Via .env:**
```env
TASK_NINJA_REMOTE_ACCESS=true
```

Restart the server. It will now:
- Bind to `0.0.0.0` (all interfaces) instead of `127.0.0.1`
- Require the auth token (`TASK_NINJA_SECRET`) for all requests
- Print the token prefix on startup for reference

#### 5b. Choose a tunneling method

Your local machine isn't directly reachable from your phone over the internet. You need a tunnel.

---

**Option A: Tailscale (recommended — free, no domain needed, encrypted)**

Tailscale creates a private network (VPN mesh) between your devices. Your phone connects directly to your computer's Tailscale IP — no public exposure, no domain required.

**On your computer (macOS):**

```bash
# Install via Homebrew
brew install tailscale

# Start the Tailscale daemon
sudo tailscaled &

# Authenticate (opens browser)
tailscale up

# Get your Tailscale IP (e.g., 100.64.x.x)
tailscale ip -4
```

> On macOS you can also install the [Tailscale app](https://tailscale.com/download/mac) from the Mac App Store — it runs in the menu bar and handles the daemon automatically.

**On your phone (iOS / Android):**

1. Install **Tailscale** from the [App Store](https://apps.apple.com/app/tailscale/id1470499037) or [Google Play](https://play.google.com/store/apps/details?id=com.tailscale.ipn)
2. Open the app and sign in with the **same account** you used on your computer
3. Toggle the VPN switch **ON**
4. Your phone is now on the same Tailscale network as your computer

**Connect:**

1. On your phone's browser, go to:
   ```
   http://100.64.x.x:8420
   ```
   (Replace `100.64.x.x` with the Tailscale IP from `tailscale ip -4`)
2. Enter your auth token (`TASK_NINJA_SECRET` from `.env`)
3. You're in — full kanban board on your phone

**Verify the connection:**
```bash
# From your computer, check both devices are connected
tailscale status
```

You should see both your computer and phone listed with green indicators.

> **Tip:** Tailscale IPs are stable — bookmark `http://100.64.x.x:8420` on your phone for quick access.

---

**Option B: ngrok (no install on phone, public URL)**

ngrok creates a temporary public URL that tunnels to your local server. No need to install anything on your phone — just open the URL in any browser.

```bash
# Install
brew install ngrok

# Authenticate (free account at ngrok.com)
ngrok config add-authtoken YOUR_TOKEN

# Start tunnel
ngrok http 8420
```

ngrok will display a URL like `https://abc123.ngrok-free.app`. Open it on your phone and enter your auth token.

> **Note:** Free tier URLs change on every restart. Paid plans get stable subdomains.

---

**Option C: Cloudflare Tunnel (requires domain)**

If you own a domain and use Cloudflare DNS:

```bash
# Install
brew install cloudflared

# Create tunnel (one-time)
cloudflared tunnel create task-ninja
cloudflared tunnel route dns task-ninja ninja.yourdomain.com

# Run tunnel
cloudflared tunnel --url http://localhost:8420 run task-ninja
```

Access via `https://ninja.yourdomain.com` from any device.

---

#### 5c. Login from your phone

1. Open the Task Ninja URL in your phone's browser
2. The login screen will appear asking for your access token
3. Find your token: check `.env` file for `TASK_NINJA_SECRET`, or in the UI under Settings > Remote Access (click the eye icon to reveal)
4. Paste the token and tap **Sign In**
5. The token is saved in your browser — you won't need to enter it again unless you clear browser data

### Step 6 (Optional): Enable push notifications

For browser notifications when tickets complete or fail:

1. In the Settings drawer, go to **Notifications** tab
2. Click **Enable** to grant browser notification permission
3. Toggle **Server Notifications** on

For notifications even when the tab is closed (Web Push):

1. Generate VAPID keys: `python -c "from pywebpush import webpush; from py_vapid import Vapid; v = Vapid(); v.generate_keys(); print('Private:', v.private_pem()); print('Public:', v.public_key)"`
2. Add to `.env`:
   ```
   VAPID_PRIVATE_KEY=your-private-key
   VAPID_PUBLIC_KEY=your-public-key
   VAPID_EMAIL=your-email@example.com
   NOTIFICATIONS_ENABLED=true
   ```
3. In Settings > Notifications, click **Subscribe to Push**

### Step 7 (Optional): Configure auto-retry & working hours

Edit `.env` to enable:

```env
# Auto-retry failed tickets (e.g., token exhaustion)
AUTO_RETRY_ENABLED=true
AUTO_RETRY_MAX=3
AUTO_RETRY_DELAY_MINUTES=15

# Only spawn workers during business hours
WORKING_HOURS_ENABLED=true
WORKING_HOURS_START=09:00
WORKING_HOURS_END=18:00
WORKING_HOURS_DAYS=mon,tue,wed,thu,fri
```

### Step 8 (Optional): Register as MCP server in Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "task-ninja": {
      "command": "python",
      "args": ["/Users/you/mcp-servers/task-ninja/server.py", "--mcp"]
    }
  }
}
```

Jira credentials are now read from `.env`, not MCP server env vars.

---

## Configuration

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
  max_parallel: 2                        # default concurrent workers
  poll_interval: 5                       # seconds between orchestrator checks
  worker_timeout: 1800                   # 30 min max per ticket (0 = unlimited)

claude:
  command: "claude"                      # path to claude CLI
  flags: ["--print"]                     # headless mode flags
  skip_permissions: true                 # --dangerously-skip-permissions (toggle in UI settings)
  execute_command: "/execute-jira-task"  # command to run per ticket
  pr_command: "/open-pr --draft"         # command for draft PR

mcp:
  atlassian_server: "mcp-atlassian-with-bitbucket"  # MCP server name
  jira_status_mapping:                   # board state -> Jira transition
    planning: "In Progress"
    developing: "In Progress"
    review: "In Review"
    done: "Done"

git:
  worktree_dir: ".worktrees"             # relative to project root
  branch_prefix: "feat"                  # feat/MC-XXXX
  cleanup_worktrees: true                # remove worktrees after PR

database:
  path: "autonomous_task.db"
```

---

## Usage Guide

### Workflow 1: Execute an entire Epic

1. Open `http://localhost:8420`
2. Enter Epic key: `MC-9056`
3. Click "Load Epic" — all child tickets appear with checkboxes
4. Check the [BE] tickets you want to work on
5. Click "Queue Selected" — tickets move to Queued column
6. Set max parallel workers (e.g., 2)
7. Click "Start" — orchestrator begins picking tickets

### Workflow 2: Execute specific tickets

1. Open `http://localhost:8420`
2. Switch to "Paste Tickets" tab
3. Enter ticket keys (one per line):
   ```
   MC-9173
   MC-9174
   MC-9177
   ```
4. Click "Queue All" — tickets go directly to Queued
5. Click "Start"

### Workflow 3: Scheduled execution

1. Load tickets via Workflow 1 or 2
2. Open Scheduler panel
3. Set: Start at 09:00, End at 18:00, Repeat: Weekdays
4. Save schedule — orchestrator will auto-start/stop daily

### Managing active work

- **Pause a ticket**: Click pause button on the card. Worker is killed. Card becomes draggable.
- **Resume a ticket**: Click play button. A fresh Claude session spawns.
- **Reorder**: Drag cards within the Queued column to change priority.
- **Move back**: Drag a Review card back to Queued to re-implement with PR feedback.
- **Delete**: Click delete on any card. Confirms, then removes from board.
- **Switch terminal**: Click ticket tabs in the Live Terminal panel to view different worker outputs.

### Settings (gear icon in config bar)

| Setting | Default | Description |
|---------|---------|-------------|
| Max Parallel | 2 | Concurrent Claude workers (1-4) |
| Skip Permissions | ON | Adds `--dangerously-skip-permissions` to Claude CLI. Turn OFF if you want manual approval per tool call (slower but safer). |
| Worker Timeout | 30 min | Max time per ticket before auto-kill (0 = unlimited) |
| Cleanup Worktrees | ON | Remove git worktrees after PR creation |

---

## MCP Integration

The server can also run as an MCP server, exposing tools that Claude Code can call directly:

### MCP Tools

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

## Troubleshooting

### Common Issues

**"claude: command not found"**
- Ensure Claude CLI is installed: `npm install -g @anthropic-ai/claude-code`
- Or add to PATH: `export PATH="$PATH:$(npm bin -g)"`

**"Git worktree creation failed"**
- Ensure you're in a git repository
- Check for existing worktrees: `git worktree list`
- Clean stale worktrees: `git worktree prune`

**"Jira API 401 Unauthorized"**
- Verify JIRA_API_TOKEN is valid
- Generate new token: https://id.atlassian.com/manage-profile/security/api-tokens

**Worker stuck in Planning/Developing**
- Check live terminal for errors
- Pause and resume the ticket (spawns fresh session)
- If persistent, delete ticket and re-queue

**Port 8420 already in use**
- Change port in config.yaml or: `python server.py --port 8421`

---

## License

Internal tool — Mekari engineering use only.
