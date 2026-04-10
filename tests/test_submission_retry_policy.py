import asyncio

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers.tickets import retry_ticket
from engine.ticket_watchdog import TicketWatchdog
from engine.worker import (
    DETERMINISTIC_PROMPT_SUBMISSION_ERROR_PREFIX,
    DeterministicPromptSubmissionError,
    Worker,
)
from models.ticket import Ticket, TicketState


def make_worker_with_state(state: MagicMock, broadcaster: MagicMock | None = None) -> Worker:
    ticket_broadcaster = broadcaster or MagicMock()
    ticket_broadcaster.broadcast_ticket_update = AsyncMock()
    return Worker(
        ticket_id="ticket-1",
        run_id="run-1",
        jira_key="MC-1",
        worktree_path="/worktree",
        state_manager=state,
        broadcaster=ticket_broadcaster,
        claude_command="claude",
        phases_config=[
            {
                "phase": "planning",
                "prompts": ["/planning-task {JIRA_KEY}"],
                "marker": "[PLANNING_COMPLETE]",
            }
        ],
    )


def test_first_deterministic_prompt_submission_failure_requeues_once():
    ticket = Ticket(
        id="ticket-1",
        run_id="run-1",
        jira_key="MC-1",
        state=TicketState.PLANNING,
        prompt_submit_requeues=0,
    )
    state = MagicMock()
    state.get_ticket = AsyncMock(return_value=ticket)
    state.update_ticket = AsyncMock()
    state.update_ticket_state = AsyncMock()
    broadcaster = MagicMock()
    broadcaster.broadcast_ticket_update = AsyncMock()
    worker = make_worker_with_state(state, broadcaster)

    result = asyncio.get_event_loop().run_until_complete(
        worker._handle_prompt_submission_failure(
            "planning",
            DeterministicPromptSubmissionError("Interactive prompt submission was not accepted by CLI"),
        )
    )

    error = (
        f"{DETERMINISTIC_PROMPT_SUBMISSION_ERROR_PREFIX} "
        "Interactive prompt submission was not accepted by CLI"
    )
    assert result is False
    state.update_ticket.assert_any_await("ticket-1", prompt_submit_requeues=1, error=error)
    state.update_ticket_state.assert_awaited_once_with("ticket-1", TicketState.QUEUED)
    broadcaster.broadcast_ticket_update.assert_awaited_once_with(
        "run-1", "ticket-1", TicketState.QUEUED, error=error
    )


def test_repeated_deterministic_prompt_submission_failure_fails_ticket():
    ticket = Ticket(
        id="ticket-1",
        run_id="run-1",
        jira_key="MC-1",
        state=TicketState.PLANNING,
        prompt_submit_requeues=2,  # exhausted all retries (max=2)
    )
    state = MagicMock()
    state.get_ticket = AsyncMock(return_value=ticket)
    state.update_ticket = AsyncMock()
    state.update_ticket_state = AsyncMock()
    broadcaster = MagicMock()
    broadcaster.broadcast_ticket_update = AsyncMock()
    worker = make_worker_with_state(state, broadcaster)

    result = asyncio.get_event_loop().run_until_complete(
        worker._handle_prompt_submission_failure(
            "planning",
            DeterministicPromptSubmissionError("Interactive prompt submission was not accepted by CLI"),
        )
    )

    error = (
        f"{DETERMINISTIC_PROMPT_SUBMISSION_ERROR_PREFIX} "
        "Interactive prompt submission was not accepted by CLI"
    )
    assert result is False
    state.update_ticket.assert_any_await("ticket-1", error=error)
    state.update_ticket_state.assert_awaited_once_with("ticket-1", TicketState.FAILED)
    broadcaster.broadcast_ticket_update.assert_awaited_once_with(
        "run-1", "ticket-1", TicketState.FAILED, error=error
    )


def test_submit_phase_prompt_raises_deterministic_failure_when_pane_delta_never_changes():
    state = MagicMock()
    state.get_ticket = AsyncMock()
    state.update_ticket = AsyncMock()
    state.update_ticket_state = AsyncMock()
    worker = make_worker_with_state(state)
    worker._use_tmux = True
    worker._tmux_target = "%42"
    worker._wait_for_startup_ready = AsyncMock()
    worker._wait_for_prompt_echo = AsyncMock(return_value="> /planning-task MC-1")
    worker._capture_submission_state = AsyncMock(return_value="Type @ to mention\n> ")

    real_verify_prompt_submitted = Worker._verify_prompt_submitted.__get__(worker, Worker)

    async def verify_stuck_submission(prompt_text: str, **kwargs: object) -> bool:
        original_capture = worker._capture_submission_state
        worker._capture_submission_state = AsyncMock(return_value="Type @ to mention\n> /planning-task MC-1")
        try:
            return await real_verify_prompt_submitted(prompt_text, timeout=0.2, **kwargs)
        finally:
            worker._capture_submission_state = original_capture

    worker._verify_prompt_submitted = AsyncMock(side_effect=verify_stuck_submission)

    with (
        pytest.raises(
            DeterministicPromptSubmissionError,
            match="Interactive prompt submission was not accepted by CLI",
        ),
        patch("engine.worker.tmux_mgr.send_literal_text", new=AsyncMock(return_value=True)) as send_text_mock,
        patch("engine.worker.tmux_mgr.send_key", new=AsyncMock(return_value=True)) as send_key_mock,
    ):
        asyncio.get_event_loop().run_until_complete(worker._submit_phase_prompt("/planning-task MC-1"))

    assert send_text_mock.await_count == 2
    # Each attempt sends End + Enter (2 send_key calls per attempt)
    assert send_key_mock.await_count == 4
    worker._wait_for_startup_ready.assert_awaited_once_with(min_delay=1, stability_secs=1, timeout=10)
    assert worker._verify_prompt_submitted.await_count == 2


def test_watchdog_retries_deterministic_prompt_submission_failure():
    """Prompt submission failures (e.g. MCP reload race) are transient — watchdog should retry."""
    state = MagicMock()
    broadcaster = MagicMock()
    watchdog = TicketWatchdog(state, broadcaster)
    watchdog._is_auto_retry_enabled = MagicMock(return_value=True)
    watchdog._set_timer = MagicMock()

    watchdog.on_ticket_failed(
        "ticket-1",
        f"{DETERMINISTIC_PROMPT_SUBMISSION_ERROR_PREFIX} Interactive prompt submission was not accepted by CLI",
    )

    watchdog._set_timer.assert_called_once()
    assert watchdog._retry_counts["ticket-1"] == 1


def test_manual_retry_resets_prompt_submission_requeue_counter():
    ticket = Ticket(
        id="ticket-1",
        run_id="run-1",
        jira_key="MC-1",
        state=TicketState.FAILED,
        prompt_submit_requeues=1,
    )
    state = MagicMock()
    state.get_ticket = AsyncMock(return_value=ticket)
    state.update_ticket = AsyncMock()
    state.update_ticket_state = AsyncMock()
    orchestrator = MagicMock()
    orchestrator.kill_worker = AsyncMock(return_value=False)
    orchestrator._running = True
    broadcaster = MagicMock()
    broadcaster.broadcast_ticket_update = AsyncMock()

    result = asyncio.get_event_loop().run_until_complete(
        retry_ticket(
            "ticket-1",
            clean=False,
            state=state,
            orchestrator=orchestrator,
            broadcaster=broadcaster,
            config={},
        )
    )

    assert result == {"status": "retrying", "clean": False}
    state.update_ticket.assert_awaited_once_with(
        "ticket-1",
        error=None,
        worker_pid=None,
        paused=False,
        prompt_submit_requeues=0,
    )
    state.update_ticket_state.assert_awaited_once_with("ticket-1", TicketState.QUEUED)
