import asyncio
import contextlib

from unittest.mock import AsyncMock, MagicMock, patch

from engine.worker import (
    VIEWER_TERMINAL_ENDED_CLOSE_CODE,
    VIEWER_TERMINAL_ENDED_CLOSE_REASON,
    ViewerSession,
    Worker,
)


def make_worker() -> Worker:
    worker = Worker(
        ticket_id="ticket-1",
        run_id="run-1",
        jira_key="MC-1",
        worktree_path="/worktree",
        state_manager=MagicMock(),
        broadcaster=MagicMock(),
        claude_command="copilot",
        phases_config=[],
    )
    worker._master_fd = 42
    worker._send_to_pty = AsyncMock()
    return worker


def test_write_input_from_viewer_ignores_redraw_control_while_submit_guard_active() -> None:
    worker = make_worker()
    worker._phase_submit_in_progress = True
    ws = object()
    worker._viewer_sessions[ws] = ViewerSession(
        ws=ws,
        session_name="tn-ticket-1-v1",
        master_fd=99,
        pid=1234,
    )

    with patch("engine.worker.os.write") as write:
        worker.write_input_from_viewer(ws, b"\x0c")

    write.assert_not_called()


def test_write_input_ignores_redraw_control_while_submit_guard_active() -> None:
    worker = make_worker()
    worker._phase_submit_in_progress = True
    worker._use_tmux = False

    with patch("engine.worker.os.write") as write:
        worker.write_input(b"\x0c")

    write.assert_not_called()


def test_write_input_from_viewer_forwards_redraw_control_once_submit_guard_clears() -> None:
    worker = make_worker()
    worker._phase_submit_in_progress = False
    ws = object()
    worker._viewer_sessions[ws] = ViewerSession(
        ws=ws,
        session_name="tn-ticket-1-v1",
        master_fd=99,
        pid=1234,
    )

    with patch("engine.worker.os.write") as write:
        worker.write_input_from_viewer(ws, b"\x0c")

    write.assert_called_once_with(99, b"\x0c")


def test_submit_phase_prompt_clears_submit_guard_in_finally() -> None:
    worker = make_worker()
    worker._use_tmux = False

    async def fail_send(_data: bytes) -> None:
        assert worker._phase_submit_in_progress is True
        raise RuntimeError("boom")

    worker._send_to_pty = AsyncMock(side_effect=fail_send)

    with contextlib.suppress(RuntimeError):
        asyncio.get_event_loop().run_until_complete(worker._submit_phase_prompt("/planning-task MC-1"))

    assert worker._phase_submit_in_progress is False


def test_viewer_read_loop_closes_websocket_with_terminal_reason_on_pty_end() -> None:
    worker = make_worker()
    ws = AsyncMock()
    vs = ViewerSession(
        ws=ws,
        session_name="tn-ticket-1-v1",
        master_fd=99,
        pid=1234,
    )
    worker._viewer_sessions[ws] = vs
    loop = asyncio.get_event_loop()

    async def run_in_executor(_executor, func):
        return func()

    with (
        patch.object(loop, "run_in_executor", side_effect=run_in_executor),
        patch("engine.worker.select.select", side_effect=ValueError("bad fd")),
        patch("engine.worker.os.close"),
        patch("engine.worker.tmux_mgr.kill_session", new_callable=AsyncMock) as kill_session,
    ):
        asyncio.get_event_loop().run_until_complete(worker._viewer_read_loop(vs))

    ws.close.assert_awaited_once_with(
        code=VIEWER_TERMINAL_ENDED_CLOSE_CODE,
        reason=VIEWER_TERMINAL_ENDED_CLOSE_REASON,
    )
    ws.send_text.assert_not_awaited()
    kill_session.assert_awaited_once_with("tn-ticket-1-v1")
    assert ws not in worker._viewer_sessions
