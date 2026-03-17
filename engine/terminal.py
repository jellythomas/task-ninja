"""Interactive terminal sessions via WebSocket + PTY."""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import pty
import select
import signal
import struct
import termios

logger = logging.getLogger(__name__)


class TerminalSession:
    """Manages a single PTY-backed terminal session."""

    def __init__(self, cwd: str, session_id: str) -> None:
        self.cwd = cwd
        self.session_id = session_id
        self.master_fd: int | None = None
        self.pid: int | None = None
        self._closed = False

    def start(self) -> None:
        """Fork a new PTY process running the user's shell."""
        shell = os.environ.get("SHELL", "/bin/zsh")
        pid, master_fd = pty.fork()

        if pid == 0:
            # Child process
            os.chdir(self.cwd)
            os.execvpe(shell, [shell, "-l"], os.environ)  # noqa: S606 — intentional exec in child process
        else:
            # Parent process
            self.pid = pid
            self.master_fd = master_fd
            # Set non-blocking
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def resize(self, rows: int, cols: int) -> None:
        """Resize the PTY."""
        if self.master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def write(self, data: bytes) -> None:
        """Write data to the PTY (user input)."""
        if self.master_fd is not None and not self._closed:
            os.write(self.master_fd, data)

    def read(self, size: int = 4096) -> bytes | None:
        """Read available data from the PTY (terminal output). Non-blocking."""
        if self.master_fd is None or self._closed:
            return None
        try:
            ready, _, _ = select.select([self.master_fd], [], [], 0)
            if ready:
                return os.read(self.master_fd, size)
        except (OSError, ValueError):
            self._closed = True
        return None

    def is_alive(self) -> bool:
        """Check if the PTY process is still running."""
        if self.pid is None:
            return False
        try:
            pid, _status = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except ChildProcessError:
            return False

    def close(self) -> None:
        """Close the PTY session."""
        self._closed = True
        if self.master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = None
        if self.pid is not None:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.kill(self.pid, signal.SIGHUP)
            self.pid = None


class TerminalManager:
    """Manages multiple terminal sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}

    def create_session(self, session_id: str, cwd: str) -> TerminalSession:
        """Create and start a new terminal session."""
        # Close existing session with same ID
        if session_id in self._sessions:
            self._sessions[session_id].close()

        session = TerminalSession(cwd, session_id)
        session.start()
        self._sessions[session_id] = session
        logger.info("Created session %s in %s", session_id, cwd)
        return session

    def get_session(self, session_id: str) -> TerminalSession | None:
        return self._sessions.get(session_id)

    def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session.close()
            logger.info("Closed session %s", session_id)

    def close_all(self) -> None:
        for sid in list(self._sessions.keys()):
            self.close_session(sid)
