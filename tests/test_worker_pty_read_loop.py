import asyncio
import logging

from unittest.mock import AsyncMock, MagicMock, patch

from engine.worker import Worker


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
    worker._use_tmux = True
    worker._tmux_session = "tn-ticket-1"
    worker._master_fd = 42
    worker._cancelled = False
    worker._send_to_viewers = AsyncMock()
    worker._process_output = AsyncMock()
    return worker


def test_pty_read_loop_exits_cleanly_when_fd_is_cleared_before_select_runs(caplog) -> None:
    worker = make_worker()
    loop = asyncio.get_event_loop()

    async def run_in_executor(_executor, func):
        worker._master_fd = None
        worker._cancelled = True
        return func()

    def fake_select(readers, _writers, _errors, _timeout):
        if readers == [None]:
            raise TypeError("argument must be an int, or have a fileno() method")
        return ([], [], [])

    with (
        caplog.at_level(logging.ERROR),
        patch.object(loop, "run_in_executor", side_effect=run_in_executor),
        patch("engine.worker.select.select", side_effect=fake_select),
    ):
        asyncio.get_event_loop().run_until_complete(worker._pty_read_loop())

    assert "PTY read loop unexpected error" not in caplog.text


def test_pty_read_loop_stops_when_master_fd_changes_after_select(caplog) -> None:
    worker = make_worker()
    loop = asyncio.get_event_loop()
    executor_calls = 0

    async def run_in_executor(_executor, func):
        nonlocal executor_calls
        executor_calls += 1
        result = func()
        if executor_calls == 1:
            worker._master_fd = 99
            return result
        raise AssertionError("read batch should not run after fd swap")

    with (
        caplog.at_level(logging.ERROR),
        patch.object(loop, "run_in_executor", side_effect=run_in_executor),
        patch("engine.worker.select.select", return_value=([42], [], [])),
    ):
        asyncio.get_event_loop().run_until_complete(worker._pty_read_loop())

    assert executor_calls == 1
    assert "PTY read loop unexpected error" not in caplog.text
