"""Tests for Orchestrator._prioritize_queue conflict-prevention logic."""
import asyncio
import json
import sys
import os
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.ticket import Ticket, TicketState


def make_ticket(jira_key, predicted_files=None, paused=False, rank=0, blocked_by_keys=None):
    return Ticket(
        id=f"id-{jira_key}",
        run_id="run-1",
        jira_key=jira_key,
        state=TicketState.QUEUED,
        rank=rank,
        paused=paused,
        predicted_files=json.dumps(predicted_files) if predicted_files else None,
        blocked_by_keys=json.dumps(blocked_by_keys) if blocked_by_keys else None,
    )


def make_orchestrator(workers_predicted_files: dict[str, list[str]] | None = None):
    """Build a minimal Orchestrator-like object with mocked deps."""
    from engine.orchestrator import Orchestrator

    state = MagicMock()
    broadcaster = MagicMock()
    config = {}
    orch = Orchestrator.__new__(Orchestrator)
    orch.state = state
    orch.broadcaster = broadcaster
    orch.config = config
    orch._run_id = "run-1"
    orch._workers = {}

    # Mock state.count_dependents to always return 0
    state.count_dependents = AsyncMock(return_value=0)
    # Mock state.get_tickets_by_jira_keys to return [] (no blockers resolved)
    state.get_tickets_by_jira_keys = AsyncMock(return_value=[])

    # Set up active workers with their predicted files
    if workers_predicted_files:
        for tid, pfiles in workers_predicted_files.items():
            orch._workers[tid] = MagicMock()
            worker_ticket = MagicMock()
            worker_ticket.predicted_files = json.dumps(pfiles) if pfiles else None
            state.get_ticket = AsyncMock(side_effect=lambda tid, _wt=workers_predicted_files, _tickets=None: _make_get_ticket(tid, _wt))
    else:
        state.get_ticket = AsyncMock(return_value=None)

    return orch


def _make_get_ticket(tid: str, workers_predicted_files: dict):
    """Return a ticket mock for active worker ticket IDs."""
    if tid in workers_predicted_files:
        t = MagicMock()
        pfiles = workers_predicted_files[tid]
        t.predicted_files = json.dumps(pfiles) if pfiles else None
        return t
    return None


class TestPrioritizeQueueNoConflict:
    """Happy path: no file overlap — all tickets pass through."""

    def test_empty_queue_returns_empty(self):
        orch = make_orchestrator()
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue([]))
        assert result == []

    def test_tickets_without_predicted_files_pass_through(self):
        orch = make_orchestrator()
        tickets = [make_ticket("MC-1"), make_ticket("MC-2")]
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue(tickets))
        assert len(result) == 2

    def test_no_active_workers_all_tickets_pass(self):
        orch = make_orchestrator(workers_predicted_files={})
        tickets = [
            make_ticket("MC-1", predicted_files=["src/auth/login.py"]),
            make_ticket("MC-2", predicted_files=["src/payments/charge.py"]),
        ]
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue(tickets))
        assert len(result) == 2

    def test_non_overlapping_files_both_pass(self):
        orch = make_orchestrator(workers_predicted_files={"id-ACTIVE": ["src/auth/login.py"]})
        orch.state.get_ticket = AsyncMock(side_effect=lambda tid: _make_get_ticket(tid, {"id-ACTIVE": ["src/auth/login.py"]}))
        tickets = [
            make_ticket("MC-1", predicted_files=["src/payments/charge.py"]),
            make_ticket("MC-2", predicted_files=["api/routes.py"]),
        ]
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue(tickets))
        assert len(result) == 2


class TestPrioritizeQueueConflict:
    """File overlap detection — conflicting tickets are skipped."""

    def test_ticket_overlapping_active_worker_is_skipped(self):
        orch = make_orchestrator(workers_predicted_files={"id-ACTIVE": ["src/auth/login.py"]})
        orch.state.get_ticket = AsyncMock(side_effect=lambda tid: _make_get_ticket(tid, {"id-ACTIVE": ["src/auth/login.py"]}))
        tickets = [
            make_ticket("MC-1", predicted_files=["src/auth/login.py"]),  # conflict
        ]
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue(tickets))
        assert result == []

    def test_only_conflicting_ticket_is_skipped_others_pass(self):
        orch = make_orchestrator(workers_predicted_files={"id-ACTIVE": ["src/auth/login.py"]})
        orch.state.get_ticket = AsyncMock(side_effect=lambda tid: _make_get_ticket(tid, {"id-ACTIVE": ["src/auth/login.py"]}))
        tickets = [
            make_ticket("MC-1", predicted_files=["src/payments/charge.py"]),  # safe
            make_ticket("MC-2", predicted_files=["src/auth/login.py"]),       # conflict
        ]
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue(tickets))
        assert len(result) == 1
        assert result[0].jira_key == "MC-1"

    def test_candidate_files_accumulate_preventing_subsequent_conflicts(self):
        """Once MC-1 is selected, MC-2 touching same file should be blocked."""
        orch = make_orchestrator()
        tickets = [
            make_ticket("MC-1", predicted_files=["src/auth/login.py"], rank=0),
            make_ticket("MC-2", predicted_files=["src/auth/login.py"], rank=1),
        ]
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue(tickets))
        # MC-1 passes first; MC-2 conflicts with MC-1's files
        assert len(result) == 1
        assert result[0].jira_key == "MC-1"

    def test_paused_tickets_always_skipped(self):
        orch = make_orchestrator()
        tickets = [make_ticket("MC-1", paused=True)]
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue(tickets))
        assert result == []


class TestPrioritizeQueueEdgeCases:
    """Edge cases: malformed JSON, empty predicted_files, etc."""

    def test_malformed_predicted_files_json_treated_as_no_files(self):
        orch = make_orchestrator()
        ticket = make_ticket("MC-1")
        ticket.predicted_files = "not-valid-json"
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue([ticket]))
        # Should not raise; ticket passes through (no overlap detected)
        assert len(result) == 1

    def test_predicted_files_null_in_db_treated_as_no_files(self):
        orch = make_orchestrator()
        tickets = [make_ticket("MC-1", predicted_files=None)]
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue(tickets))
        assert len(result) == 1

    def test_worker_with_malformed_predicted_files_ignored_safely(self):
        orch = make_orchestrator(workers_predicted_files={"id-ACTIVE": None})
        bad_worker_ticket = MagicMock()
        bad_worker_ticket.predicted_files = "{{bad json}}"
        orch.state.get_ticket = AsyncMock(return_value=bad_worker_ticket)
        tickets = [make_ticket("MC-1", predicted_files=["src/auth/login.py"])]
        # Should not raise; ticket passes through
        result = asyncio.get_event_loop().run_until_complete(orch._prioritize_queue(tickets))
        assert len(result) == 1
