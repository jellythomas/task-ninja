#!/usr/bin/env python3
"""Autonomous Atlassian Task — FastAPI server + orchestrator."""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from engine.broadcaster import Broadcaster
from engine.orchestrator import Orchestrator
from engine.state import StateManager, init_db
from models.ticket import (
    AddTicketsRequest,
    CreateRunRequest,
    CreateScheduleRequest,
    LoadEpicRequest,
    MoveTicketRequest,
    RunStatus,
    TicketState,
    UpdateConfigRequest,
    UpdateRankRequest,
)

# Load config
CONFIG_PATH = Path(__file__).parent / "config.yaml"
config = yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}

# Shared instances
state = StateManager(config.get("database", {}).get("path", "autonomous_task.db"))
broadcaster = Broadcaster()
orchestrator = Orchestrator(state, broadcaster, config)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = config.get("database", {}).get("path", "autonomous_task.db")
    await init_db(db_path)
    print(f"[server] Database initialized at {db_path}", file=sys.stderr)
    yield


app = FastAPI(title="Autonomous Atlassian Task", lifespan=lifespan)


# --- Static UI ---

@app.get("/")
async def serve_ui():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# --- Runs ---

@app.post("/api/runs")
async def create_run(req: CreateRunRequest):
    run = await state.create_run(req.name, req.project_path, req.max_parallel)
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
    await orchestrator.pause()
    return {"status": "paused"}


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str):
    await orchestrator.resume()
    return {"status": "resumed"}


# --- Tickets ---

@app.post("/api/runs/{run_id}/load-epic")
async def load_epic(run_id: str, req: LoadEpicRequest):
    """Load tickets from a Jira epic. Adds them as Pending."""
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    await state.update_run_config(run_id, epic_key=req.epic_key)

    # TODO: Call mcp-atlassian-with-bitbucket to search epic children
    # For now, return a placeholder that the UI will handle
    return {
        "status": "epic_loaded",
        "epic_key": req.epic_key,
        "message": "Use the MCP tools to fetch tickets from Jira, then call /api/runs/{run_id}/add-tickets",
    }


@app.post("/api/runs/{run_id}/add-tickets")
async def add_tickets(run_id: str, req: AddTicketsRequest):
    """Add tickets by Jira keys. Goes directly to Queued."""
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    added = []
    for key in req.keys:
        key = key.strip().upper()
        if not key:
            continue
        existing = await state.get_ticket_by_jira_key(run_id, key)
        if existing:
            continue
        ticket = await state.add_ticket(run_id, key, state=TicketState.QUEUED)
        added.append(ticket.model_dump())
        await broadcaster.broadcast_ticket_update(run_id, ticket.id, TicketState.QUEUED)

    return {"added": len(added), "tickets": added}


@app.put("/api/tickets/{ticket_id}/state")
async def move_ticket(ticket_id: str, req: MoveTicketRequest):
    """Move a ticket to a new state (drag-and-drop)."""
    try:
        ticket = await state.update_ticket_state(ticket_id, req.state)
        await broadcaster.broadcast_ticket_update(ticket.run_id, ticket_id, req.state)
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
    return schedule.model_dump()


@app.get("/api/schedules")
async def list_schedules(run_id: str = None):
    schedules = await state.list_schedules(run_id)
    return [s.model_dump() for s in schedules]


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    await state.delete_schedule(schedule_id)
    return {"status": "deleted"}


# --- Entry point ---

if __name__ == "__main__":
    host = config.get("server", {}).get("host", "127.0.0.1")
    port = config.get("server", {}).get("port", 8420)
    print(f"[server] Starting at http://{host}:{port}", file=sys.stderr)
    uvicorn.run("server:app", host=host, port=port, reload=True)
