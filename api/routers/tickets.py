"""Ticket management routes — CRUD, state transitions, Jira sync, schedules."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import (
    get_broadcaster,
    get_claude_helper,
    get_config,
    get_jira_client,
    get_orchestrator,
    get_run_scheduler,
    get_state,
)
from engine.broadcaster import Broadcaster
from engine.claude_helper import ClaudeHelper
from engine.git_manager import GitManager
from engine.jira_client import JiraClient
from engine.orchestrator import Orchestrator
from engine.scheduler import RunScheduler
from engine.state import StateManager
from models.ticket import (
    AddTicketsRequest,
    CreateScheduleRequest,
    FetchTicketsRequest,
    LoadEpicRequest,
    MoveTicketRequest,
    ResolveInputRequest,
    TicketState,
    UpdateRankRequest,
    UpdateScheduleRequest,
    UpdateTicketAssignmentRequest,
)

router = APIRouter(tags=["tickets"])
logger = logging.getLogger(__name__)

# Module-level set keeps fire-and-forget task refs alive until completion (RUF006)
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:  # type: ignore[type-arg]
    """Schedule a coroutine as a background task, keeping a strong ref to prevent GC."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# --- Epic / Ticket Fetching ---


@router.post("/api/runs/{run_id}/load-epic")
async def load_epic(
    run_id: str,
    req: LoadEpicRequest,
    state: StateManager = Depends(get_state),
    jira_client: JiraClient = Depends(get_jira_client),
    claude_helper: ClaudeHelper = Depends(get_claude_helper),
):
    """Load tickets from a Jira epic. Uses direct API if configured, falls back to Claude CLI."""
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    epic_key = req.epic_key.strip()
    m = re.search(r"/browse/([A-Z][A-Z0-9]+-\d+)", epic_key, re.IGNORECASE)
    epic_key = m.group(1).upper() if m else epic_key.upper()

    await state.update_run_config(run_id, epic_key=epic_key)

    if await jira_client.is_configured():
        children = await jira_client.fetch_epic_children(epic_key)
    else:
        children = await claude_helper.fetch_epic_children(epic_key)

    repos = await state.list_repositories()
    repo_map = {r.id: r for r in repos}
    label_to_repo = {r.jira_label.lower(): r.id for r in repos if r.jira_label}

    tickets = []
    for child in children:
        key = child.get("key", "").strip().upper()
        if not key:
            continue
        existing = await state.get_ticket_by_jira_key(run_id, key)

        matched_repo_id = None
        key_prefix = key.split("-")[0].lower() if "-" in key else ""
        if key_prefix and key_prefix in label_to_repo:
            matched_repo_id = label_to_repo[key_prefix]
        else:
            child_labels = [lbl.lower() for lbl in child.get("labels", [])]
            child_components = [c.lower() for c in child.get("components", [])]
            all_tags = child_labels + child_components
            for tag in all_tags:
                for label_key, repo_id in label_to_repo.items():
                    if label_key in tag or tag in label_key:
                        matched_repo_id = repo_id
                        break
                if matched_repo_id:
                    break

        blocked_by = child.get("blocked_by", [])
        predicted_files = child.get("predicted_files", [])
        ticket_data = {
            "jira_key": key,
            "summary": child.get("summary"),
            "status": child.get("status", "To Do"),
            "already_added": existing is not None,
            "labels": child.get("labels", []),
            "components": child.get("components", []),
            "blocked_by": blocked_by,
            "predicted_files": predicted_files,
        }
        if matched_repo_id:
            repo = repo_map.get(matched_repo_id)
            ticket_data["matched_repository_id"] = matched_repo_id
            ticket_data["matched_repository_name"] = repo.name if repo else None

        # Persist blocked_by_keys and predicted_files on existing tickets if data has changed
        if existing and blocked_by:
            await state.update_ticket(existing.id, blocked_by_keys=json.dumps(blocked_by))
        if existing and predicted_files:
            await state.update_ticket(existing.id, predicted_files=json.dumps(predicted_files))

        tickets.append(ticket_data)

    return {
        "status": "epic_loaded",
        "epic_key": epic_key,
        "found": len(tickets),
        "tickets": tickets,
    }


@router.post("/api/runs/{run_id}/fetch-tickets")
async def fetch_tickets(
    run_id: str,
    req: FetchTicketsRequest,
    state: StateManager = Depends(get_state),
    jira_client: JiraClient = Depends(get_jira_client),
):
    """Fetch ticket details from Jira for the selection modal."""
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    parsed_keys = []
    for raw in req.keys:
        raw = raw.strip()
        if not raw:
            continue
        m = re.search(r"/browse/([A-Z][A-Z0-9]+-\d+)", raw, re.IGNORECASE)
        parsed_keys.append(m.group(1).upper() if m else raw.upper())

    repos = await state.list_repositories()
    label_to_repo = {r.jira_label.lower(): r.id for r in repos if r.jira_label}
    repo_map = {r.id: r for r in repos}

    tickets = []
    for key in parsed_keys:
        existing = await state.get_ticket_by_jira_key(run_id, key)
        issue = None
        if await jira_client.is_configured():
            issue = await jira_client.get_issue(key)

        matched_repo_id = None
        key_prefix = key.split("-")[0].lower() if "-" in key else ""
        if key_prefix and key_prefix in label_to_repo:
            matched_repo_id = label_to_repo[key_prefix]

        blocked_by = issue.get("blocked_by", []) if issue else []
        ticket_data = {
            "jira_key": key,
            "summary": issue.get("summary", "") if issue else None,
            "status": issue.get("status", "To Do") if issue else "Unknown",
            "already_added": existing is not None,
            "labels": issue.get("labels", []) if issue else [],
            "components": issue.get("components", []) if issue else [],
            "blocked_by": blocked_by,
        }
        if matched_repo_id:
            repo = repo_map.get(matched_repo_id)
            ticket_data["matched_repository_id"] = matched_repo_id
            ticket_data["matched_repository_name"] = repo.name if repo else None

        # Persist blocked_by_keys on existing tickets if data has changed
        if existing and blocked_by:
            await state.update_ticket(existing.id, blocked_by_keys=json.dumps(blocked_by))

        tickets.append(ticket_data)

    return {"status": "tickets_fetched", "found": len(tickets), "tickets": tickets}


@router.post("/api/runs/{run_id}/add-tickets")
async def add_tickets(
    run_id: str,
    req: AddTicketsRequest,
    state: StateManager = Depends(get_state),
    broadcaster: Broadcaster = Depends(get_broadcaster),
):
    """Add tickets by Jira keys. Goes directly to Queued."""
    run = await state.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    added = []
    summaries = req.summaries or {}
    blocked_by_map = req.blocked_by_keys or {}
    predicted_files_map = req.predicted_files or {}
    for raw_key in req.keys:
        raw_key = raw_key.strip()
        if not raw_key:
            continue
        m = re.search(r"/browse/([A-Z][A-Z0-9]+-\d+)", raw_key, re.IGNORECASE)
        key = m.group(1).upper() if m else raw_key.upper()
        existing = await state.get_ticket_by_jira_key(run_id, key)
        if existing:
            continue
        summary = summaries.get(key)
        ticket = await state.add_ticket(run_id, key, summary=summary, state=TicketState.QUEUED)
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
        blockers = blocked_by_map.get(key, [])
        if blockers:
            assignment["blocked_by_keys"] = json.dumps(blockers)
        pfiles = predicted_files_map.get(key, [])
        if pfiles:
            assignment["predicted_files"] = json.dumps(pfiles)
        if assignment:
            await state.update_ticket(ticket.id, **assignment)
            ticket = await state.get_ticket(ticket.id)
        added.append(ticket.model_dump())
        await broadcaster.broadcast_ticket_update(run_id, ticket.id, TicketState.QUEUED)

    return {"added": len(added), "tickets": added}


# --- Ticket Actions ---


@router.put("/api/tickets/{ticket_id}/state")
async def move_ticket(
    ticket_id: str,
    req: MoveTicketRequest,
    state: StateManager = Depends(get_state),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    broadcaster: Broadcaster = Depends(get_broadcaster),
    jira_client: JiraClient = Depends(get_jira_client),
    claude_helper: ClaudeHelper = Depends(get_claude_helper),
    config: dict = Depends(get_config),
):
    """Move a ticket to a new state. Kills worker if moving to non-active state."""
    try:
        logger.info("move_ticket %s -> %s", ticket_id, req.state.value)

        if req.state not in {TicketState.PLANNING, TicketState.DEVELOPING}:
            try:
                killed = await orchestrator.kill_worker(ticket_id)
                logger.debug("kill_worker=%s", killed)
                if killed:
                    await asyncio.sleep(0.3)
            except (RuntimeError, ValueError) as e:
                logger.warning("kill_worker error (ignoring): %s", e)

        if req.state == TicketState.TODO:
            await state.update_ticket(ticket_id, last_completed_phase=None, error=None)

        ticket = await state.update_ticket_state(ticket_id, req.state)
        logger.debug("DB updated, state now=%s", ticket.state)

        try:
            await broadcaster.broadcast_ticket_update(ticket.run_id, ticket_id, req.state)
        except (RuntimeError, OSError) as e:
            logger.warning("broadcast error (ignoring): %s", e)

        if req.state == TicketState.QUEUED and not orchestrator._running and ticket.run_id:
            await orchestrator.resume(ticket.run_id)

        if req.state in {TicketState.DONE, TicketState.REVIEW} and ticket.worktree_path:
            cleanup_enabled = config.get("git", {}).get("cleanup_worktrees", True)
            if cleanup_enabled:
                try:
                    run = await state.get_run(ticket.run_id)
                    git = GitManager(
                        run.project_path,
                        config.get("git", {}).get("worktree_dir", ".worktrees"),
                    )
                    await git.cleanup_worktree(ticket.worktree_path)
                    logger.info("Cleaned up worktree for %s", ticket_id)
                except (RuntimeError, OSError) as e:
                    logger.warning("Failed to cleanup worktree for %s: %s", ticket_id, e)

        mcp_cfg = config.get("mcp", {})
        jira_mapping = mcp_cfg.get("jira_status_mapping", {})
        target_status = jira_mapping.get(req.state.value)
        if target_status:
            if await jira_client.is_configured():
                _fire_and_forget(jira_client.transition_issue(ticket.jira_key, target_status))
            else:
                _fire_and_forget(claude_helper.transition_jira_issue(ticket.jira_key, target_status))

        return ticket.model_dump()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        logger.exception("Unexpected error in move_ticket: %s", e)
        raise HTTPException(500, str(e)) from e


@router.put("/api/tickets/{ticket_id}/rank")
async def update_rank(
    ticket_id: str,
    req: UpdateRankRequest,
    state: StateManager = Depends(get_state),
):
    await state.update_ticket_rank(ticket_id, req.rank)
    return {"status": "updated"}


@router.post("/api/tickets/{ticket_id}/pause")
async def pause_ticket(
    ticket_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    await orchestrator.pause_ticket(ticket_id)
    return {"status": "paused"}


@router.post("/api/tickets/{ticket_id}/resume")
async def resume_ticket(
    ticket_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    await orchestrator.resume_ticket(ticket_id)
    return {"status": "resumed"}


@router.post("/api/tickets/{ticket_id}/interrupt")
async def interrupt_ticket(
    ticket_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    """Send Escape to the worker's PTY to interrupt current operation."""
    sent = orchestrator.interrupt_worker(ticket_id)
    return {"status": "interrupted" if sent else "no_worker"}


@router.post("/api/tickets/{ticket_id}/resolve-input")
async def resolve_ticket_input(
    ticket_id: str,
    req: ResolveInputRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    """Resolve an AWAITING_INPUT ticket (e.g., branch mismatch)."""
    try:
        result = await orchestrator.resolve_input(ticket_id, req.choice)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/api/tickets/{ticket_id}/retry")
async def retry_ticket(
    ticket_id: str,
    clean: bool = False,
    state: StateManager = Depends(get_state),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    broadcaster: Broadcaster = Depends(get_broadcaster),
    config: dict = Depends(get_config),
):
    """Retry a failed/done ticket. clean=true destroys worktree for fresh start."""
    ticket = await state.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    await orchestrator.kill_worker(ticket_id)

    if clean and ticket.worktree_path:
        with contextlib.suppress(Exception):
            run = await state.get_run(ticket.run_id)
            git_cfg = config.get("git", {})
            git = GitManager(
                run.project_path if run else ".",
                git_cfg.get("worktree_dir", ".worktrees"),
            )
            await git._remove_worktree(Path(ticket.worktree_path))

    await state.update_ticket(ticket_id, error=None, worker_pid=None, paused=False)
    await state.update_ticket_state(ticket_id, TicketState.QUEUED)
    await broadcaster.broadcast_ticket_update(ticket.run_id, ticket_id, TicketState.QUEUED)

    if not orchestrator._running and ticket.run_id:
        await orchestrator.resume(ticket.run_id)

    return {"status": "retrying", "clean": clean}


@router.delete("/api/tickets/{ticket_id}")
async def delete_ticket(
    ticket_id: str,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    await orchestrator.delete_ticket(ticket_id)
    return {"status": "deleted"}


@router.put("/api/tickets/{ticket_id}/assignment")
async def update_ticket_assignment(
    ticket_id: str,
    req: UpdateTicketAssignmentRequest,
    state: StateManager = Depends(get_state),
):
    raw = req.model_dump()
    updates = {k: v for k, v in raw.items() if v is not None}
    if "profile_id" in (req.model_fields_set or set()) and req.profile_id is None:
        updates["profile_id"] = None
    if not updates:
        raise HTTPException(400, "No fields to update")
    await state.update_ticket(ticket_id, **updates)
    ticket = await state.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    return ticket.model_dump()


# --- Logs ---


@router.get("/api/logs/{ticket_id}")
async def get_logs(
    ticket_id: str,
    tail: int = 200,
    state: StateManager = Depends(get_state),
):
    logs = await state.get_logs(ticket_id, tail)
    return {"logs": logs}


# --- Schedules ---


@router.post("/api/schedules")
async def create_schedule(
    req: CreateScheduleRequest,
    state: StateManager = Depends(get_state),
    run_scheduler: RunScheduler = Depends(get_run_scheduler),
):
    schedule = await state.create_schedule(
        req.run_id,
        req.schedule_type,
        cron_expression=req.cron_expression,
        start_time=req.start_time.isoformat() if req.start_time else None,
        end_time=req.end_time.isoformat() if req.end_time else None,
    )
    await run_scheduler.add_schedule(
        schedule.id,
        req.run_id,
        req.schedule_type,
        cron_expression=req.cron_expression,
        start_time=req.start_time.isoformat() if req.start_time else None,
    )
    return schedule.model_dump()


@router.get("/api/schedules")
async def list_schedules(
    run_id: str | None = None,
    state: StateManager = Depends(get_state),
):
    schedules = await state.list_schedules(run_id)
    return [s.model_dump() for s in schedules]


@router.patch("/api/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    req: UpdateScheduleRequest,
    state: StateManager = Depends(get_state),
    run_scheduler: RunScheduler = Depends(get_run_scheduler),
):
    schedule = await state.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    updates = req.model_dump(exclude_none=True)
    if updates.get("end_time"):
        updates["end_time"] = updates["end_time"].isoformat()
    updated = await state.update_schedule(schedule_id, **updates)
    if req.enabled is False:
        run_scheduler.remove_schedule(schedule_id)
    elif req.enabled is True or req.cron_expression:
        run_scheduler.remove_schedule(schedule_id)
        if updated.enabled:
            await run_scheduler.add_schedule(
                updated.id,
                updated.run_id,
                updated.schedule_type,
                cron_expression=updated.cron_expression,
                start_time=updated.start_time.isoformat() if updated.start_time else None,
            )
    return updated.model_dump()


@router.delete("/api/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    state: StateManager = Depends(get_state),
    run_scheduler: RunScheduler = Depends(get_run_scheduler),
):
    run_scheduler.remove_schedule(schedule_id)
    await state.delete_schedule(schedule_id)
    return {"status": "deleted"}
