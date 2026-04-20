import asyncio

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
        claude_flags=["--yolo"],
        phases_config=[
            {
                "phase": "planning",
                "prompts": ["/planning-task {JIRA_KEY}"],
                "marker": "[PLANNING_COMPLETE]",
            }
        ],
    )
    worker._use_tmux = True
    worker._tmux_target = "%42"
    return worker


def test_normalize_prompt_for_submit_flattens_multiline_prompt():
    worker = make_worker()

    assert (
        worker._normalize_prompt_for_submit(" /planning-task MC-1 \n\nWhen done\n print marker ")
        == "/planning-task MC-1 When done print marker"
    )


def test_build_phase_prompt_renders_slash_commands_without_marker():
    worker = make_worker()

    assert (
        worker._build_phase_prompt(
            ["/planning-task {JIRA_KEY} parent:{PARENT_BRANCH}"],
            "[PLANNING_COMPLETE]",
        )
        == "/planning-task MC-1 parent:master"
    )


def test_build_phase_prompt_renders_direct_prompts_without_marker():
    worker = make_worker()

    assert (
        worker._build_phase_prompt(
            ["Analyze ticket {JIRA_KEY}"],
            "[PLANNING_COMPLETE]",
        )
        == "Analyze ticket MC-1"
    )


def test_submission_echo_prefix_uses_visible_command_head_for_long_prompt():
    worker = make_worker()

    prompt = "/planning-task MC-9384 parent:master When you are completely done, print exactly: [PLANNING_COMPLETE]"

    assert worker._submission_echo_prefix(prompt) == "/planning-task MC-9384 parent:master"


def test_submission_echo_prefix_uses_bounded_visible_prefix_for_non_slash_prompt():
    worker = make_worker()

    prompt = (
        "Please investigate the planning failure and summarize the visible output before continuing with more work."
    )

    assert worker._submission_echo_prefix(prompt) == prompt[:40].strip()


def test_wait_for_prompt_echo_matches_visible_prefix_line_for_long_prompt():
    worker = make_worker()
    long_prompt = (
        "/planning-task MC-9384 parent:master When you are completely done, print exactly: [PLANNING_COMPLETE]"
    )
    visible_prefix = worker._submission_echo_prefix(long_prompt)
    baseline = "Type @ to mention\n> "

    worker._capture_submission_state = AsyncMock(return_value=f"Type @ to mention\n\u276f {visible_prefix}")

    assert (
        asyncio.get_event_loop().run_until_complete(
            worker._wait_for_prompt_echo(long_prompt, baseline, echo_text=visible_prefix, timeout=0.2)
        )
        == f"\u276f {visible_prefix}"
    )


def test_submit_phase_prompt_accepts_visible_prefix_when_full_prompt_is_not_visible():
    worker = make_worker()
    long_prompt = (
        "/planning-task MC-9384 parent:master When you are completely done, print exactly: [PLANNING_COMPLETE]"
    )
    visible_prefix = worker._submission_echo_prefix(long_prompt)
    baseline = "Type @ to mention\n> "
    composed_line = f"\u276f {visible_prefix}"

    worker._wait_for_startup_ready = AsyncMock()
    worker._capture_submission_state = AsyncMock(return_value=baseline)
    worker._verify_prompt_submitted = AsyncMock(return_value=True)

    async def wait_for_prompt_echo(
        prompt_text: str, baseline_text: str, *, echo_text: str | None = None, timeout: float = 2.0
    ) -> str:
        assert prompt_text == long_prompt
        assert baseline_text == baseline
        assert echo_text == visible_prefix
        assert timeout == 2.0
        return composed_line

    with (
        patch("engine.worker.tmux_mgr.send_literal_text", new=AsyncMock(return_value=True)),
        patch("engine.worker.tmux_mgr.send_key", new=AsyncMock(return_value=True)),
        patch.object(worker, "_wait_for_prompt_echo", new=AsyncMock(side_effect=wait_for_prompt_echo)),
    ):
        asyncio.get_event_loop().run_until_complete(worker._submit_phase_prompt(long_prompt))

    worker._verify_prompt_submitted.assert_awaited_once_with(
        long_prompt,
        composed_line=composed_line,
        baseline=baseline,
    )


def test_submit_phase_prompt_retries_when_verify_stays_idle():
    worker = make_worker()
    worker._wait_for_startup_ready = AsyncMock()
    worker._capture_submission_state = AsyncMock(return_value="Type @ to mention\n> ")
    worker._wait_for_prompt_echo = AsyncMock(return_value="> /planning-task MC-1")
    worker._verify_prompt_submitted = AsyncMock(side_effect=[False, True])

    with (
        patch("engine.worker.tmux_mgr.send_literal_text", new=AsyncMock(return_value=True)) as send_text_mock,
        patch("engine.worker.tmux_mgr.send_key", new=AsyncMock(return_value=True)) as send_key_mock,
    ):
        asyncio.get_event_loop().run_until_complete(worker._submit_phase_prompt("/planning-task MC-1"))

    assert send_text_mock.await_count == 2
    # Each attempt sends End + Enter (2 send_key calls per attempt)
    assert send_key_mock.await_count == 4
    worker._wait_for_startup_ready.assert_awaited_once_with(min_delay=1, stability_secs=1, timeout=10)
    assert worker._wait_for_prompt_echo.await_count == 2
    assert worker._verify_prompt_submitted.await_count == 2


def test_submit_phase_prompt_uses_literal_transport_for_claude_too():
    worker = make_worker()
    worker.claude_command = "claude"
    worker._capture_submission_state = AsyncMock(return_value="bypass permissions\n> ")
    worker._wait_for_prompt_echo = AsyncMock(return_value="> /planning-task MC-1")
    worker._verify_prompt_submitted = AsyncMock(return_value=True)

    with (
        patch("engine.worker.tmux_mgr.send_literal_text", new=AsyncMock(return_value=True)) as send_text_mock,
        patch("engine.worker.tmux_mgr.send_key", new=AsyncMock(return_value=True)) as send_key_mock,
        patch("engine.worker.tmux_mgr.send_keys", new=AsyncMock(return_value=True)) as send_keys_mock,
    ):
        asyncio.get_event_loop().run_until_complete(worker._submit_phase_prompt("/planning-task MC-1"))

    send_text_mock.assert_awaited_once_with("%42", "/planning-task MC-1")
    # End key to dismiss autocomplete, then Enter to submit
    assert send_key_mock.await_count == 2
    send_key_mock.assert_any_await("%42", "End")
    send_key_mock.assert_any_await("%42", "Enter")
    send_keys_mock.assert_not_called()


def test_verify_prompt_submission_accepts_prompt_when_footer_phrases_persist():
    worker = make_worker()

    before = "Type @ to mention\n> "
    typed = "Type @ to mention\n> /planning-task MC-1"
    after_enter = "Type @ to mention\nThinking about MC-1..."

    worker._capture_submission_state = AsyncMock(side_effect=[typed, after_enter])

    assert (
        asyncio.get_event_loop().run_until_complete(
            worker._verify_prompt_submitted(
                "/planning-task MC-1",
                composed_line="> /planning-task MC-1",
                baseline=before,
                timeout=0.2,
            )
        )
        is True
    )


def test_verify_prompt_submission_fails_when_composed_input_line_never_changes():
    worker = make_worker()

    stuck = "Type @ to mention\n> /planning-task MC-1"
    worker._capture_submission_state = AsyncMock(side_effect=[stuck, stuck, stuck])

    assert (
        asyncio.get_event_loop().run_until_complete(
            worker._verify_prompt_submitted(
                "/planning-task MC-1",
                composed_line="> /planning-task MC-1",
                baseline="Type @ to mention\n> ",
                timeout=0.2,
            )
        )
        is False
    )


def test_verify_prompt_submitted_rejects_idle_redraw_after_prefix_disappears():
    worker = make_worker()
    baseline = "Type @ to mention\n> "
    idle = "Type @ to mention\n> "
    worker._capture_submission_state = AsyncMock(side_effect=[idle, idle, idle])

    result = asyncio.get_event_loop().run_until_complete(
        worker._verify_prompt_submitted(
            "/planning-task MC-9384 parent:master",
            composed_line="\u276f /planning-task MC-9384 parent:master",
            baseline=baseline,
            timeout=0.2,
        )
    )

    assert result is False


def test_verify_prompt_submitted_rejects_static_status_bullets_without_progress():
    worker = make_worker()
    baseline = "Type @ to mention\n> "
    idle_with_status = "\n".join(
        [
            "● Environment loaded: 2 custom instructions, 14 agents, 38 skills",
            "! Failed to connect to MCP server 'sequentialthinking'.",
            "● MCP Servers reloaded: 5 servers connected",
            "Type @ to mention files, # for issues/PRs, / for commands, or ? for shortcuts",
            "shift+tab switch mode",
            "Remaining reqs.: 94.3%",
        ]
    )
    worker._capture_submission_state = AsyncMock(side_effect=[idle_with_status, idle_with_status, idle_with_status])

    result = asyncio.get_event_loop().run_until_complete(
        worker._verify_prompt_submitted(
            "/planning-task MC-9384 parent:master",
            composed_line="\u276f /planning-task MC-9384 parent:master",
            baseline=baseline,
            timeout=0.2,
        )
    )

    assert result is False


def test_verify_prompt_submitted_rejects_copilot_pending_status_without_progress():
    worker = make_worker()
    baseline = "Type @ to mention\n> "
    pending_only = "\n".join(
        [
            "Type @ to mention files, # for issues/PRs, / for commands, or ? for shortcuts",
            "shift+tab switch mode",
            "└ [pending]",
        ]
    )
    worker._capture_submission_state = AsyncMock(side_effect=[pending_only, pending_only, pending_only])

    result = asyncio.get_event_loop().run_until_complete(
        worker._verify_prompt_submitted(
            "/planning-task MC-9384 parent:master",
            composed_line="❯ /planning-task MC-9384 parent:master",
            baseline=baseline,
            timeout=0.2,
        )
    )

    assert result is False


def test_verify_prompt_submitted_accepts_real_busy_transition():
    worker = make_worker()
    baseline = "Type @ to mention\n> "
    busy = "● Thinking (Esc to cancel)"
    worker._capture_submission_state = AsyncMock(side_effect=[busy])

    result = asyncio.get_event_loop().run_until_complete(
        worker._verify_prompt_submitted(
            "/planning-task MC-9384 parent:master",
            composed_line="\u276f /planning-task MC-9384 parent:master",
            baseline=baseline,
            timeout=0.2,
        )
    )

    assert result is True


def test_pane_shows_prompt_processed_ignores_pending_status_after_echo():
    worker = make_worker()
    worker._last_phase_prompt = "/planning-task MC-9384 parent:master"
    pending_after_echo = "\n".join(
        [
            "❯ /planning-task MC-9384 parent:master",
            "└ [pending]",
            "Type @ to mention files, # for issues/PRs, / for commands, or ? for shortcuts",
            "shift+tab switch mode",
        ]
    )

    assert worker._pane_shows_prompt_processed(pending_after_echo) is False
