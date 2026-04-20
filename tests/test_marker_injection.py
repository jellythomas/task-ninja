from unittest.mock import MagicMock

from engine.worker import Worker


def make_worker(phases_config: list[dict] | None = None) -> Worker:
    return Worker(
        ticket_id="ticket-1",
        run_id="run-1",
        jira_key="MC-1",
        worktree_path="/worktree",
        state_manager=MagicMock(),
        broadcaster=MagicMock(),
        phases_config=phases_config,
    )


TWO_PHASE_CONFIG = [
    {
        "phase": "planning",
        "prompts": ["/planning-task {JIRA_KEY}"],
    },
    {
        "phase": "developing",
        "prompts": ["/developing-task {JIRA_KEY}"],
    },
]


# --- _resolve_phase_marker tests ---


def test_resolve_phase_marker_both_phases_filled_returns_planning_marker():
    worker = make_worker(phases_config=TWO_PHASE_CONFIG)
    assert worker._resolve_phase_marker("planning") == "[PLANNING_COMPLETE]"


def test_resolve_phase_marker_both_phases_filled_returns_developing_marker():
    worker = make_worker(phases_config=TWO_PHASE_CONFIG)
    assert worker._resolve_phase_marker("developing") == "[DEVELOPING_COMPLETE]"


def test_resolve_phase_marker_only_developing_filled_returns_markers():
    worker = make_worker(
        phases_config=[
            {
                "phase": "planning",
                "prompts": [],
            },
            {
                "phase": "developing",
                "prompts": ["/developing-task {JIRA_KEY}"],
            },
        ]
    )
    # Phase with prompts gets marker
    assert worker._resolve_phase_marker("developing") == "[DEVELOPING_COMPLETE]"
    # Phase without prompts still gets marker (engine waits for it from running CLI)
    assert worker._resolve_phase_marker("planning") == "[PLANNING_COMPLETE]"


def test_resolve_phase_marker_only_planning_filled_returns_markers():
    worker = make_worker(
        phases_config=[
            {
                "phase": "planning",
                "prompts": ["/planning-task {JIRA_KEY}"],
            },
            {
                "phase": "developing",
                "prompts": [],
            },
        ]
    )
    # Phase with prompts gets marker
    assert worker._resolve_phase_marker("planning") == "[PLANNING_COMPLETE]"
    # Phase without prompts still gets marker (engine waits for it from running CLI)
    assert worker._resolve_phase_marker("developing") == "[DEVELOPING_COMPLETE]"


def test_resolve_phase_marker_single_phase_in_config_returns_marker():
    worker = make_worker(
        phases_config=[
            {
                "phase": "developing",
                "prompts": ["/developing-task {JIRA_KEY}"],
            },
        ]
    )
    # Single phase with prompts should still get a marker
    assert worker._resolve_phase_marker("developing") == "[DEVELOPING_COMPLETE]"


def test_resolve_phase_marker_review_phase_returns_none():
    worker = make_worker(
        phases_config=[
            {"phase": "planning", "prompts": ["/planning-task {JIRA_KEY}"]},
            {"phase": "developing", "prompts": []},
            {"phase": "review", "prompts": []},
        ]
    )
    # review is not in PHASE_MARKERS — returns None (auto-complete, no wait)
    assert worker._resolve_phase_marker("review") is None


# --- _build_phase_prompt tests ---


def test_build_phase_prompt_with_marker_renders_template_without_injection():
    worker = make_worker(phases_config=TWO_PHASE_CONFIG)
    result = worker._build_phase_prompt(
        ["/planning-task {JIRA_KEY}"],
        "[PLANNING_COMPLETE]",
    )
    # Marker is NOT injected into prompt — skills handle printing it
    assert "IMPORTANT" not in result
    assert "[PLANNING_COMPLETE]" not in result
    assert "/planning-task MC-1" in result


def test_build_phase_prompt_without_marker_has_no_important_or_marker_text():
    worker = make_worker(phases_config=TWO_PHASE_CONFIG)
    result = worker._build_phase_prompt(
        ["/planning-task {JIRA_KEY}"],
        None,
    )
    assert "IMPORTANT" not in result
    assert "MARKER" not in result
    assert "/planning-task MC-1" in result
