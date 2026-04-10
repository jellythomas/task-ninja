import asyncio

from unittest.mock import AsyncMock, patch

from engine import tmux


def test_send_literal_text_targets_explicit_pane():
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate.return_value = (b"", b"")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as exec_mock:
        ok = asyncio.get_event_loop().run_until_complete(
            tmux.send_literal_text("%42", "/planning-task MC-1")
        )

    assert ok is True
    exec_mock.assert_awaited_with(
        "tmux",
        "send-keys",
        "-l",
        "-t",
        "%42",
        "/planning-task MC-1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def test_get_primary_pane_id_returns_first_pane():
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate.return_value = (b"%42\n", b"")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        pane_id = asyncio.get_event_loop().run_until_complete(
            tmux.get_primary_pane_id("tn-ticket")
        )

    assert pane_id == "%42"
