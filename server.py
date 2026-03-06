#!/usr/bin/env python3
"""Autonomous Atlassian Task — FastAPI server + orchestrator."""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from engine.broadcaster import Broadcaster
from engine.claude_helper import ClaudeHelper
from engine.jira_client import JiraClient
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
    LoadEpicRequest,
    MoveTicketRequest,
    RunStatus,
    TicketState,
    UpdateAgentProfileRequest,
    UpdateConfigRequest,
    UpdateRankRequest,
    UpdateRepositoryRequest,
    UpdateSettingsRequest,
    UpdateTicketAssignmentRequest,
)

# Load config
CONFIG_PATH = Path(__file__).parent / "config.yaml"
config = yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}

# Shared instances
state = StateManager(config.get("database", {}).get("path", "autonomous_task.db"))
broadcaster = Broadcaster()
orchestrator = Orchestrator(state, broadcaster, config)
claude_cfg = config.get("claude", {})
claude_helper = ClaudeHelper(claude_cfg.get("command", "claude"), claude_cfg.get("skip_permissions", True))
jira_client = JiraClient(state)
terminal_manager = TerminalManager()
run_scheduler = RunScheduler(state, orchestrator.start)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = config.get("database", {}).get("path", "autonomous_task.db")
    await init_db(db_path)
    print(f"[server] Database initialized at {db_path}", file=sys.stderr)

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
    terminal_manager.close_all()


app = FastAPI(title="Autonomous Atlassian Task", lifespan=lifespan)


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
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    await state.update_run_config(run_id, epic_key=req.epic_key)

    # Try direct Jira API first, fall back to Claude CLI + MCP
    if await jira_client.is_configured():
        children = await jira_client.fetch_epic_children(req.epic_key)
    else:
        children = await claude_helper.fetch_epic_children(req.epic_key)

    # Load label mappings for auto-assigning repos
    label_mappings = await state.list_label_mappings()
    repos = await state.list_repositories()
    repo_map = {r.id: r for r in repos}

    # Build label -> repo_id lookup
    label_to_repo = {}
    for m in label_mappings:
        label_to_repo[m.jira_label.lower()] = m.repository_id

    # Return ticket data without persisting — user selects in modal, then add-tickets creates them
    tickets = []
    for child in children:
        key = child.get("key", "").strip().upper()
        if not key:
            continue
        # Check if already on the board
        existing = await state.get_ticket_by_jira_key(run_id, key)

        # Auto-detect repository from labels/components
        matched_repo_id = None
        child_labels = [l.lower() for l in child.get("labels", [])]
        child_components = [c.lower() for c in child.get("components", [])]
        all_tags = child_labels + child_components
        for tag in all_tags:
            # Match against registered labels (e.g., "[mc]" matches "[MC]")
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
        "epic_key": req.epic_key,
        "found": len(tickets),
        "tickets": tickets,
    }


@app.post("/api/runs/{run_id}/add-tickets")
async def add_tickets(run_id: str, req: AddTicketsRequest):
    """Add tickets by Jira keys. Goes directly to Queued."""
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    added = []
    summaries = req.summaries or {}
    for key in req.keys:
        key = key.strip().upper()
        if not key:
            continue
        existing = await state.get_ticket_by_jira_key(run_id, key)
        if existing:
            continue
        summary = summaries.get(key)
        ticket = await state.add_ticket(run_id, key, summary=summary, state=TicketState.QUEUED)
        # Apply optional assignment fields
        assignment = {}
        if req.repository_id:
            assignment["repository_id"] = req.repository_id
        if req.parent_branch:
            assignment["parent_branch"] = req.parent_branch
        if req.profile_id:
            assignment["profile_id"] = req.profile_id
        if assignment:
            await state.update_ticket(ticket.id, **assignment)
            ticket = await state.get_ticket(ticket.id)
        added.append(ticket.model_dump())
        await broadcaster.broadcast_ticket_update(run_id, ticket.id, TicketState.QUEUED)

    return {"added": len(added), "tickets": added}


@app.put("/api/tickets/{ticket_id}/state")
async def move_ticket(ticket_id: str, req: MoveTicketRequest):
    """Move a ticket to a new state (drag-and-drop). Syncs to Jira for terminal states."""
    try:
        ticket = await state.update_ticket_state(ticket_id, req.state)
        await broadcaster.broadcast_ticket_update(ticket.run_id, ticket_id, req.state)

        # Sync to Jira for manually-triggered state changes
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
    repo = await state.create_repository(req.name, req.path, req.default_branch, req.default_profile_id)
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
    profile = await state.create_agent_profile(req.name, req.command, req.args_template, req.log_format)
    return profile.model_dump()


@app.put("/api/profiles/{profile_id}")
async def update_agent_profile(profile_id: int, req: UpdateAgentProfileRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
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


@app.get("/api/settings/jira-status")
async def jira_status():
    """Check if Jira credentials are configured."""
    configured = await jira_client.is_configured()
    return {"configured": configured}


@app.post("/api/settings/test-jira")
async def test_jira_connection():
    """Test Jira API connection with stored credentials."""
    jira_url = await state.get_setting("jira_url")
    jira_email = await state.get_setting("jira_email")
    jira_token = await state.get_setting("jira_token")
    if not all([jira_url, jira_email, jira_token]):
        raise HTTPException(400, "Jira credentials not configured")
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

    # Find the live worker for this ticket
    worker = orchestrator._workers.get(ticket_id)
    if not worker or not worker.is_running:
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
    host = config.get("server", {}).get("host", "127.0.0.1")
    port = config.get("server", {}).get("port", 8420)
    print(f"[server] Starting at http://{host}:{port}", file=sys.stderr)
    uvicorn.run("server:app", host=host, port=port, reload=False)
