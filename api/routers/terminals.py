"""Terminal routes — WebSocket attach, PTY input, external terminal launch."""

from __future__ import annotations

import asyncio
import json as _json
import logging
import platform
import subprocess

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from api.dependencies import get_orchestrator, get_state
from engine.auth import verify_ws_token
from engine.broadcaster import Broadcaster
from engine.orchestrator import Orchestrator
from engine.state import StateManager
from models.ticket import TicketState

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminals"])


async def _accept_websocket_once(websocket: WebSocket, accepted: bool) -> bool:
    if accepted:
        return True
    await websocket.accept()
    return True


async def _send_terminal_startup_failure(websocket: WebSocket, message: str) -> None:
    detail = message.strip() or "Terminal startup failed"
    await websocket.send_text(_json.dumps({"type": "startup_error", "message": detail}))
    await websocket.close(code=4005, reason="Terminal startup failed")


@router.websocket("/ws/terminal/{ticket_id}")
async def terminal_ws(
    websocket: WebSocket,
    ticket_id: str,
):
    """WebSocket endpoint to attach to a running worker's live terminal.

    Attach/detach model: closing the terminal does NOT kill the worker process.
    """
    # Resolve dependencies manually — WebSocket endpoints don't inject Request
    state: StateManager = websocket.app.state.state
    orchestrator: Orchestrator = websocket.app.state.orchestrator
    broadcaster: Broadcaster = websocket.app.state.broadcaster

    if not verify_ws_token(websocket):
        await websocket.close(code=4001, reason="Authentication required")
        return

    allow_adhoc = websocket.query_params.get("allow_adhoc", "1").lower() not in {"0", "false", "no"}
    worker = orchestrator._workers.get(ticket_id)

    if not worker:
        for _ in range(10):
            await asyncio.sleep(0.5)
            worker = orchestrator._workers.get(ticket_id)
            if worker:
                break

    if worker and not worker.is_running:
        for _ in range(20):
            await asyncio.sleep(0.5)
            # Re-fetch: orchestrator may have replaced the dead worker
            # with a freshly spawned one during our wait.
            worker = orchestrator._workers.get(ticket_id) or worker
            if worker.is_running:
                break

    # If still no live worker, try to respawn or attach ad-hoc.
    adhoc = None
    websocket_accepted = False
    if not worker or not worker.is_running:
        existing_adhoc = orchestrator._adhoc_terminals.get(ticket_id)
        existing_alive = False
        if existing_adhoc:
            if hasattr(existing_adhoc, "async_is_running"):
                existing_alive = await existing_adhoc.async_is_running()
            else:
                existing_alive = existing_adhoc.is_running
        if existing_adhoc and existing_alive:
            worker = existing_adhoc
            adhoc = existing_adhoc
        else:
            if existing_adhoc:
                orchestrator._adhoc_terminals.pop(ticket_id, None)
            ticket = await state.get_ticket(ticket_id)
            terminal_states = (TicketState.REVIEW, TicketState.DONE, TicketState.FAILED)
            allow_terminal_adhoc = bool(ticket and ticket.state in terminal_states)

            # For non-terminal tickets with no worker, respawn directly.
            # This handles: requeued tickets, failed prompt submissions,
            # and any state where the worker died unexpectedly.
            if ticket and ticket.state not in (*terminal_states, TicketState.TODO) and orchestrator._run_id:
                run = await state.get_run(orchestrator._run_id)
                if run:
                    # Reset to QUEUED with fresh retry budget
                    await state.update_ticket(
                        ticket_id,
                        error=None,
                        prompt_submit_requeues=0,
                    )
                    await state.update_ticket_state(ticket_id, TicketState.QUEUED)
                    # Kill any stale worker/task references
                    orchestrator._tasks.pop(ticket_id, None)
                    orchestrator._workers.pop(ticket_id, None)
                    orchestrator._spawning.discard(ticket_id)
                    # Spawn directly
                    logger.info("Respawning worker for %s (no active worker found)", ticket.jira_key)
                    await orchestrator._spawn_worker(ticket_id, ticket.jira_key, run)
                    # Wait for the new worker to come up
                    for _ in range(60):  # up to 30s for startup
                        await asyncio.sleep(0.5)
                        worker = orchestrator._workers.get(ticket_id)
                        if worker and worker.is_running:
                            break

            if ticket and ticket.state in terminal_states:
                websocket_accepted = await _accept_websocket_once(websocket, websocket_accepted)

            # Ad-hoc terminal for terminal-state tickets
            if (
                (not worker or not worker.is_running)
                and ticket
                and (allow_adhoc or allow_terminal_adhoc)
                and ticket.worktree_path
                and Path(ticket.worktree_path).is_dir()
                and ticket.state in terminal_states
            ):
                from engine.worker import AdHocTerminal

                try:
                    adhoc_command = "claude --dangerously-skip-permissions"
                    if ticket.profile_id:
                        profile = await state.get_agent_profile(ticket.profile_id)
                        if profile and profile.command:
                            args = (profile.args_template or "").strip()
                            adhoc_command = f"{profile.command} {args}".strip()
                    adhoc = AdHocTerminal(worktree_path=ticket.worktree_path, claude_command=adhoc_command)
                    await adhoc.start()
                    orchestrator._adhoc_terminals[ticket_id] = adhoc
                    worker = adhoc
                    adhoc_pid = getattr(adhoc, "_tmux_pid", None) or (adhoc.process.pid if adhoc.process else None)
                    if adhoc_pid:
                        await state.update_ticket(ticket_id, worker_pid=adhoc_pid)
                        await broadcaster.broadcast_ticket_update(
                            orchestrator._run_id,
                            ticket_id,
                            ticket.state,
                            worker_pid=adhoc_pid,
                        )
                except Exception as e:
                    logger.error("Failed to spawn ad-hoc terminal for %s: %s", ticket_id, e)
                    websocket_accepted = await _accept_websocket_once(websocket, websocket_accepted)
                    await _send_terminal_startup_failure(websocket, str(e))
                    return

            if not worker or not worker.is_running:
                websocket_accepted = await _accept_websocket_once(websocket, websocket_accepted)
                await websocket.close(code=4004, reason="No running process for this ticket")
                return

    websocket_accepted = await _accept_websocket_once(websocket, websocket_accepted)

    # In tmux mode, wait for the first resize message before attaching.
    # This ensures the grouped session is created at the viewer's actual
    # dimensions (e.g. 45 cols on phone vs 200 cols on desktop), preventing
    # garbled initial renders from size mismatch.
    use_tmux = getattr(worker, "_use_tmux", False)
    initial_rows, initial_cols = 24, 80
    if use_tmux:
        try:
            # Wait up to 2s for the first resize message from the frontend
            first_msg = await asyncio.wait_for(websocket.receive(), timeout=2.0)
            if first_msg.get("text"):
                try:
                    ctrl = _json.loads(first_msg["text"])
                    if ctrl.get("type") == "resize":
                        initial_rows = ctrl.get("rows", 24)
                        initial_cols = ctrl.get("cols", 80)
                except (_json.JSONDecodeError, KeyError):
                    pass
        except (asyncio.TimeoutError, WebSocketDisconnect):
            pass

    await worker.attach_viewer(websocket, rows=initial_rows, cols=initial_cols)

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.receive":
                if message.get("bytes"):
                    # Use per-viewer input in tmux mode for lower latency
                    if hasattr(worker, "write_input_from_viewer"):
                        worker.write_input_from_viewer(websocket, message["bytes"])
                    else:
                        worker.write_input(message["bytes"])
                elif message.get("text"):
                    try:
                        ctrl = _json.loads(message["text"])
                        if ctrl.get("type") == "resize":
                            # Per-viewer resize in tmux mode, shared resize in raw PTY mode
                            if hasattr(worker, "resize_viewer_pty"):
                                worker.resize_viewer_pty(websocket, ctrl.get("rows", 24), ctrl.get("cols", 80))
                            else:
                                worker.resize_pty(ctrl.get("rows", 24), ctrl.get("cols", 80))
                        elif ctrl.get("type") == "scroll_bottom":
                            if hasattr(worker, "scroll_viewer_to_bottom"):
                                await worker.scroll_viewer_to_bottom(websocket)
                        elif ctrl.get("type") == "redraw":
                            if hasattr(worker, "refresh_viewer"):
                                await worker.refresh_viewer(websocket)
                        elif ctrl.get("type") == "ping":
                            await websocket.send_text(_json.dumps({"type": "pong"}))
                    except (_json.JSONDecodeError, KeyError):
                        if hasattr(worker, "write_input_from_viewer"):
                            worker.write_input_from_viewer(websocket, message["text"].encode())
                        else:
                            worker.write_input(message["text"].encode())
            elif message["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    except (RuntimeError, OSError):
        pass
    finally:
        worker.detach_viewer(websocket)
        if adhoc and not adhoc._viewers and not adhoc.is_running:
            orchestrator._adhoc_terminals.pop(ticket_id, None)
            await state.update_ticket(ticket_id, worker_pid=None)
            ticket = await state.get_ticket(ticket_id)
            if ticket:
                await broadcaster.broadcast_ticket_update(
                    orchestrator._run_id, ticket_id, ticket.state, worker_pid=None
                )


@router.post("/api/tickets/{ticket_id}/terminal-input")
async def terminal_input(
    ticket_id: str,
    req: dict,
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    """Send input text to a running worker's PTY."""
    worker = orchestrator._workers.get(ticket_id)
    if not worker or not worker.is_running:
        raise HTTPException(404, "No running process for this ticket")
    text = req.get("input", "")
    if text:
        worker.write_input(text.encode())
    return {"status": "sent"}


@router.post("/api/tickets/{ticket_id}/open-terminal")
async def open_external_terminal(
    ticket_id: str,
    state: StateManager = Depends(get_state),
):
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

    terminal_cmd = await state.get_setting("external_terminal_command")

    try:
        if terminal_cmd:
            cmd = terminal_cmd.replace("{PATH}", cwd)
            subprocess.Popen(cmd, shell=True)  # noqa: S602 — user-configured command
        elif platform.system() == "Darwin":
            try:
                subprocess.Popen(
                    [
                        "osascript",
                        "-e",
                        f'tell application "iTerm2" to create window with default profile command "cd {cwd} && exec $SHELL"',
                    ]
                )
            except (OSError, FileNotFoundError):
                subprocess.Popen(["open", "-a", "Terminal", cwd])
        elif platform.system() == "Linux":
            for term in ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"]:
                try:
                    subprocess.Popen([term, f"--working-directory={cwd}"])
                    break
                except FileNotFoundError:
                    continue
        else:
            raise HTTPException(400, f"Unsupported platform: {platform.system()}")

        return {"status": "opened", "cwd": cwd}
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(500, f"Failed to open terminal: {e}") from e
