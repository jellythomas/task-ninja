"""Run management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_orchestrator, get_state
from engine.orchestrator import Orchestrator
from engine.state import StateManager
from models.ticket import (
    CreateRunRequest,
    TicketState,
    UpdateConfigRequest,
)

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("")
async def create_run(
    req: CreateRunRequest,
    state: StateManager = Depends(get_state),
):
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


@router.get("")
async def list_runs(state: StateManager = Depends(get_state)):
    runs = await state.list_runs()
    return [r.model_dump() for r in runs]


@router.get("/{run_id}")
async def get_run(run_id: str, state: StateManager = Depends(get_state)):
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    tickets = await state.get_tickets_for_run(run_id)
    return {
        **run.model_dump(),
        "tickets": [t.model_dump() for t in tickets],
    }


@router.delete("/{run_id}")
async def delete_run(
    run_id: str,
    state: StateManager = Depends(get_state),
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    tickets = await state.get_tickets_for_run(run_id)
    for t in tickets:
        if t.state in {TicketState.PLANNING, TicketState.DEVELOPING}:
            await orchestrator.delete_ticket(t.id)
    await state.delete_run(run_id)
    return {"status": "deleted"}


@router.put("/{run_id}/config")
async def update_run_config(
    run_id: str,
    req: UpdateConfigRequest,
    state: StateManager = Depends(get_state),
):
    updates = {}
    if req.max_parallel is not None:
        updates["max_parallel"] = req.max_parallel
    if updates:
        await state.update_run_config(run_id, **updates)
    return {"status": "updated"}


@router.post("/{run_id}/start")
async def start_run(
    run_id: str,
    state: StateManager = Depends(get_state),
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await orchestrator.start(run_id)
    return {"status": "started"}


@router.post("/{run_id}/pause")
async def pause_run(
    run_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    await orchestrator.pause(run_id)
    return {"status": "paused"}


@router.post("/{run_id}/resume")
async def resume_run(
    run_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    await orchestrator.resume(run_id)
    return {"status": "resumed"}


@router.get("/{run_id}/queue-estimates")
async def get_queue_estimates(
    run_id: str,
    state: StateManager = Depends(get_state),
):
    """Return estimated wait time in seconds for each queued ticket in this run.

    Formula: wait_estimate_seconds = queue_position * avg_duration / max_parallel
    """
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    queued = await state.get_tickets_by_state(run_id, TicketState.QUEUED)
    avg_duration = await state.get_avg_ticket_duration(run_id)

    estimates: dict[str, float | None] = {}
    for position, ticket in enumerate(queued, start=1):
        if avg_duration is not None and run.max_parallel > 0:
            estimates[ticket.id] = round(position * avg_duration / run.max_parallel, 1)
        else:
            estimates[ticket.id] = None

    return {"estimates": estimates}
