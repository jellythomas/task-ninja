# Changelog

All notable changes to Task Ninja are documented in this file.

## [Unreleased]

### Added

#### Security & Remote Access
- **Bearer token authentication** — middleware protects all API endpoints when remote access is enabled
- **Login screen** — full-screen token input with localStorage persistence
- **`.env` file management** — secrets stored in `.env` (chmod 600) instead of SQLite
  - Auto-generates `TASK_NINJA_SECRET` on first run
  - Secret masking in API responses
  - Supports: `TASK_NINJA_SECRET`, `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `BITBUCKET_APP_PASSWORD`
- **Remote access toggle** — set `TASK_NINJA_REMOTE_ACCESS=true` to bind to `0.0.0.0` with auth enforced
- **WebSocket/SSE auth** — token passed via `?token=` query parameter

#### Ticket Watchdog (Event-Driven)
- **Auto-retry on failure** — configurable retry count and delay for failed tickets (e.g., token exhaustion)
  - `AUTO_RETRY_ENABLED`, `AUTO_RETRY_MAX`, `AUTO_RETRY_DELAY_MINUTES` in `.env`
- **Stale ticket detection** — per-ticket timers kill workers that exceed `WORKER_TIMEOUT_MINUTES`
- **Working hours window** — only spawn new workers within configured hours/days
  - `WORKING_HOURS_ENABLED`, `WORKING_HOURS_START`, `WORKING_HOURS_END`, `WORKING_HOURS_DAYS`
- **Zero polling overhead** — uses `asyncio.call_later` per-ticket timers, no background polling loop

#### Push Notifications
- **Browser Notification API** — in-tab notifications when tickets complete, fail, or run finishes
- **Web Push (VAPID)** — notifications even when the tab is closed (requires `pywebpush` + VAPID keys)
- **Notification settings panel** — enable/disable, test notification, VAPID status indicator
- **Server-side notifier** — `engine/notifier.py` with Web Push + SSE listener support

#### UI Improvements
- **Quick Setup Wizard** — 3-step wizard (Jira, Repository, Agent Profile) replaces old single-modal setup
- **Settings drawer** — slide-out panel from right with sidebar tabs (General, Repos, Profiles, Jira, Notifications)
- **Vibrant repo badges** — violet rounded-full badges on ticket cards
- **Terminal button** — cyan "Terminal" pill on active ticket cards for quick access
- **Mobile list view** — responsive layout for 375px+ screens

#### Multi-Repository & Agent Profiles
- **Repository management** — register repos with path, default branch, and Jira key prefix for auto-matching
- **Agent profiles** — configurable AI agents (Claude Code, Gemini CLI, etc.) with args templates
- **Per-ticket assignment** — override repository, parent branch, or agent profile per ticket

#### Engine Modules (Code Quality)
- `engine/env_manager.py` — `.env` file parsing, writing, secret masking
- `engine/auth.py` — Bearer token middleware + WebSocket auth
- `engine/ticket_watchdog.py` — event-driven retry/stale/working-hours
- `engine/notifier.py` — push notification manager

### Changed
- **Jira credentials** now read from `.env` instead of SQLite settings table
- **JiraClient** no longer requires `StateManager` in constructor
- **Orchestrator** integrates watchdog and notifier for ticket lifecycle events

#### Unified Ticket Modal (2026-03-06)
- **Jira URL parsing** — paste full Jira URLs (e.g., `https://jurnal.atlassian.net/browse/MC-9173`) anywhere: epic input, ticket input, or add-tickets API. Keys are auto-extracted.
- **Unified modal for Epic + Tickets** — both flows now open the same selection modal with Jira-fetched summaries, statuses, and auto-matched repositories
- **`POST /api/runs/{run_id}/fetch-tickets`** — new endpoint that fetches ticket details from Jira for the selection modal (used by Tickets paste flow)
- **Agent profile selector in modal** — dropdown to pick which AI agent executes the queued tickets
- **Auto-fill parent branch** — populated from the matched repo's `default_branch` based on Jira key prefix mapping
- **Migrated Jira search API** — from deprecated `/rest/api/3/search` to `/rest/api/3/search/jql`

#### Planned: 3-Tier Assignment Cascade
- Design approved for per-prefix-group and per-ticket assignment overrides
- See [design doc](docs/plans/2026-03-06-unified-ticket-modal-design.md) for full spec

### Fixed
- Stale tickets (planning/developing with dead worker PID) recovered on startup
- SSE reconnection handles auth token correctly
- `_testNotifSent` Alpine ReferenceError — variable declared in data object
- Settings form grids stack vertically on mobile (375px) instead of cramping
