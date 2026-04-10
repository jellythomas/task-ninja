import asyncio
import contextlib
import tempfile

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from api.routers.terminals import terminal_ws
from engine.worker import (
    VIEWER_TERMINAL_ENDED_CLOSE_CODE,
    VIEWER_TERMINAL_ENDED_CLOSE_REASON,
    ViewerSession,
    Worker,
)
from models.ticket import Ticket, TicketState


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


class FakeWebSocket:
    def __init__(self, *, query_params: dict[str, str] | None = None, messages: list[dict] | None = None) -> None:
        self.query_params = query_params or {}
        self.headers = {}
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self.send_text = AsyncMock()
        self.receive = AsyncMock(side_effect=messages or [{"type": "websocket.disconnect"}])
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                state=None,
                orchestrator=None,
                broadcaster=None,
            )
        )


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


def test_scroll_viewer_to_bottom_cancels_tmux_copy_mode_without_typing_into_cli() -> None:
    worker = make_worker()
    worker._use_tmux = True
    ws = object()
    worker._viewer_sessions[ws] = ViewerSession(
        ws=ws,
        session_name="tn-ticket-1-v1",
        master_fd=99,
        pid=1234,
    )

    with (
        patch("engine.worker.tmux_mgr.cancel_copy_mode", new_callable=AsyncMock) as cancel_copy_mode,
        patch("engine.worker.os.write") as write,
    ):
        asyncio.get_event_loop().run_until_complete(worker.scroll_viewer_to_bottom(ws))

    cancel_copy_mode.assert_awaited_once_with("tn-ticket-1-v1")
    write.assert_not_called()


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


def test_terminal_ws_spawns_review_adhoc_even_when_browser_disables_it() -> None:
    with tempfile.TemporaryDirectory() as worktree:
        websocket = FakeWebSocket(query_params={"token": "test-token", "allow_adhoc": "0"})
        state = MagicMock()
        state.get_ticket = AsyncMock(
            return_value=Ticket(
                id="ticket-1",
                run_id="run-1",
                jira_key="MC-1",
                state=TicketState.REVIEW,
                worktree_path=worktree,
            )
        )
        state.get_agent_profile = AsyncMock(return_value=None)
        state.update_ticket = AsyncMock()

        broadcaster = MagicMock()
        broadcaster.broadcast_ticket_update = AsyncMock()

        orchestrator = SimpleNamespace(
            _workers={},
            _adhoc_terminals={},
            _run_id="run-1",
            _tasks={},
            _spawning=set(),
        )

        websocket.app.state.state = state
        websocket.app.state.orchestrator = orchestrator
        websocket.app.state.broadcaster = broadcaster

        adhoc = SimpleNamespace(
            start=AsyncMock(),
            attach_viewer=AsyncMock(),
            detach_viewer=MagicMock(),
            is_running=True,
            _viewers=set(),
            _use_tmux=False,
            process=SimpleNamespace(pid=4321),
        )

        with (
            patch("api.routers.terminals.verify_ws_token", return_value=True),
            patch("engine.worker.AdHocTerminal", return_value=adhoc),
        ):
            asyncio.get_event_loop().run_until_complete(terminal_ws(websocket, "ticket-1"))

        adhoc.start.assert_awaited_once()
        adhoc.attach_viewer.assert_awaited_once_with(websocket, rows=24, cols=80)
        adhoc.detach_viewer.assert_called_once_with(websocket)
        websocket.accept.assert_awaited_once()
        websocket.close.assert_not_awaited()
        state.update_ticket.assert_awaited_once_with("ticket-1", worker_pid=4321)
        broadcaster.broadcast_ticket_update.assert_awaited_once_with(
            "run-1",
            "ticket-1",
            TicketState.REVIEW,
            worker_pid=4321,
        )
        assert orchestrator._adhoc_terminals["ticket-1"] is adhoc


def test_terminal_ws_accepts_then_reports_review_startup_failure() -> None:
    with tempfile.TemporaryDirectory() as worktree:
        websocket = FakeWebSocket(query_params={"token": "test-token", "allow_adhoc": "1"})
        state = MagicMock()
        state.get_ticket = AsyncMock(
            return_value=Ticket(
                id="ticket-1",
                run_id="run-1",
                jira_key="MC-1",
                state=TicketState.REVIEW,
                worktree_path=worktree,
            )
        )
        state.get_agent_profile = AsyncMock(return_value=None)
        state.update_ticket = AsyncMock()

        broadcaster = MagicMock()
        broadcaster.broadcast_ticket_update = AsyncMock()

        orchestrator = SimpleNamespace(
            _workers={},
            _adhoc_terminals={},
            _run_id="run-1",
            _tasks={},
            _spawning=set(),
        )

        websocket.app.state.state = state
        websocket.app.state.orchestrator = orchestrator
        websocket.app.state.broadcaster = broadcaster

        adhoc = SimpleNamespace(
            start=AsyncMock(side_effect=RuntimeError("tmux bootstrap failed")),
            attach_viewer=AsyncMock(),
            detach_viewer=MagicMock(),
            is_running=False,
            _viewers=set(),
            _use_tmux=False,
            process=None,
        )

        with (
            patch("api.routers.terminals.verify_ws_token", return_value=True),
            patch("engine.worker.AdHocTerminal", return_value=adhoc),
        ):
            asyncio.get_event_loop().run_until_complete(terminal_ws(websocket, "ticket-1"))

        websocket.accept.assert_awaited_once()
        websocket.send_text.assert_awaited_once_with('{"type": "startup_error", "message": "tmux bootstrap failed"}')
        websocket.close.assert_awaited_once_with(code=4005, reason="Terminal startup failed")
        adhoc.attach_viewer.assert_not_awaited()
        adhoc.detach_viewer.assert_not_called()
        state.update_ticket.assert_not_awaited()
        broadcaster.broadcast_ticket_update.assert_not_awaited()
        assert "ticket-1" not in orchestrator._adhoc_terminals


def test_terminal_ws_scroll_bottom_uses_worker_viewer_helper() -> None:
    websocket = FakeWebSocket(
        query_params={"token": "test-token"},
        messages=[
            {"type": "websocket.receive", "text": '{"type":"scroll_bottom"}'},
            {"type": "websocket.disconnect"},
        ],
    )
    state = MagicMock()
    broadcaster = MagicMock()
    orchestrator = SimpleNamespace(
        _workers={},
        _adhoc_terminals={},
        _run_id="run-1",
        _tasks={},
        _spawning=set(),
    )
    worker = SimpleNamespace(
        is_running=True,
        _use_tmux=False,
        attach_viewer=AsyncMock(),
        detach_viewer=MagicMock(),
        scroll_viewer_to_bottom=AsyncMock(),
    )
    orchestrator._workers["ticket-1"] = worker

    websocket.app.state.state = state
    websocket.app.state.orchestrator = orchestrator
    websocket.app.state.broadcaster = broadcaster

    with patch("api.routers.terminals.verify_ws_token", return_value=True):
        asyncio.get_event_loop().run_until_complete(terminal_ws(websocket, "ticket-1"))

    websocket.accept.assert_awaited_once()
    worker.attach_viewer.assert_awaited_once_with(websocket, rows=24, cols=80)
    worker.scroll_viewer_to_bottom.assert_awaited_once_with(websocket)
    worker.detach_viewer.assert_called_once_with(websocket)


def test_frontend_terminal_connect_does_not_force_disable_adhoc() -> None:
    html = Path(__file__).resolve().parents[1] / "static" / "index.html"
    source = html.read_text(encoding="utf-8")

    assert "allow_adhoc=0" not in source


def test_frontend_terminal_connect_handles_startup_error_messages() -> None:
    html = Path(__file__).resolve().parents[1] / "static" / "index.html"
    source = html.read_text(encoding="utf-8")

    assert "msg.type === 'startup_error'" in source
    assert "startupErrorShown" in source


def test_frontend_scroll_bottom_uses_control_message_instead_of_raw_q() -> None:
    html = Path(__file__).resolve().parents[1] / "static" / "index.html"
    source = html.read_text(encoding="utf-8")

    assert "type: 'scroll_bottom'" in source
    assert "encode('q')" not in source
