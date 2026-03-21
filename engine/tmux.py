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
        "tmux", "new-session",
        "-d",                    # Detached
        "-s", session_name,      # Session name
        "-x", str(cols),         # Initial width
        "-y", str(rows),         # Initial height
        "--",                    # Separator
        *cmd,                    # Command to run
    ]

    # Configure session: hide status bar (viewers are web-based, not terminal clients)
    # and set window-size to latest (follow most recently active client)
    post_cmds = [
        ["tmux", "set-option", "-t", session_name, "status", "off"],
        ["tmux", "set-option", "-t", session_name, "window-size", "latest"],
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


async def create_grouped_session(
    target_session: str, viewer_session: str, rows: int = 24, cols: int = 80
) -> bool:
    """Create a grouped session that shares windows with the target but has independent sizing."""
    tmux_cmd = [
        "tmux", "new-session",
        "-d",                       # Detached
        "-t", target_session,       # Target session to group with
        "-s", viewer_session,       # New session name
        "-x", str(cols),            # Initial width (viewer's actual size)
        "-y", str(rows),            # Initial height (viewer's actual size)
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
            "tmux", "attach-session", "-t", session_name,
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
            "tmux", "kill-session", "-t", session_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        logger.info("Killed tmux session: %s", session_name)
    except (OSError, FileNotFoundError):
        pass


async def session_exists(session_name: str) -> bool:
    """Check if a tmux session exists."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", session_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except (OSError, FileNotFoundError):
        return False


async def list_sessions(prefix: str = SESSION_PREFIX) -> list[str]:
    """List all tmux sessions matching the prefix."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-sessions", "-F", "#{session_name}",
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


async def send_keys(session_name: str, keys: str) -> bool:
    """Send keys to a tmux session (for phase prompt injection)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, keys, "Enter",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except (OSError, FileNotFoundError):
        return False


async def get_session_pid(session_name: str) -> int | None:
    """Get the PID of the process running in a tmux session."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout.strip():
            return int(stdout.decode().strip().splitlines()[0])
    except (OSError, FileNotFoundError, ValueError):
        pass
    return None
