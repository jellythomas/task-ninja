"""Interactive terminal sessions via WebSocket + PTY."""

import asyncio
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
from typing import Optional

from fastapi import WebSocket


class TerminalSession:
    """Manages a single PTY-backed terminal session."""

    def __init__(self, cwd: str, session_id: str):
        self.cwd = cwd
        self.session_id = session_id
        self.master_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self._closed = False

    def start(self) -> None:
        """Fork a new PTY process running the user's shell."""
        shell = os.environ.get("SHELL", "/bin/zsh")
        pid, master_fd = pty.fork()

        if pid == 0:
            # Child process
            os.chdir(self.cwd)
            os.execvpe(shell, [shell, "-l"], os.environ)
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

    def read(self, size: int = 4096) -> Optional[bytes]:
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
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except ChildProcessError:
            return False

    def close(self) -> None:
        """Close the PTY session."""
        self._closed = True
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except (OSError, ProcessLookupError):
                pass
            self.pid = None


class TerminalManager:
    """Manages multiple terminal sessions."""

    def __init__(self):
        self._sessions: dict[str, TerminalSession] = {}

    def create_session(self, session_id: str, cwd: str) -> TerminalSession:
        """Create and start a new terminal session."""
        # Close existing session with same ID
        if session_id in self._sessions:
            self._sessions[session_id].close()

        session = TerminalSession(cwd, session_id)
        session.start()
        self._sessions[session_id] = session
        print(f"[terminal] Created session {session_id} in {cwd}", file=sys.stderr)
        return session

    def get_session(self, session_id: str) -> Optional[TerminalSession]:
        return self._sessions.get(session_id)

    def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session.close()
            print(f"[terminal] Closed session {session_id}", file=sys.stderr)

    def close_all(self) -> None:
        for sid in list(self._sessions.keys()):
            self.close_session(sid)


async def handle_terminal_websocket(ws: WebSocket, session: TerminalSession) -> None:
    """Bridge a WebSocket connection to a PTY session.

    Protocol:
    - Binary messages from client -> PTY input
    - JSON messages from client -> control (resize, ping)
    - Binary messages to client <- PTY output
    """
    import json

    async def read_pty():
        """Read from PTY and send to WebSocket."""
        while session.is_alive() and not session._closed:
            data = session.read()
            if data:
                try:
                    await ws.send_bytes(data)
                except Exception:
                    break
            else:
                await asyncio.sleep(0.02)  # 50fps max

    async def write_pty():
        """Read from WebSocket and write to PTY."""
        while session.is_alive() and not session._closed:
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=30)
                if message["type"] == "websocket.receive":
                    if "bytes" in message and message["bytes"]:
                        session.write(message["bytes"])
                    elif "text" in message and message["text"]:
                        try:
                            ctrl = json.loads(message["text"])
                            if ctrl.get("type") == "resize":
                                session.resize(ctrl.get("rows", 24), ctrl.get("cols", 80))
                            elif ctrl.get("type") == "ping":
                                await ws.send_text(json.dumps({"type": "pong"}))
                        except (json.JSONDecodeError, KeyError):
                            # Plain text input
                            session.write(message["text"].encode())
                elif message["type"] == "websocket.disconnect":
                    break
            except asyncio.TimeoutError:
                # Send keepalive
                if session.is_alive():
                    try:
                        await ws.send_text(json.dumps({"type": "pong"}))
                    except Exception:
                        break
                else:
                    break
            except Exception:
                break

    # Run both tasks concurrently
    reader = asyncio.create_task(read_pty())
    writer = asyncio.create_task(write_pty())

    try:
        done, pending = await asyncio.wait(
            [reader, writer], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    finally:
        session.close()
