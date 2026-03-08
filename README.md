# Task Ninja

An AI-powered ticket execution engine with a visual kanban board. Point it at a Jira Epic, pick your tickets, hit Start — and watch AI agents implement them in parallel with live terminal streaming, automatic PR creation, and Jira status sync.

> **Not another kanban board.** Task Ninja doesn't just track tickets — it *executes* them. Each ticket gets its own git worktree, its own AI agent worker, and its own live terminal. You supervise from the board while AI does the coding.

## What makes Task Ninja different

| | Vibe Kanban / Trello / Linear | Task Ninja |
|---|---|---|
| **Tickets** | You track them manually | AI agents execute them autonomously |
| **Parallelism** | One task at a time | Multiple AI workers in parallel (configurable) |
| **Git isolation** | Manual branch management | Auto-creates git worktrees per ticket |
| **PR creation** | You open PRs yourself | Auto-opens draft PRs on completion |
| **Jira sync** | Copy-paste status updates | Bidirectional — board state syncs to Jira |
| **Terminal** | Not applicable | Live terminal streaming per worker |
| **Agent flexibility** | Locked to one tool | Pluggable — Claude Code, Gemini CLI, or custom |
| **Retry on failure** | Manual re-run | Auto-retry with configurable delay and max attempts |
| **Mobile access** | Cloud-hosted only | Run locally, access from phone via Tailscale/ngrok |

## Features

- **Parallel AI execution** — run multiple AI agents simultaneously, each in isolated git worktrees
- **Any AI agent** — Claude Code, Gemini CLI, or any CLI tool via configurable agent profiles
- **Live terminal** — watch each worker's output in real-time, tab-switch between active workers
- **Live Process overlay** — fullscreen terminal view with input bar to interact with the AI agent when it needs confirmation
- **Jira integration** — load tickets from epics or paste Jira URLs, auto-sync status bidirectionally
- **Auto PR creation** — draft PRs opened automatically when workers finish
- **Multi-repo support** — register multiple repositories, auto-match tickets by `[bracket]` tags in summaries
- **3-tier assignment** — set repository, branch, and agent profile globally, per prefix group, or per ticket
- **Smart watchdog** — auto-retry failed tickets, stale detection, working hours enforcement
- **Push notifications** — browser alerts when tickets complete or fail (Web Push for background)
- **Remote access** — access from your phone via Tailscale, ngrok, or Cloudflare Tunnel
- **Scheduler** — one-time or recurring runs with visual cron builder, all features optional
- **Auto-install** — missing Python dependencies installed automatically on first run

---

## Installation

### Prerequisites

You only need **Python 3.10+** installed. Everything else (dependencies, database, migrations) is handled automatically on first run.

<details>
<summary><strong>macOS</strong></summary>

```bash
brew install python@3.11 git
```

</details>

<details>
<summary><strong>Ubuntu / Debian</strong></summary>

```bash
sudo apt update
sudo apt install -y python3.11 python3-pip git
```

</details>

<details>
<summary><strong>Windows</strong></summary>

1. Download and install [Python 3.11+](https://www.python.org/downloads/) — check **"Add to PATH"** during install
2. Download and install [Git](https://git-scm.com/download/win)
3. Open **Command Prompt** or **PowerShell**

</details>

Verify Python is installed:

```bash
python3 --version   # macOS/Linux
python --version    # Windows
```

### Clone and Run

```bash
git clone https://github.com/jellythomas/task-ninja.git
cd task-ninja
python3 server.py        # macOS/Linux
python server.py         # Windows
```

On first run, Task Ninja will:

1. **Auto-install dependencies** — reads `requirements.txt` and installs missing packages
2. **Create `.env`** — configuration file with default settings
3. **Initialize the database** — SQLite database created at `task_ninja.db` with all migrations applied
4. **Generate an auth token** — displayed once in the terminal:

```
  ╔══════════════════════════════════════════════════════╗
  ║  Your Task Ninja auth token (save it now!):         ║
  ║                                                      ║
  ║  abc123...your-token-here...xyz789                   ║
  ║                                                      ║
  ║  This token is shown ONCE and never stored on disk.  ║
  ║  To regenerate: python server.py --regenerate-token  ║
  ╚══════════════════════════════════════════════════════╝
```

5. **Start the server** — available at **http://localhost:8420**

> **Save your auth token!** It's hashed and never stored in plain text. If you lose it, regenerate with `python server.py --regenerate-token`.

### Open the Dashboard

Open **http://localhost:8420** in your browser. The **Setup Wizard** appears automatically on first visit.

---

## Setup Wizard

The wizard walks you through three required steps. You can re-open it anytime from the header icon.

### 1. Jira Connection

Connect to your Atlassian instance:

| Field | Value | How to get it |
|-------|-------|---------------|
| **Jira Base URL** | `https://yourcompany.atlassian.net` | Your Jira cloud URL |
| **Email** | `you@company.com` | Your Jira account email |
| **API Token** | `ATATT3x...` | [Generate here](https://id.atlassian.com/manage-profile/security/api-tokens) → Create API token |

Click **Test Connection** to verify. You should see a green checkmark.

### 2. Repository

Register the git repository where AI agents will create branches and worktrees:

| Field | Value | Example |
|-------|-------|---------|
| **Name** | Display name | `my-app` |
| **Path** | Absolute path on disk | `/Users/you/projects/my-app` |
| **Default Branch** | Branch to fork from | `main` or `develop` |
| **Jira Prefix** | Auto-match tickets by key | `MC` (matches `MC-1234`) |

> The repository must be a git repo. Task Ninja creates worktrees inside a `.worktrees/` directory at the repo root.

### 3. Agent Profile

Configure which AI CLI agent executes tickets:

| Agent | Command | Args Template |
|-------|---------|---------------|
| Claude Code | `claude` | `--print "/execute-jira-task {JIRA_KEY}"` |
| Custom | `your-cli` | `--task {JIRA_KEY} --cwd {WORKTREE_PATH}` |

**Available template variables:**

| Variable | Description |
|----------|-------------|
| `{JIRA_KEY}` | Ticket key (e.g., `MC-1234`) |
| `{JIRA_SUMMARY}` | Ticket title from Jira |
| `{BRANCH_NAME}` | Git branch name created for this ticket |
| `{WORKTREE_PATH}` | Absolute path to the git worktree |
| `{PARENT_BRANCH}` | The branch the worktree was forked from |
| `{PROJECT_PATH}` | Root path of the registered repository |

Click **Finish** — you're ready to go!

---

## Usage

### Execute an Epic

1. Enter an Epic key (e.g., `MC-9056`) and click **Load Epic**
2. Select which tickets to work on from the modal
3. Click **Queue Selected** → tickets appear in the Queued column
4. Set max parallel workers (default: 2) and click **Start**

### Execute Specific Tickets

1. Switch to the **Paste Tickets** tab
2. Enter Jira keys (e.g., `MC-9173, MC-9174`)
3. Click **Queue All** → click **Start**

### Board Controls

- **Drag-and-drop** cards between columns to change status
- **Pause/Resume** active workers from the card menu
- **Live Terminal** — click any active ticket to view real-time worker output
- **Delete** — remove any ticket from the board

---

## Optional Configuration

### Remote Access

Access Task Ninja from your phone — even when it's running on your local machine.

**Enable it:** Settings > Remote Access > toggle on (or set `TASK_NINJA_REMOTE_ACCESS=true` in `.env`), then restart.

**Connect via Tailscale (recommended):**

```bash
# On your computer
brew install tailscale
tailscale up
tailscale ip -4          # Note the 100.x.x.x IP
```

On your phone: install [Tailscale](https://tailscale.com/download) and sign in with the same account. Open `http://100.x.x.x:8420` in your phone's browser and enter your auth token.

See the [full remote access guide](docs/architecture.md) for ngrok and Cloudflare Tunnel options.

### Push Notifications

Get alerted when tickets complete or fail:

1. Settings > Notifications > click **Enable** (grants browser permission)
2. Toggle **Server Notifications** on

For background notifications (tab closed), configure VAPID keys in `.env`. See [architecture docs](docs/architecture.md) for details.

### Scheduler, Auto-Retry & Working Hours

All scheduler features are **optional** and independently toggleable from the UI or `.env`.

**UI setup:** Settings > Scheduler tab — toggle each feature on/off, configure schedules with a visual cron builder, and set auto-retry/working hours parameters.

**`.env` setup:**

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

**Schedules:** Create recurring (cron) or one-time schedules from the Scheduler tab. Schedules re-run tickets already on the board — they don't create new tickets.

---

## How It Works

Each ticket goes through: **Todo → Queued → Planning → Developing → Review → Done**

The orchestrator picks queued tickets, creates a git worktree for each, spawns an AI agent worker, and streams output to the dashboard in real-time. On completion, it opens a draft PR and moves the ticket to Review. On failure, the watchdog can auto-retry.

For the full execution flow, architecture diagrams, API reference, database schema, and configuration options, see **[docs/architecture.md](docs/architecture.md)**.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `claude: command not found` | Install: `npm install -g @anthropic-ai/claude-code` |
| `mcp-atlassian` not found by Claude | Add it to `~/.claude/settings.json` under `mcpServers` |
| Claude can't access Jira | Verify `mcp-atlassian-with-bitbucket` works: open `claude` and ask "List my Jira projects" |
| Git worktree creation failed | Run `git worktree list` and `git worktree prune` |
| Jira API 401 | Check API token: [regenerate here](https://id.atlassian.com/manage-profile/security/api-tokens) |
| Worker stuck in Planning/Developing | Pause and resume the ticket (spawns fresh session) |
| Port 8420 in use | Set `TASK_NINJA_PORT=8421` in `.env` |
| Lost auth token | Regenerate: `python server.py --regenerate-token` |
| Bitbucket PR creation fails | Verify `BITBUCKET_APP_PASSWORD` in both `.env` and Claude Code MCP config |

---

## License

MIT
