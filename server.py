#!/usr/bin/env python3
"""Task Ninja — FastAPI server + orchestrator."""

import subprocess
import sys
from pathlib import Path


def _check_python_version():
    """Ensure Python version is compatible (3.10+)."""
    if sys.version_info < (3, 10):
        print(f"[server] Python 3.10+ required (found {sys.version})", file=sys.stderr)
        if sys.platform == "win32":
            print(f"[server] Download from: https://www.python.org/downloads/", file=sys.stderr)
        elif sys.platform == "darwin":
            print(f"[server] Install with: brew install python@3.12", file=sys.stderr)
        else:
            print(f"[server] Install with: sudo apt install python3.12", file=sys.stderr)
        sys.exit(1)


def _check_git():
    """Check if git is available, suggest install if missing."""
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            return
    except FileNotFoundError:
        pass
    print(f"[server] WARNING: git not found — required for worktree-based ticket execution", file=sys.stderr)
    if sys.platform == "win32":
        print(f"[server] Download from: https://git-scm.com/download/win", file=sys.stderr)
    elif sys.platform == "darwin":
        print(f"[server] Install with: brew install git", file=sys.stderr)
    else:
        print(f"[server] Install with: sudo apt install git", file=sys.stderr)


def _check_node():
    """Check if Node.js is available, suggest install if missing."""
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            return
    except FileNotFoundError:
        pass
    print(f"[server] WARNING: Node.js not found — required for AI CLI agents (Claude Code, etc.)", file=sys.stderr)
    if sys.platform == "win32":
        print(f"[server] Download from: https://nodejs.org/", file=sys.stderr)
    elif sys.platform == "darwin":
        print(f"[server] Install with: brew install node", file=sys.stderr)
    else:
        print(f"[server] Install with: sudo apt install nodejs npm", file=sys.stderr)


def _ensure_config():
    """Create config.yaml with defaults if missing."""
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        return
    default_config = """# Task Ninja configuration
orchestrator:
  poll_interval: 5

claude:
  idle_timeout: 10

mcp:
  jira_status_mapping:
    planning: "In Progress"
    developing: "In Progress"
    review: "In Review"
    done: "Done"

git:
  worktree_dir: ".worktrees"
  branch_prefix: "feat"

database:
  path: "task_ninja.db"
"""
    config_path.write_text(default_config)
    print(f"[server] Created config.yaml with defaults", file=sys.stderr)


def _check_dependencies():
    """Auto-install missing dependencies from requirements.txt on first run."""
    req_file = Path(__file__).parent / "requirements.txt"
    if not req_file.exists():
        return
    from importlib.metadata import distribution, PackageNotFoundError
    import re
    # Extract package names from requirements.txt (strip extras, versions)
    missing = []
    for line in req_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Extract base package name (before [extras] or version spec)
        match = re.match(r"([a-zA-Z0-9_-]+)", line)
        if not match:
            continue
        pkg = match.group(1)
        try:
            distribution(pkg)
        except PackageNotFoundError:
            missing.append(line)
    if missing:
        print(f"[server] Missing dependencies: {', '.join(missing)}", file=sys.stderr)
        print(f"[server] Installing from requirements.txt...", file=sys.stderr)
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file), "-q"],
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError:
            # Homebrew/system Python — needs --break-system-packages or --user
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "-r", str(req_file),
                     "-q", "--break-system-packages"],
                )
            except subprocess.CalledProcessError:
                print(f"[server] Auto-install failed. Please run manually:", file=sys.stderr)
                print(f"  pip3 install -r {req_file}", file=sys.stderr)
                return
        print(f"[server] Dependencies installed.", file=sys.stderr)


_check_python_version()
_check_dependencies()
_check_git()
_check_node()
_ensure_config()

import asyncio
from contextlib import asynccontextmanager

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from engine.auth import AuthMiddleware, verify_ws_token
from engine.git_manager import GitManager
from engine.broadcaster import Broadcaster
from engine.claude_helper import ClaudeHelper
from engine.env_manager import load_env, get_env, update_env, get_public_env, verify_token, generate_token
from engine.jira_client import JiraClient
from engine.notifier import Notifier
from engine.orchestrator import Orchestrator
from engine.scheduler import RunScheduler
from engine.state import StateManager, init_db
from engine.terminal import TerminalManager
from models.ticket import (
    AddTicketsRequest,
    CreateAgentProfileRequest,
    CreateLabelMappingRequest,
    CreateRepositoryRequest,
    CreateRunRequest,
    CreateScheduleRequest,
    FetchTicketsRequest,
    LoadEpicRequest,
    MoveTicketRequest,
    RunStatus,
    TicketState,
    UpdateAgentProfileRequest,
    UpdateConfigRequest,
    UpdateRankRequest,
    UpdateRepositoryRequest,
    UpdateScheduleRequest,
    UpdateSettingsRequest,
    ResolveInputRequest,
    UpdateTicketAssignmentRequest,
)

# Load .env first (creates file with defaults if missing)
env_config = load_env()

# Load config
CONFIG_PATH = Path(__file__).parent / "config.yaml"
config = yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}

# Resolve database path to absolute (relative paths are relative to project root)
# This ensures workers running in worktree directories can still find the database
_db_path_cfg = config.get("database", {}).get("path", "task_ninja.db")
DB_PATH = str(Path(__file__).parent / _db_path_cfg) if not Path(_db_path_cfg).is_absolute() else _db_path_cfg

# Shared instances
state = StateManager(DB_PATH)
broadcaster = Broadcaster()
orchestrator = Orchestrator(state, broadcaster, config)
claude_helper = ClaudeHelper("claude")
jira_client = JiraClient()
terminal_manager = TerminalManager()
notifier = Notifier(state)
orchestrator.notifier = notifier
run_scheduler = RunScheduler(state, orchestrator.start)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DB_PATH)  # Now synchronous - uses yoyo migrations
    print(f"[server] Database initialized at {DB_PATH}", file=sys.stderr)

    # Restore orchestrator state from DB on startup
    runs = await state.list_runs()
    for run in runs:
        if run.status == RunStatus.RUNNING:
            print(f"[server] Restoring running state for run {run.id}", file=sys.stderr)
            await orchestrator.start(run.id)
            break
        elif run.status in (RunStatus.PAUSED, RunStatus.COMPLETED):
            # Restore run_id so pause/resume work after server restart
            orchestrator._run_id = run.id
            print(f"[server] Restored run_id {run.id} (status: {run.status})", file=sys.stderr)
            break

    run_scheduler.start()
    await run_scheduler.load_existing_schedules()
    yield
    run_scheduler.stop()
    orchestrator.watchdog.cancel_all()
    terminal_manager.close_all()


app = FastAPI(title="Task Ninja", lifespan=lifespan)
app.add_middleware(AuthMiddleware)


# --- Auth ---

@app.post("/api/auth/login")
async def auth_login(req: dict):
    """Validate token and return success."""
    token = req.get("token", "")
    if verify_token(token):
        return {"status": "ok"}
    raise HTTPException(401, "Invalid token")


@app.get("/api/auth/status")
async def auth_status():
    """Check if auth is required."""
    remote = get_env("TASK_NINJA_REMOTE_ACCESS", "false").lower() == "true"
    return {"auth_required": remote}


# --- Tailscale ---

@app.get("/api/tailscale/status")
async def tailscale_status():
    """Check Tailscale installation and connection status."""
    import shutil
    result = {"installed": False, "running": False, "ip": None, "url": None}

    if not shutil.which("tailscale"):
        return result
    result["installed"] = True

    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            import json as _json
            ts = _json.loads(stdout)
            if ts.get("Self", {}).get("Online"):
                result["running"] = True
                # Get IPv4 address
                addrs = ts["Self"].get("TailscaleIPs", [])
                ipv4 = next((a for a in addrs if "." in a), None)
                if ipv4:
                    port = int(get_env("TASK_NINJA_PORT") or "8420")
                    result["ip"] = ipv4
                    result["url"] = f"http://{ipv4}:{port}"
    except Exception:
        pass

    return result


@app.post("/api/tailscale/install")
async def tailscale_install():
    """Install Tailscale via package manager."""
    import platform, shutil

    if shutil.which("tailscale"):
        return {"status": "already_installed"}

    system = platform.system().lower()
    if system == "darwin":
        cmd = ["brew", "install", "--cask", "tailscale"]
    elif system == "linux":
        # Use the official Tailscale install script
        cmd = ["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"]
    else:
        raise HTTPException(400, f"Auto-install not supported on {system}. Install manually from https://tailscale.com/download")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

    if proc.returncode != 0:
        raise HTTPException(500, f"Install failed: {stderr.decode()[-500:]}")

    return {"status": "installed", "message": "Tailscale installed. Open the Tailscale app to log in, then check status again."}


@app.post("/api/tailscale/up")
async def tailscale_up():
    """Start Tailscale (tailscale up)."""
    import shutil
    if not shutil.which("tailscale"):
        raise HTTPException(400, "Tailscale is not installed")

    proc = await asyncio.create_subprocess_exec(
        "tailscale", "up",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

    if proc.returncode != 0:
        err = stderr.decode()
        if "login" in err.lower() or "auth" in err.lower():
            return {"status": "needs_login", "message": "Open the Tailscale app to log in first."}
        raise HTTPException(500, f"Failed to start: {err[-500:]}")

    return {"status": "started"}


# --- Environment Config ---

@app.get("/api/env")
async def get_env_config():
    """Get .env configuration (secrets masked)."""
    return get_public_env()


@app.put("/api/env")
async def update_env_config(req: dict):
    """Update .env configuration."""
    updates = req.get("settings", req)
    # Don't allow overwriting secret via API unless explicitly provided
    if "TASK_NINJA_SECRET" in updates and not updates["TASK_NINJA_SECRET"]:
        del updates["TASK_NINJA_SECRET"]
    update_env(updates)
    return {"status": "updated"}


# --- Static UI ---

@app.get("/")
async def serve_ui():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# --- Runs ---

@app.post("/api/runs")
async def create_run(req: CreateRunRequest):
    # If repository_id provided, resolve project_path from repo
    project_path = req.project_path
    if req.repository_id and not project_path:
        repo = await state.get_repository(req.repository_id)
        if repo:
            project_path = repo.path
    run = await state.create_run(req.name, project_path, req.max_parallel)
    if req.parent_branch or req.repository_id:
        updates = {}
        if req.parent_branch:
            updates["parent_branch"] = req.parent_branch
        if req.repository_id:
            updates["repository_id"] = req.repository_id
        await state.update_run_config(run.id, **updates)
        run = await state.get_run(run.id)
    return run.model_dump()


@app.get("/api/runs")
async def list_runs():
    runs = await state.list_runs()
    return [r.model_dump() for r in runs]


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    tickets = await state.get_tickets_for_run(run_id)
    return {
        **run.model_dump(),
        "tickets": [t.model_dump() for t in tickets],
    }


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str):
    # Kill all workers for this run
    tickets = await state.get_tickets_for_run(run_id)
    for t in tickets:
        if t.state in {TicketState.PLANNING, TicketState.DEVELOPING}:
            await orchestrator.delete_ticket(t.id)
    await state.delete_run(run_id)
    return {"status": "deleted"}


@app.put("/api/runs/{run_id}/config")
async def update_run_config(run_id: str, req: UpdateConfigRequest):
    updates = {}
    if req.max_parallel is not None:
        updates["max_parallel"] = req.max_parallel
    if updates:
        await state.update_run_config(run_id, **updates)
    return {"status": "updated"}


@app.post("/api/runs/{run_id}/start")
async def start_run(run_id: str):
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await orchestrator.start(run_id)
    return {"status": "started"}


@app.post("/api/runs/{run_id}/pause")
async def pause_run(run_id: str):
    await orchestrator.pause(run_id)
    return {"status": "paused"}


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str):
    await orchestrator.resume(run_id)
    return {"status": "resumed"}


# --- Tickets ---

@app.post("/api/runs/{run_id}/load-epic")
async def load_epic(run_id: str, req: LoadEpicRequest):
    """Load tickets from a Jira epic. Uses direct API if configured, falls back to Claude CLI."""
    import re
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    # Parse epic key from Jira URL if needed
    epic_key = req.epic_key.strip()
    m = re.search(r'/browse/([A-Z][A-Z0-9]+-\d+)', epic_key, re.IGNORECASE)
    if m:
        epic_key = m.group(1).upper()
    else:
        epic_key = epic_key.upper()

    await state.update_run_config(run_id, epic_key=epic_key)

    # Try direct Jira API first, fall back to Claude CLI + MCP
    if await jira_client.is_configured():
        children = await jira_client.fetch_epic_children(epic_key)
    else:
        children = await claude_helper.fetch_epic_children(epic_key)

    # Load repositories for auto-assigning by jira_label
    repos = await state.list_repositories()
    repo_map = {r.id: r for r in repos}

    # Build label -> repo_id lookup from repo.jira_label
    label_to_repo = {}
    for r in repos:
        if r.jira_label:
            label_to_repo[r.jira_label.lower()] = r.id

    # Return ticket data without persisting — user selects in modal, then add-tickets creates them
    tickets = []
    for child in children:
        key = child.get("key", "").strip().upper()
        if not key:
            continue
        # Check if already on the board
        existing = await state.get_ticket_by_jira_key(run_id, key)

        # Auto-detect repository from ticket key prefix or labels/components
        matched_repo_id = None
        # Match by ticket key prefix (e.g., "MC-1234" matches jira_label "MC")
        key_prefix = key.split("-")[0].lower() if "-" in key else ""
        if key_prefix and key_prefix in label_to_repo:
            matched_repo_id = label_to_repo[key_prefix]
        else:
            # Fallback: match against Jira labels/components
            child_labels = [l.lower() for l in child.get("labels", [])]
            child_components = [c.lower() for c in child.get("components", [])]
            all_tags = child_labels + child_components
            for tag in all_tags:
                for label_key, repo_id in label_to_repo.items():
                    if label_key in tag or tag in label_key:
                        matched_repo_id = repo_id
                        break
                if matched_repo_id:
                    break

        ticket_data = {
            "jira_key": key,
            "summary": child.get("summary"),
            "status": child.get("status", "To Do"),
            "already_added": existing is not None,
            "labels": child.get("labels", []),
            "components": child.get("components", []),
        }
        if matched_repo_id:
            repo = repo_map.get(matched_repo_id)
            ticket_data["matched_repository_id"] = matched_repo_id
            ticket_data["matched_repository_name"] = repo.name if repo else None

        tickets.append(ticket_data)

    return {
        "status": "epic_loaded",
        "epic_key": epic_key,
        "found": len(tickets),
        "tickets": tickets,
    }


@app.post("/api/runs/{run_id}/fetch-tickets")
async def fetch_tickets(run_id: str, req: FetchTicketsRequest):
    """Fetch ticket details from Jira for the selection modal."""
    import re
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    # Parse keys — extract from URLs like https://jurnal.atlassian.net/browse/BRO-9173
    parsed_keys = []
    for raw in req.keys:
        raw = raw.strip()
        if not raw:
            continue
        # Extract key from Jira URL
        m = re.search(r'/browse/([A-Z][A-Z0-9]+-\d+)', raw, re.IGNORECASE)
        if m:
            parsed_keys.append(m.group(1).upper())
        else:
            parsed_keys.append(raw.upper())

    repos = await state.list_repositories()
    label_to_repo = {}
    repo_map = {r.id: r for r in repos}
    for r in repos:
        if r.jira_label:
            label_to_repo[r.jira_label.lower()] = r.id

    tickets = []
    for key in parsed_keys:
        existing = await state.get_ticket_by_jira_key(run_id, key)
        # Fetch from Jira
        issue = None
        if await jira_client.is_configured():
            issue = await jira_client.get_issue(key)

        # Auto-detect repository
        matched_repo_id = None
        key_prefix = key.split("-")[0].lower() if "-" in key else ""
        if key_prefix and key_prefix in label_to_repo:
            matched_repo_id = label_to_repo[key_prefix]

        ticket_data = {
            "jira_key": key,
            "summary": issue.get("summary", "") if issue else None,
            "status": issue.get("status", "To Do") if issue else "Unknown",
            "already_added": existing is not None,
            "labels": issue.get("labels", []) if issue else [],
            "components": issue.get("components", []) if issue else [],
        }
        if matched_repo_id:
            repo = repo_map.get(matched_repo_id)
            ticket_data["matched_repository_id"] = matched_repo_id
            ticket_data["matched_repository_name"] = repo.name if repo else None

        tickets.append(ticket_data)

    return {
        "status": "tickets_fetched",
        "found": len(tickets),
        "tickets": tickets,
    }


@app.post("/api/runs/{run_id}/add-tickets")
async def add_tickets(run_id: str, req: AddTicketsRequest):
    """Add tickets by Jira keys. Goes directly to Queued."""
    import re
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    added = []
    summaries = req.summaries or {}
    for raw_key in req.keys:
        raw_key = raw_key.strip()
        if not raw_key:
            continue
        # Extract key from Jira URL if needed
        m = re.search(r'/browse/([A-Z][A-Z0-9]+-\d+)', raw_key, re.IGNORECASE)
        key = m.group(1).upper() if m else raw_key.upper()
        existing = await state.get_ticket_by_jira_key(run_id, key)
        if existing:
            continue
        summary = summaries.get(key)
        ticket = await state.add_ticket(run_id, key, summary=summary, state=TicketState.QUEUED)
        # Resolve assignment: per-ticket override > global fallback
        per_ticket = (req.assignments or {}).get(key)
        assignment = {}
        repo_id = (per_ticket.repository_id if per_ticket else None) or req.repository_id
        branch = (per_ticket.parent_branch if per_ticket else None) or req.parent_branch
        profile = (per_ticket.profile_id if per_ticket else None) or req.profile_id
        if repo_id:
            assignment["repository_id"] = repo_id
        if branch:
            assignment["parent_branch"] = branch
        if profile:
            assignment["profile_id"] = profile
        if assignment:
            await state.update_ticket(ticket.id, **assignment)
            ticket = await state.get_ticket(ticket.id)
        added.append(ticket.model_dump())
        await broadcaster.broadcast_ticket_update(run_id, ticket.id, TicketState.QUEUED)

    return {"added": len(added), "tickets": added}


@app.put("/api/tickets/{ticket_id}/state")
async def move_ticket(ticket_id: str, req: MoveTicketRequest):
    """Move a ticket to a new state. Kills worker if moving to non-active state."""
    try:
        print(f"[move_ticket] {ticket_id} -> {req.state.value}", file=sys.stderr)

        # Kill worker if moving to a non-active state
        if req.state not in {TicketState.PLANNING, TicketState.DEVELOPING}:
            try:
                killed = await orchestrator.kill_worker(ticket_id)
                print(f"[move_ticket] kill_worker={killed}", file=sys.stderr)
                if killed:
                    await asyncio.sleep(0.3)
            except Exception as e:
                print(f"[move_ticket] kill_worker error (ignoring): {e}", file=sys.stderr)

        # Clear phase progress when moving back to todo (full restart)
        if req.state == TicketState.TODO:
            await state.update_ticket(ticket_id, last_completed_phase=None, error=None)

        ticket = await state.update_ticket_state(ticket_id, req.state)
        print(f"[move_ticket] DB updated, state now={ticket.state}", file=sys.stderr)

        try:
            await broadcaster.broadcast_ticket_update(ticket.run_id, ticket_id, req.state)
        except Exception as e:
            print(f"[move_ticket] broadcast error (ignoring): {e}", file=sys.stderr)

        # If moved to queued and orchestrator is stopped, restart it so it picks up the ticket
        if req.state == TicketState.QUEUED and not orchestrator._running and ticket.run_id:
            await orchestrator.resume(ticket.run_id)

        # Sync to Jira for manually-triggered state changes (fire and forget)
        mcp_cfg = config.get("mcp", {})
        jira_mapping = mcp_cfg.get("jira_status_mapping", {})
        target_status = jira_mapping.get(req.state.value)
        if target_status:
            if await jira_client.is_configured():
                asyncio.create_task(
                    jira_client.transition_issue(ticket.jira_key, target_status)
                )
            else:
                asyncio.create_task(
                    claude_helper.transition_jira_issue(ticket.jira_key, target_status)
                )

        return ticket.model_dump()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        print(f"[move_ticket] UNEXPECTED ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, str(e))


@app.put("/api/tickets/{ticket_id}/rank")
async def update_rank(ticket_id: str, req: UpdateRankRequest):
    await state.update_ticket_rank(ticket_id, req.rank)
    return {"status": "updated"}


@app.post("/api/tickets/{ticket_id}/pause")
async def pause_ticket(ticket_id: str):
    await orchestrator.pause_ticket(ticket_id)
    return {"status": "paused"}


@app.post("/api/tickets/{ticket_id}/resume")
async def resume_ticket(ticket_id: str):
    await orchestrator.resume_ticket(ticket_id)
    return {"status": "resumed"}


@app.post("/api/tickets/{ticket_id}/interrupt")
async def interrupt_ticket(ticket_id: str):
    """Send Escape to the worker's PTY to interrupt current operation."""
    sent = orchestrator.interrupt_worker(ticket_id)
    return {"status": "interrupted" if sent else "no_worker"}


@app.post("/api/tickets/{ticket_id}/resolve-input")
async def resolve_ticket_input(ticket_id: str, req: ResolveInputRequest):
    """Resolve an AWAITING_INPUT ticket (e.g., branch mismatch)."""
    try:
        result = await orchestrator.resolve_input(ticket_id, req.choice)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tickets/{ticket_id}/retry")
async def retry_ticket(ticket_id: str, clean: bool = False):
    """Retry a failed/done ticket. clean=true destroys worktree for fresh start."""
    ticket = await state.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    # Kill any lingering worker
    await orchestrator.kill_worker(ticket_id)

    # Clean worktree if requested
    if clean and ticket.worktree_path:
        try:
            run = await state.get_run(ticket.run_id)
            git_cfg = config.get("git", {})
            git = GitManager(
                run.project_path if run else ".",
                git_cfg.get("worktree_dir", ".worktrees"),
            )
            await git._remove_worktree(Path(ticket.worktree_path))
        except Exception:
            pass  # Best effort — orchestrator will handle it on spawn

    # Clear error and move to queued
    await state.update_ticket(ticket_id, error=None, worker_pid=None, paused=False)
    await state.update_ticket_state(ticket_id, TicketState.QUEUED)
    await broadcaster.broadcast_ticket_update(ticket.run_id, ticket_id, TicketState.QUEUED)

    # Auto-resume orchestrator if needed
    if not orchestrator._running and ticket.run_id:
        await orchestrator.resume(ticket.run_id)

    return {"status": "retrying", "clean": clean}


@app.delete("/api/tickets/{ticket_id}")
async def delete_ticket(ticket_id: str):
    await orchestrator.delete_ticket(ticket_id)
    return {"status": "deleted"}


# --- Logs ---

@app.get("/api/logs/{ticket_id}")
async def get_logs(ticket_id: str, tail: int = 200):
    logs = await state.get_logs(ticket_id, tail)
    return {"logs": logs}


# --- SSE Stream ---

@app.get("/api/stream/{run_id}")
async def stream(run_id: str):
    """SSE endpoint for real-time board updates."""
    queue = broadcaster.subscribe(run_id)

    async def event_generator():
        try:
            while True:
                message = await queue.get()
                yield {"data": message}
        except asyncio.CancelledError:
            broadcaster.unsubscribe(run_id, queue)

    return EventSourceResponse(event_generator())


# --- Schedules ---

@app.post("/api/schedules")
async def create_schedule(req: CreateScheduleRequest):
    schedule = await state.create_schedule(
        req.run_id,
        req.schedule_type,
        cron_expression=req.cron_expression,
        start_time=req.start_time.isoformat() if req.start_time else None,
        end_time=req.end_time.isoformat() if req.end_time else None,
    )
    await run_scheduler.add_schedule(
        schedule.id, req.run_id, req.schedule_type,
        cron_expression=req.cron_expression,
        start_time=req.start_time.isoformat() if req.start_time else None,
    )
    return schedule.model_dump()


@app.get("/api/schedules")
async def list_schedules(run_id: str = None):
    schedules = await state.list_schedules(run_id)
    return [s.model_dump() for s in schedules]


@app.patch("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, req: UpdateScheduleRequest):
    schedule = await state.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    updates = req.model_dump(exclude_none=True)
    if "end_time" in updates and updates["end_time"]:
        updates["end_time"] = updates["end_time"].isoformat()
    updated = await state.update_schedule(schedule_id, **updates)
    # Re-register with scheduler if cron changed or toggled
    if req.enabled is False:
        run_scheduler.remove_schedule(schedule_id)
    elif req.enabled is True or req.cron_expression:
        run_scheduler.remove_schedule(schedule_id)
        if updated.enabled:
            await run_scheduler.add_schedule(
                updated.id, updated.run_id, updated.schedule_type,
                cron_expression=updated.cron_expression,
                start_time=updated.start_time.isoformat() if updated.start_time else None,
            )
    return updated.model_dump()


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    run_scheduler.remove_schedule(schedule_id)
    await state.delete_schedule(schedule_id)
    return {"status": "deleted"}


# --- Repositories ---

@app.get("/api/repositories")
async def list_repositories():
    repos = await state.list_repositories()
    return [r.model_dump() for r in repos]


@app.post("/api/repositories")
async def create_repository(req: CreateRepositoryRequest):
    repo = await state.create_repository(req.name, req.path, req.default_branch, req.jira_label, req.default_profile_id)
    return repo.model_dump()


@app.put("/api/repositories/{repo_id}")
async def update_repository(repo_id: int, req: UpdateRepositoryRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")
    repo = await state.update_repository(repo_id, **updates)
    if not repo:
        raise HTTPException(404, "Repository not found")
    return repo.model_dump()


@app.delete("/api/repositories/{repo_id}")
async def delete_repository(repo_id: int):
    await state.delete_repository(repo_id)
    return {"status": "deleted"}


# --- Label Mappings ---

@app.get("/api/label-mappings")
async def list_label_mappings():
    mappings = await state.list_label_mappings()
    return [m.model_dump() for m in mappings]


@app.post("/api/label-mappings")
async def create_label_mapping(req: CreateLabelMappingRequest):
    mapping = await state.create_label_mapping(req.jira_label, req.repository_id)
    return mapping.model_dump()


@app.delete("/api/label-mappings/{mapping_id}")
async def delete_label_mapping(mapping_id: int):
    await state.delete_label_mapping(mapping_id)
    return {"status": "deleted"}


# --- Agent Profiles ---

@app.get("/api/profiles")
async def list_agent_profiles():
    profiles = await state.list_agent_profiles()
    return [p.model_dump() for p in profiles]


@app.post("/api/profiles")
async def create_agent_profile(req: CreateAgentProfileRequest):
    profile = await state.create_agent_profile(
        req.name, req.command, req.args_template, req.log_format,
        phases_config=req.phases_config,
    )
    return profile.model_dump()


@app.put("/api/profiles/{profile_id}")
async def update_agent_profile(profile_id: int, req: UpdateAgentProfileRequest):
    # Use exclude_unset so only fields the client sent are included (allows clearing phases_config to None)
    updates = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None or k == "phases_config"}
    if not updates:
        raise HTTPException(400, "No fields to update")
    profile = await state.update_agent_profile(profile_id, **updates)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile.model_dump()


@app.put("/api/profiles/{profile_id}/default")
async def set_default_profile(profile_id: int):
    await state.set_default_agent_profile(profile_id)
    return {"status": "updated"}


@app.delete("/api/profiles/{profile_id}")
async def delete_agent_profile(profile_id: int):
    await state.delete_agent_profile(profile_id)
    return {"status": "deleted"}


# --- Settings ---

@app.get("/api/settings")
async def get_settings():
    all_settings = await state.get_all_settings()
    # Mask sensitive values
    masked = {}
    for k, v in all_settings.items():
        if "token" in k.lower() or "secret" in k.lower() or "password" in k.lower():
            masked[k] = v[:4] + "****" if len(v) > 4 else "****"
        else:
            masked[k] = v
    return masked


@app.put("/api/settings")
async def update_settings(req: UpdateSettingsRequest):
    await state.set_settings(req.settings)
    return {"status": "updated"}


@app.get("/api/watchdog/status")
async def watchdog_status():
    """Get watchdog status (active timers, retries, working hours)."""
    return orchestrator.watchdog.get_status()


# --- Notifications ---

@app.get("/api/notifications/vapid-key")
async def get_vapid_key():
    """Get VAPID public key for Web Push subscription."""
    key = notifier.get_vapid_public_key()
    return {"key": key, "enabled": notifier.is_enabled()}


@app.post("/api/notifications/subscribe")
async def subscribe_push(req: dict):
    """Store a Web Push subscription."""
    subscription = req.get("subscription")
    if not subscription:
        raise HTTPException(400, "Missing subscription object")
    await notifier.store_subscription(subscription)
    return {"status": "subscribed"}


@app.delete("/api/notifications/subscribe")
async def unsubscribe_push(req: dict):
    """Remove a Web Push subscription."""
    endpoint = req.get("endpoint", "")
    if not endpoint:
        raise HTTPException(400, "Missing endpoint")
    await notifier.remove_subscription(endpoint)
    return {"status": "unsubscribed"}


@app.get("/api/settings/jira-status")
async def jira_status():
    """Check if Jira credentials are configured."""
    configured = await jira_client.is_configured()
    return {"configured": configured}


@app.post("/api/settings/test-jira")
async def test_jira_connection():
    """Test Jira API connection with .env credentials."""
    jira_url = get_env("JIRA_BASE_URL")
    jira_email = get_env("JIRA_EMAIL")
    jira_token = get_env("JIRA_API_TOKEN")
    if not all([jira_url, jira_email, jira_token]):
        raise HTTPException(400, "Jira credentials not configured in .env")
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{jira_url.rstrip('/')}/rest/api/3/myself",
                auth=(jira_email, jira_token),
                timeout=10,
            )
            if resp.status_code == 200:
                user = resp.json()
                return {"status": "connected", "user": user.get("displayName", user.get("emailAddress"))}
            else:
                raise HTTPException(resp.status_code, f"Jira API returned {resp.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Connection failed: {e}")


# --- Ticket Assignment ---

@app.put("/api/tickets/{ticket_id}/assignment")
async def update_ticket_assignment(ticket_id: str, req: UpdateTicketAssignmentRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")
    await state.update_ticket(ticket_id, **updates)
    ticket = await state.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    return ticket.model_dump()


# --- Interactive Terminal ---

@app.websocket("/ws/terminal/{ticket_id}")
async def terminal_ws(websocket: WebSocket, ticket_id: str):
    """WebSocket endpoint to attach to a running worker's live terminal.

    Attach/detach model: closing the terminal does NOT kill the worker process.
    """
    import json as _json

    # Auth check for WebSocket
    if not verify_ws_token(websocket):
        await websocket.close(code=4001, reason="Authentication required")
        return

    # Find the live worker for this ticket (with retry for startup race)
    worker = orchestrator._workers.get(ticket_id)

    # Worker might not be in dict yet if orchestrator just spawned it — retry
    if not worker:
        for _ in range(10):
            await asyncio.sleep(0.5)
            worker = orchestrator._workers.get(ticket_id)
            if worker:
                break

    # Worker exists but process not spawned yet — wait for it
    if worker and not worker.is_running:
        for _ in range(20):  # Up to 10 seconds
            await asyncio.sleep(0.5)
            if worker.is_running:
                break

    # If no active worker, try reusing or spawning an ad-hoc terminal for review/done tickets
    adhoc = None
    if not worker or not worker.is_running:
        # Check for existing ad-hoc terminal first
        existing_adhoc = orchestrator._adhoc_terminals.get(ticket_id)
        if existing_adhoc and existing_adhoc.is_running:
            worker = existing_adhoc
            adhoc = existing_adhoc
        else:
            # Clean up dead ad-hoc terminal if present
            if existing_adhoc:
                orchestrator._adhoc_terminals.pop(ticket_id, None)
            # Spawn new ad-hoc terminal for review/done/failed tickets
            ticket = await state.get_ticket(ticket_id)
            if ticket and ticket.worktree_path and Path(ticket.worktree_path).is_dir() and ticket.state in ('review', 'done', 'failed'):
                from engine.worker import AdHocTerminal
                try:
                    # Resolve the ticket's agent profile command (ccs, claude, etc.)
                    adhoc_command = "claude"
                    if ticket.profile_id:
                        profile = await state.get_agent_profile(ticket.profile_id)
                        if profile and profile.command:
                            adhoc_command = profile.command
                    adhoc = AdHocTerminal(worktree_path=ticket.worktree_path, claude_command=adhoc_command)
                    await adhoc.start()
                    orchestrator._adhoc_terminals[ticket_id] = adhoc
                    worker = adhoc
                    # Update ticket PID and broadcast so UI shows it
                    if adhoc.process and adhoc.process.pid:
                        await state.update_ticket(ticket_id, worker_pid=adhoc.process.pid)
                        await broadcaster.broadcast_ticket_update(
                            orchestrator._run_id, ticket_id, ticket.state,
                            worker_pid=adhoc.process.pid
                        )
                except Exception as e:
                    print(f"[adhoc] Failed to spawn ad-hoc terminal for {ticket_id}: {e}", file=sys.stderr)
                    await websocket.accept()
                    reason = str(e)[:120]  # WebSocket close reasons limited to 123 bytes
                    await websocket.close(code=4005, reason=f"Spawn failed: {reason}")
                    return
            else:
                await websocket.accept()
                await websocket.close(code=4004, reason="No running process for this ticket")
                return

    await websocket.accept()
    await worker.attach_viewer(websocket)

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.receive":
                if "bytes" in message and message["bytes"]:
                    worker.write_input(message["bytes"])
                elif "text" in message and message["text"]:
                    try:
                        ctrl = _json.loads(message["text"])
                        if ctrl.get("type") == "resize":
                            worker.resize_pty(ctrl.get("rows", 24), ctrl.get("cols", 80))
                        elif ctrl.get("type") == "ping":
                            await websocket.send_text(_json.dumps({"type": "pong"}))
                    except (_json.JSONDecodeError, KeyError):
                        worker.write_input(message["text"].encode())
            elif message["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        worker.detach_viewer(websocket)
        # Clean up ad-hoc terminal if no more viewers and process died
        if adhoc and not adhoc._viewers and not adhoc.is_running:
            orchestrator._adhoc_terminals.pop(ticket_id, None)
            await state.update_ticket(ticket_id, worker_pid=None)
            ticket = await state.get_ticket(ticket_id)
            if ticket:
                await broadcaster.broadcast_ticket_update(
                    orchestrator._run_id, ticket_id, ticket.state, worker_pid=None
                )


@app.post("/api/tickets/{ticket_id}/terminal-input")
async def terminal_input(ticket_id: str, req: dict):
    """Send input text to a running worker's PTY."""
    worker = orchestrator._workers.get(ticket_id)
    if not worker or not worker.is_running:
        raise HTTPException(404, "No running process for this ticket")
    text = req.get("input", "")
    if text:
        worker.write_input(text.encode())
    return {"status": "sent"}


@app.post("/api/tickets/{ticket_id}/open-terminal")
async def open_external_terminal(ticket_id: str):
    """Open the ticket's worktree in the user's external terminal app."""
    ticket = await state.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    cwd = ticket.worktree_path
    if not cwd or not Path(cwd).exists():
        if ticket.repository_id:
            repo = await state.get_repository(ticket.repository_id)
            if repo:
                cwd = repo.path
        if not cwd:
            run = await state.get_run(ticket.run_id)
            cwd = run.project_path if run else None
    if not cwd or not Path(cwd).exists():
        raise HTTPException(400, "No valid working directory for this ticket")

    # Get configured terminal command or use platform defaults
    terminal_cmd = await state.get_setting("external_terminal_command")

    import platform
    import subprocess

    try:
        if terminal_cmd:
            # User-configured command: replace {PATH} with actual path
            cmd = terminal_cmd.replace("{PATH}", cwd)
            subprocess.Popen(cmd, shell=True)
        elif platform.system() == "Darwin":
            # macOS: try iTerm2 first, fall back to Terminal.app
            try:
                subprocess.Popen([
                    "osascript", "-e",
                    f'tell application "iTerm2" to create window with default profile command "cd {cwd} && exec $SHELL"'
                ])
            except Exception:
                subprocess.Popen(["open", "-a", "Terminal", cwd])
        elif platform.system() == "Linux":
            # Try common terminal emulators
            for term in ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"]:
                try:
                    subprocess.Popen([term, f"--working-directory={cwd}"])
                    break
                except FileNotFoundError:
                    continue
        else:
            raise HTTPException(400, f"Unsupported platform: {platform.system()}")

        return {"status": "opened", "cwd": cwd}
    except Exception as e:
        raise HTTPException(500, f"Failed to open terminal: {e}")


# --- Entry point ---

if __name__ == "__main__":
    # Handle --regenerate-token before anything else
    if "--regenerate-token" in sys.argv:
        raw_token = generate_token()
        print(f"")
        print(f"  New auth token generated:")
        print(f"")
        print(f"    {raw_token}")
        print(f"")
        print(f"  Save this token — it is shown once and never stored on disk.")
        print(f"  The hash has been written to .env.")
        print(f"")
        sys.exit(0)

    # .env takes precedence, then defaults
    remote = get_env("TASK_NINJA_REMOTE_ACCESS", "false").lower() == "true"
    host = get_env("TASK_NINJA_HOST", "127.0.0.1")
    if remote:
        host = "0.0.0.0"
    port = int(get_env("TASK_NINJA_PORT", "8420"))
    if remote:
        print(f"[server] Remote access ENABLED — auth required", file=sys.stderr)
        print(f"[server] Token is hashed — use your saved token to log in", file=sys.stderr)
    print(f"[server] Starting at http://{host}:{port}", file=sys.stderr)
    uvicorn.run("server:app", host=host, port=port, reload=False)
