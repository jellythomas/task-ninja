"""tmux session management for multi-viewer terminal support.

Provides per-viewer independent terminal sizing by using tmux grouped sessions.
Each WebSocket viewer gets its own tmux client session that shares windows with
the main session but sizes independently.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import shutil
import struct
import subprocess
import sys
import termios

logger = logging.getLogger(__name__)

SESSION_PREFIX = "tn-"


def is_available() -> bool:
    """Check if tmux is installed and accessible."""
    return shutil.which("tmux") is not None


def auto_install() -> bool:
    """Attempt to install tmux automatically. Returns True if successful."""
    if is_available():
        return True

    print("[tmux] tmux not found — attempting auto-install...", file=sys.stderr)

    commands: list[list[str]] = []

    if sys.platform == "darwin":
        # macOS — try Homebrew
        if shutil.which("brew"):
            commands.append(["brew", "install", "tmux"])
        else:
            print("[tmux] Homebrew not found. Install tmux with: brew install tmux", file=sys.stderr)
            return False

    elif sys.platform == "win32":
        # Windows — check if we're in WSL
        try:
            result = subprocess.run(["wsl", "--status"], capture_output=True, timeout=5)
            if result.returncode == 0:
                commands.append(["wsl", "sudo", "apt-get", "install", "-y", "tmux"])
            else:
                print(
                    "[tmux] tmux requires Unix PTY (not available on native Windows).\n"
                    "[tmux] Install WSL: wsl --install\n"
                    "[tmux] Then: wsl sudo apt-get install -y tmux",
                    file=sys.stderr,
                )
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired):
            print(
                "[tmux] tmux requires WSL on Windows.\n"
                "[tmux] Install WSL: wsl --install\n"
                "[tmux] Then: wsl sudo apt-get install -y tmux",
                file=sys.stderr,
            )
            return False

    else:
        # Linux — try package managers in order
        if shutil.which("apt-get"):
            commands.append(["sudo", "apt-get", "install", "-y", "tmux"])
        elif shutil.which("dnf"):
            commands.append(["sudo", "dnf", "install", "-y", "tmux"])
        elif shutil.which("yum"):
            commands.append(["sudo", "yum", "install", "-y", "tmux"])
        elif shutil.which("pacman"):
            commands.append(["sudo", "pacman", "-S", "--noconfirm", "tmux"])
        elif shutil.which("apk"):
            commands.append(["sudo", "apk", "add", "tmux"])
        else:
            print("[tmux] No supported package manager found. Install tmux manually.", file=sys.stderr)
            return False

    for cmd in commands:
        try:
            print(f"[tmux] Running: {' '.join(cmd)}", file=sys.stderr)
            subprocess.check_call(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if is_available():
                print("[tmux] Successfully installed tmux.", file=sys.stderr)
                return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning("tmux install command failed: %s", e)

    print("[tmux] Auto-install failed. Please install tmux manually.", file=sys.stderr)
    return False


async def create_session(session_name: str, cmd: list[str], cwd: str, rows: int = 24, cols: int = 80) -> bool:
    """Create a new detached tmux session running the given command."""
    tmux_cmd = [
        "tmux",
        "new-session",
        "-d",  # Detached
        "-s",
        session_name,  # Session name
        "-x",
        str(cols),  # Initial width
        "-y",
        str(rows),  # Initial height
        "--",  # Separator
        *cmd,  # Command to run
    ]

    # Configure session: hide status bar (viewers are web-based, not terminal clients)
    # and set window-size to latest (follow most recently active client)
    post_cmds = [
        ["tmux", "set-option", "-t", session_name, "status", "off"],
        ["tmux", "set-option", "-t", session_name, "window-size", "latest"],
        # Prevent shell fallback: if the command exits, destroy the pane
        # instead of leaving a bare shell (overrides user's tmux.conf)
        ["tmux", "set-option", "-t", session_name, "remain-on-exit", "off"],
        # Enable mouse globally so all grouped viewer sessions inherit it
        ["tmux", "set-option", "-g", "mouse", "on"],
        # Enable CSI u / extended keys so Shift+Enter (\x1b[13;2u) reaches
        # the application (required for CSI u input mode with Claude Code)
        ["tmux", "set-option", "-t", session_name, "extended-keys", "always"],
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *tmux_cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("Failed to create tmux session %s: %s", session_name, stderr.decode())
            return False

        # Apply post-creation settings (hide status bar, set window-size mode)
        for post_cmd in post_cmds:
            post_proc = await asyncio.create_subprocess_exec(
                *post_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await post_proc.communicate()

        logger.info("Created tmux session: %s", session_name)
        return True
    except (OSError, FileNotFoundError) as e:
        logger.error("tmux exec failed: %s", e)
        return False


async def create_grouped_session(target_session: str, viewer_session: str, rows: int = 24, cols: int = 80) -> bool:
    """Create a grouped session that shares windows with the target but has independent sizing."""
    tmux_cmd = [
        "tmux",
        "new-session",
        "-d",  # Detached
        "-t",
        target_session,  # Target session to group with
        "-s",
        viewer_session,  # New session name
        "-x",
        str(cols),  # Initial width (viewer's actual size)
        "-y",
        str(rows),  # Initial height (viewer's actual size)
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *tmux_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("Failed to create grouped session %s: %s", viewer_session, stderr.decode())
            return False

        # Apply viewer session settings: hide status bar (web viewers don't need
        # tmux chrome) and enable mouse for scroll support
        for post_cmd in [
            ["tmux", "set-option", "-t", viewer_session, "status", "off"],
            ["tmux", "set-option", "-t", viewer_session, "mouse", "on"],
        ]:
            post_proc = await asyncio.create_subprocess_exec(
                *post_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await post_proc.communicate()

        logger.info("Created grouped session: %s -> %s", viewer_session, target_session)
        return True
    except (OSError, FileNotFoundError) as e:
        logger.error("tmux grouped session failed: %s", e)
        return False


async def attach_pty(session_name: str, rows: int = 24, cols: int = 80) -> tuple[int, int] | None:
    """Attach to a tmux session via a new PTY and return (master_fd, pid).

    Spawns `tmux attach -t <session>` in a child process connected to a PTY.
    The caller reads/writes the master_fd to stream terminal I/O to the viewer.
    The PTY is sized to rows x cols so tmux renders at the correct dimensions.
    """
    try:
        master_fd, slave_fd = pty.openpty()

        # Set PTY size BEFORE attaching — tmux reads this on attach
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

        # Set non-blocking on master
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "attach-session",
            "-t",
            session_name,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
        )
        os.close(slave_fd)

        return master_fd, proc.pid

    except (OSError, FileNotFoundError) as e:
        logger.error("Failed to attach PTY to tmux session %s: %s", session_name, e)
        with contextlib.suppress(OSError):
            os.close(master_fd)
        return None


def resize_pty(master_fd: int, rows: int, cols: int) -> None:
    """Resize a PTY attached to a tmux session."""
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


async def kill_session(session_name: str) -> None:
    """Kill a tmux session (also kills all grouped sessions)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "kill-session",
            "-t",
            session_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        logger.info("Killed tmux session: %s", session_name)
    except (OSError, FileNotFoundError):
        pass


async def session_exists(session_name: str) -> bool:
    """Check if a tmux session with this exact name exists.

    Uses ``list-sessions`` instead of ``has-session -t`` because ``-t``
    also matches **group names**.  An orphaned grouped session (e.g.
    ``foo-monitor`` in group ``foo``) causes ``has-session -t foo`` to
    return true even when session ``foo`` is gone.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-sessions",
            "-F",
            "#{session_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return False
        sessions = stdout.decode().strip().splitlines()
        return session_name in sessions
    except (OSError, FileNotFoundError):
        return False


async def list_sessions(prefix: str = SESSION_PREFIX) -> list[str]:
    """List all tmux sessions matching the prefix."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-sessions",
            "-F",
            "#{session_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
        return [s for s in stdout.decode().strip().splitlines() if s.startswith(prefix)]
    except (OSError, FileNotFoundError):
        return []


async def cleanup_orphans(prefix: str = SESSION_PREFIX) -> int:
    """Kill orphaned tmux sessions from previous runs. Returns count cleaned."""
    sessions = await list_sessions(prefix)
    count = 0
    for name in sessions:
        await kill_session(name)
        count += 1
    if count:
        logger.info("Cleaned up %d orphaned tmux sessions", count)
    return count


async def capture_pane(session_name: str, history_lines: int = 200) -> str | None:
    """Capture visible pane content plus recent scrollback. Returns text or None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "capture-pane",
            "-t",
            session_name,
            "-p",
            "-S",
            str(-history_lines),  # scrollback lines to capture
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace")
        return None
    except (OSError, FileNotFoundError):
        return None


async def send_keys(session_name: str, keys: str, *, use_csi_u: bool = False) -> bool:
    """Send keys to a tmux session (for phase prompt injection).

    Supports two multiline injection methods selected by *use_csi_u*:

    - **CSI u mode** (``use_csi_u=True``): Sends each line literally with
      ``-l`` and joins them with Shift+Enter via CSI u escape sequence
      (``\\x1b[13;2u``).  Requires ``extended-keys=always`` on the session.
      Proven to work with Claude Code's Ink-based TUI.

    - **Literal single-line mode** (default): Joins all lines with spaces
      and sends via ``send-keys -l`` as a single string.  Works with ANY
      CLI — proven with Copilot, Claude Code, and generic TUIs.
      (``paste-buffer`` does NOT work with Copilot — tested and confirmed.)

    Both modes finish with a final Enter keystroke to submit the prompt.
    """
    try:
        if use_csi_u:
            return await _send_keys_csi_u(session_name, keys)
        return await _send_keys_literal(session_name, keys)
    except (OSError, FileNotFoundError):
        return False


async def get_primary_pane_id(session_name: str) -> str | None:
    """Return the first pane id for a tmux session."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-panes",
            "-t",
            session_name,
            "-F",
            "#{pane_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout.strip():
            return stdout.decode().strip().splitlines()[0]
    except (OSError, FileNotFoundError):
        pass
    return None


async def send_literal_text(target: str, text: str) -> bool:
    """Send literal text to an exact tmux target."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "send-keys",
            "-l",
            "-t",
            target,
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except (OSError, FileNotFoundError):
        return False


async def send_key(target: str, key: str) -> bool:
    """Send a single key to an exact tmux target."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "send-keys",
            "-t",
            target,
            key,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except (OSError, FileNotFoundError):
        return False


async def _send_keys_csi_u(session_name: str, keys: str) -> bool:
    """Multiline input via CSI u Shift+Enter (Claude Code proven)."""
    lines = keys.split("\n")
    for i, line in enumerate(lines):
        if line:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "send-keys",
                "-l",
                "-t",
                session_name,
                line,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode != 0:
                return False

        # Between lines, send Shift+Enter via CSI u escape sequence
        if i < len(lines) - 1:
            await asyncio.sleep(0.05)
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "send-keys",
                "-H",
                "-t",
                session_name,
                "1b",
                "5b",
                "31",
                "33",
                "3b",
                "32",
                "75",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode != 0:
                return False

    await asyncio.sleep(0.15)
    return await send_key(session_name, "Enter")


async def _send_keys_literal(session_name: str, keys: str) -> bool:
    """Single-line literal input via send-keys -l (universal).

    Joins multiline text into a single line (AI models don't need newlines
    to understand instructions).  Proven to work with Copilot CLI, Claude
    Code, and generic TUIs.  ``paste-buffer`` was tested and does NOT work
    with Copilot.
    """
    # Collapse newlines into spaces — AI models understand either way
    single_line = " ".join(line for line in keys.split("\n") if line.strip())

    ok = await send_literal_text(session_name, single_line)
    if not ok:
        return False

    # Dismiss any autocomplete/suggestion dropdown (e.g. Copilot shows
    # slash-command suggestions that intercept Enter).  Send End key to
    # move cursor to end of typed text — this implicitly closes dropdowns
    # in most TUIs without side effects.
    # NOTE: Escape exits Copilot; Space selects autocomplete. Both are unsafe.
    await asyncio.sleep(0.15)
    ok = await send_key(session_name, "End")

    await asyncio.sleep(0.1)
    return await send_key(session_name, "Enter")


async def refresh_client(session_name: str) -> bool:
    """Refresh a tmux client's display without sending input to the pane process.

    This is the safe alternative to sending \\x0c (Ctrl-L) — it redraws the
    viewer's terminal without any bytes reaching the running CLI.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "refresh-client",
            "-t",
            session_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except (OSError, FileNotFoundError):
        return False


async def send_keys_raw(session_name: str, key: str) -> bool:
    """Send a single key to a tmux session (Enter, Escape, etc.)."""
    return await send_key(session_name, key)


async def pane_in_mode(target: str) -> bool | None:
    """Return whether the target pane is currently in a tmux mode (for example copy-mode)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "display-message",
            "-p",
            "-t",
            target,
            "#{pane_in_mode}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return stdout.decode().strip() == "1"
    except (OSError, FileNotFoundError):
        pass
    return None


async def cancel_copy_mode(target: str) -> bool:
    """Exit tmux copy-mode for the given target without sending text to the CLI.

    Always attempts the cancel — ``pane_in_mode`` is a pane-level attribute
    that doesn't reliably reflect per-client copy-mode state in grouped
    sessions.  ``send-keys -X cancel`` is harmless when not in copy-mode.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "send-keys",
            "-X",
            "-t",
            target,
            "cancel",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        # returncode != 0 just means we weren't in copy-mode — that's fine
        return True
    except (OSError, FileNotFoundError):
        return False


async def get_cursor_position(session_name: str) -> tuple[int, int] | None:
    """Get cursor (x, y) position in the active pane.

    Returns a tuple of (cursor_x, cursor_y) or None on failure.
    Used for startup readiness detection — when the cursor position
    stabilizes, the CLI is at its input prompt.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "display-message",
            "-p",
            "-t",
            session_name,
            "#{cursor_x},#{cursor_y}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            parts = stdout.decode().strip().split(",")
            if len(parts) == 2:
                return (int(parts[0]), int(parts[1]))
    except (OSError, ValueError):
        pass
    return None


async def get_pane_command(session_name: str) -> str | None:
    """Get the start command of the active pane in a tmux session.

    Uses ``pane_start_command`` (the original launch command) instead of
    ``pane_current_command`` which may return a version string or subprocess
    name (e.g. ``2.1.81`` for Claude Code instead of ``claude``).

    Returns the full start command string or None on failure.
    Used to detect stale sessions where the AI CLI has exited and a shell remains.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-panes",
            "-t",
            session_name,
            "-F",
            "#{pane_start_command}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout.strip():
            return stdout.decode().strip().splitlines()[0]
    except (OSError, FileNotFoundError):
        pass
    return None


async def is_shell_fallback(session_name: str) -> bool:
    """Detect if a tmux session has fallen back to a bare shell.

    Returns True when the pane's current command is a known shell (zsh, bash,
    sh, fish, etc.), meaning the original command (e.g. Claude Code) has exited.

    Note: ``pane_current_command`` for Claude Code returns a version string
    like ``2.1.81`` rather than ``claude``, so we use a negative check
    (is it a shell?) instead of a positive check (is it claude?).
    """
    shells = {"zsh", "bash", "sh", "fish", "dash", "csh", "tcsh", "ksh", "login"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-panes",
            "-t",
            session_name,
            "-F",
            "#{pane_current_command}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout.strip():
            current_cmd = stdout.decode().strip().splitlines()[0].strip()
            # Also handle paths like /bin/zsh → extract basename
            cmd_name = current_cmd.rsplit("/", 1)[-1] if "/" in current_cmd else current_cmd
            return cmd_name in shells
    except (OSError, FileNotFoundError):
        pass
    return False


async def get_session_pid(session_name: str) -> int | None:
    """Get the PID of the process running in a tmux session."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-panes",
            "-t",
            session_name,
            "-F",
            "#{pane_pid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout.strip():
            return int(stdout.decode().strip().splitlines()[0])
    except (OSError, FileNotFoundError, ValueError):
        pass
    return None
