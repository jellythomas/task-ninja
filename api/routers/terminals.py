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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminals"])


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
            if worker.is_running:
                break

    adhoc = None
    if not worker or not worker.is_running:
        existing_adhoc = orchestrator._adhoc_terminals.get(ticket_id)
        if existing_adhoc and existing_adhoc.is_running:
            worker = existing_adhoc
            adhoc = existing_adhoc
        else:
            if existing_adhoc:
                orchestrator._adhoc_terminals.pop(ticket_id, None)
            ticket = await state.get_ticket(ticket_id)
            if (
                ticket
                and ticket.worktree_path
                and Path(ticket.worktree_path).is_dir()
                and ticket.state in ("review", "done", "failed")
            ):
                from engine.worker import AdHocTerminal

                try:
                    adhoc_command = "claude"
                    if ticket.profile_id:
                        profile = await state.get_agent_profile(ticket.profile_id)
                        if profile and profile.command:
                            adhoc_command = profile.command
                    adhoc = AdHocTerminal(worktree_path=ticket.worktree_path, claude_command=adhoc_command)
                    await adhoc.start()
                    orchestrator._adhoc_terminals[ticket_id] = adhoc
                    worker = adhoc
                    if adhoc.process and adhoc.process.pid:
                        await state.update_ticket(ticket_id, worker_pid=adhoc.process.pid)
                        await broadcaster.broadcast_ticket_update(
                            orchestrator._run_id,
                            ticket_id,
                            ticket.state,
                            worker_pid=adhoc.process.pid,
                        )
                except (OSError, RuntimeError) as e:
                    logger.error("Failed to spawn ad-hoc terminal for %s: %s", ticket_id, e)
                    await websocket.accept()
                    reason = str(e)[:120]
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
                if message.get("bytes"):
                    worker.write_input(message["bytes"])
                elif message.get("text"):
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
