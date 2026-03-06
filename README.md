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
- **Jira integration** — load tickets from epics, auto-sync status bidirectionally
- **Auto PR creation** — draft PRs opened automatically when workers finish
- **Multi-repo support** — register multiple repositories, auto-match tickets by Jira key prefix
- **Smart watchdog** — auto-retry failed tickets, stale detection, working hours enforcement
- **Push notifications** — browser alerts when tickets complete or fail (Web Push for background)
- **Remote access** — access from your phone via Tailscale, ngrok, or Cloudflare Tunnel
- **Scheduler** — one-time or recurring runs with cron expressions

---

## Quick Start

### Prerequisites

- Python >= 3.11
- Git >= 2.20
- An AI CLI agent ([Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Gemini CLI](https://github.com/google-gemini/gemini-cli), or custom)

### Install

```bash
git clone https://github.com/jellythomas/task-ninja.git
cd task-ninja
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
python server.py
```

Open **http://localhost:8420** — the Quick Setup Wizard will guide you through connecting Jira, adding a repository, and choosing your AI agent.

> On first run, Task Ninja auto-creates a `.env` file with a generated auth secret and initializes the database. No manual config needed.

---

## Setup

### 1. Jira Connection

The Setup Wizard (or Settings > Jira) asks for:
- **Jira Base URL** — e.g., `https://yourcompany.atlassian.net`
- **Email** — your Jira account email
- **API Token** — [generate one here](https://id.atlassian.com/manage-profile/security/api-tokens)

### 2. Repository

Register the project where AI agents will create branches:
- **Name** — display name (e.g., `my-app`)
- **Path** — absolute path on disk (e.g., `/Users/you/projects/my-app`)
- **Default Branch** — branch to create worktrees from (e.g., `main`)
- **Jira Prefix** — auto-match tickets by key prefix (e.g., `MC` matches `MC-1234`)

### 3. Agent Profile

Configure which AI agent executes tickets:

| Agent | Command | Args Template Example |
|-------|---------|----------------------|
| Claude Code | `claude` | `--print "/execute-jira-task {JIRA_KEY}"` |
| Gemini CLI | `gemini` | `-p "implement {JIRA_KEY}: {JIRA_SUMMARY}"` |
| Custom | `your-cli` | `--task {JIRA_KEY} --cwd {WORKTREE_PATH}` |

Available template variables: `{JIRA_KEY}`, `{JIRA_SUMMARY}`, `{BRANCH_NAME}`, `{WORKTREE_PATH}`, `{PARENT_BRANCH}`, `{PROJECT_PATH}`

### 4. Remote Access (Optional)

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

### 5. Push Notifications (Optional)

Get alerted when tickets complete or fail:

1. Settings > Notifications > click **Enable** (grants browser permission)
2. Toggle **Server Notifications** on

For background notifications (tab closed), configure VAPID keys in `.env`. See [architecture docs](docs/architecture.md) for details.

### 6. Auto-Retry & Working Hours (Optional)

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

---

## Usage

### Execute an Epic

1. Enter an Epic key (e.g., `MC-9056`) and click **Load Epic**
2. Select which tickets to work on from the modal
3. Click **Queue Selected** → tickets appear in the Queued column
4. Set max parallel workers and click **Start**

### Execute Specific Tickets

1. Switch to "Paste Tickets" tab
2. Enter Jira keys (e.g., `MC-9173, MC-9174`)
3. Click **Queue All** → click **Start**

### Board Controls

- **Drag-and-drop** cards between columns
- **Pause/Resume** active workers from the card
- **Live Terminal** — click any active ticket to view its worker output
- **Delete** — remove any ticket from the board

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
| Git worktree creation failed | Run `git worktree list` and `git worktree prune` |
| Jira API 401 | Check API token: [regenerate here](https://id.atlassian.com/manage-profile/security/api-tokens) |
| Worker stuck in Planning/Developing | Pause and resume the ticket (spawns fresh session) |
| Port 8420 in use | Set `TASK_NINJA_PORT=8421` in `.env` |

---

## License

MIT
