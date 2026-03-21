"""CLI worker — spawns an AI agent process per ticket with PTY for live terminal access.

Supports two modes:
- **tmux mode** (default): Each viewer gets an independent tmux grouped session with its own
  terminal sizing. Enables simultaneous PC + phone viewing without garbled output.
- **raw PTY mode** (fallback): Single PTY shared by all viewers. Used when tmux is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import re
import select
import signal
import struct
import termios
import time

from dataclasses import dataclass
from pathlib import Path

from fastapi import WebSocket

from engine import tmux as tmux_mgr
from engine.broadcaster import Broadcaster
from engine.claude_helper import ClaudeHelper
from engine.jira_client import JiraClient
from engine.state import StateManager
from models.ticket import TicketState

logger = logging.getLogger(__name__)

# Cursor-forward sequences → replace with spaces (preserves word spacing)
_CURSOR_FWD_RE = re.compile(r"\x1b\[(\d+)C")
_CURSOR_FWD_1_RE = re.compile(r"\x1b\[C")
# Strip remaining ANSI escape codes for log parsing
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b\[\?[0-9;]*[hl]|\r")


def _clean_ansi(text: str) -> str:
    """Strip ANSI codes, converting cursor-forward to spaces."""
    text = _CURSOR_FWD_RE.sub(lambda m: " " * int(m.group(1)), text)
    text = _CURSOR_FWD_1_RE.sub(" ", text)
    return _ANSI_RE.sub("", text)


# Whether tmux is available (checked once at import time)
_TMUX_AVAILABLE: bool = tmux_mgr.is_available()

_viewer_counter: int = 0


def _next_viewer_id() -> int:
    global _viewer_counter
    _viewer_counter += 1
    return _viewer_counter


@dataclass
class ViewerSession:
    """Per-viewer tmux grouped session with its own PTY."""

    ws: WebSocket
    session_name: str
    master_fd: int
    pid: int
    read_task: asyncio.Task | None = None


class Worker:
    """Manages a single AI agent process for a ticket with PTY-backed terminal."""

    def __init__(
        self,
        ticket_id: str,
        run_id: str,
        jira_key: str,
        worktree_path: str,
        state_manager: StateManager,
        broadcaster: Broadcaster,
        claude_command: str = "claude",
        claude_flags: list[str] | None = None,
        jira_status_mapping: dict | None = None,
        pr_base_branch: str = "master",
        phases_config: list[dict] | None = None,
        idle_timeout: int = 10,
    ):
        self.ticket_id = ticket_id
        self.run_id = run_id
        self.jira_key = jira_key
        self.worktree_path = worktree_path
        self.state = state_manager
        self.broadcaster = broadcaster
        self.claude_command = claude_command
        self.claude_flags = claude_flags or []
        self.jira_status_mapping = jira_status_mapping or {}
        self.pr_base_branch = pr_base_branch
        self.claude_helper = ClaudeHelper(claude_command)
        self.jira_client: JiraClient | None = None
        self.process: asyncio.subprocess.Process | None = None
        self._cancelled = False

        # Phase pipeline config
        self.phases_config = phases_config  # None = legacy --print mode
        self.idle_timeout = idle_timeout
        self._current_phase: str | None = None
        self._phase_marker: str | None = None
        self._marker_detected = asyncio.Event()
        self._last_output_time: float = 0
        self._user_active: bool = False

        # PTY and viewer management
        self._master_fd: int | None = None
        self._viewers: set[WebSocket] = set()
        self._output_buffer = bytearray()  # Scrollback for late-joining viewers (raw PTY fallback)
        self._max_buffer = 256 * 1024  # 256KB
        self._line_buffer = ""  # Partial line accumulator for parsing

        # tmux multi-viewer support
        self._use_tmux: bool = _TMUX_AVAILABLE
        self._tmux_session: str = f"tn-{ticket_id}"
        self._viewer_sessions: dict[WebSocket, ViewerSession] = {}

    async def run(self) -> bool:
        """Execute the ticket using interactive mode with phase pipeline."""
        try:
            # Build command — interactive mode (no --print)
            cmd = [self.claude_command, *self.claude_flags]

            # Log
            cmd_str = " ".join(cmd)
            logger.info("Running (interactive): %s in %s", cmd_str, self.worktree_path)
            await self.state.append_log(self.ticket_id, f"[worker] $ {cmd_str}")
            await self.broadcaster.broadcast_log(self.run_id, self.ticket_id, f"[worker] $ {cmd_str}")
            await self.state.append_log(self.ticket_id, f"[worker] cwd: {self.worktree_path}")
            await self.broadcaster.broadcast_log(self.run_id, self.ticket_id, f"[worker] cwd: {self.worktree_path}")

            # Set up log file
            log_dir = Path(self.worktree_path).parent
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_file_path = log_dir / f"log-{self.jira_key.lower()}.txt"
            self._log_fh = open(self._log_file_path, "a", buffering=1)  # noqa: SIM115 — kept open for lifetime of worker

            # Spawn process — tmux mode or raw PTY fallback
            if self._use_tmux:
                # Create tmux session running the AI agent
                ok = await tmux_mgr.create_session(self._tmux_session, cmd, self.worktree_path)
                if not ok:
                    logger.warning("tmux session creation failed, falling back to raw PTY")
                    self._use_tmux = False

            if self._use_tmux:
                # Get PID of the process inside tmux
                session_pid = await tmux_mgr.get_session_pid(self._tmux_session)
                self._tmux_pid = session_pid

                # Create a monitoring PTY (for log parsing / phase detection only)
                monitor_session = f"{self._tmux_session}-monitor"
                await tmux_mgr.create_grouped_session(self._tmux_session, monitor_session)
                result = await tmux_mgr.attach_pty(monitor_session)
                if result:
                    self._master_fd, _ = result
                else:
                    logger.warning("tmux monitor attach failed, falling back to raw PTY")
                    await tmux_mgr.kill_session(self._tmux_session)
                    self._use_tmux = False

            if not self._use_tmux:
                # Raw PTY fallback (original behavior)
                master_fd, slave_fd = pty.openpty()
                self._master_fd = master_fd
                winsize = struct.pack("HHHH", 24, 80, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                self.process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=self.worktree_path,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
                )
                os.close(slave_fd)

            worker_pid = self._tmux_pid if self._use_tmux else (self.process.pid if self.process else None)
            await self.state.update_ticket(
                self.ticket_id,
                worker_pid=worker_pid,
                log_file=str(self._log_file_path),
            )
            await self.broadcaster.broadcast_ticket_update(
                self.run_id, self.ticket_id, None, worker_pid=worker_pid
            )

            # Start PTY read loop in background (task ref prevents GC)
            self._pty_task = asyncio.create_task(self._pty_read_loop())

            # Wait for Claude to be ready (give it a moment to start)
            await asyncio.sleep(3)

            # Execute phase pipeline
            phase_map = {
                "planning": TicketState.PLANNING,
                "developing": TicketState.DEVELOPING,
                "review": TicketState.REVIEW,
            }

            # Determine which phases to skip (resume from last completed)
            ticket = await self.state.get_ticket(self.ticket_id)
            last_completed = ticket.last_completed_phase if ticket else None
            phase_order = [p["phase"] for p in self.phases_config]
            skip_until_after = None
            if last_completed and last_completed in phase_order:
                skip_until_after = last_completed

            for phase_cfg in self.phases_config:
                if self._cancelled:
                    break

                phase_name = phase_cfg["phase"]
                prompts = phase_cfg.get("prompts", [])
                marker = phase_cfg.get("marker")
                ticket_state = phase_map.get(phase_name)

                if not prompts:
                    continue

                # Skip already-completed phases on retry
                if skip_until_after:
                    if phase_name == skip_until_after:
                        await self.state.append_log(
                            self.ticket_id, f"[worker] === Phase: {phase_name} (already completed, skipping) ==="
                        )
                        await self.broadcaster.broadcast_log(
                            self.run_id,
                            self.ticket_id,
                            f"[worker] === Phase: {phase_name} (already completed, skipping) ===",
                        )
                        skip_until_after = None  # Next phase will run
                        continue
                    await self.state.append_log(
                        self.ticket_id, f"[worker] === Phase: {phase_name} (already completed, skipping) ==="
                    )
                    await self.broadcaster.broadcast_log(
                        self.run_id,
                        self.ticket_id,
                        f"[worker] === Phase: {phase_name} (already completed, skipping) ===",
                    )
                    continue

                # Transition state
                if ticket_state:
                    await self.state.update_ticket_state(self.ticket_id, ticket_state)
                    await self.broadcaster.broadcast_ticket_update(self.run_id, self.ticket_id, ticket_state)
                    await self._sync_jira_status(phase_name)

                self._current_phase = phase_name
                self._phase_marker = marker

                await self.state.append_log(self.ticket_id, f"[worker] === Phase: {phase_name} ===")
                await self.broadcaster.broadcast_log(
                    self.run_id, self.ticket_id, f"[worker] === Phase: {phase_name} ==="
                )

                # Send all prompts for this phase as a single block
                full_prompt = "\n".join(
                    p.replace("{JIRA_KEY}", self.jira_key).replace("{PARENT_BRANCH}", self.pr_base_branch)
                    for p in prompts
                )
                self._send_to_pty(full_prompt + "\r")

                # Wait for phase completion
                completed = await self._wait_for_phase_completion(marker)

                if self._cancelled:
                    break

                if completed:
                    # Persist phase completion for resume on retry
                    await self.state.update_ticket(self.ticket_id, last_completed_phase=phase_name)
                elif self._process_has_exited():
                    # Process died during phase — always keep review/done state
                    # Review is the last phase; core work (planning+developing) is done.
                    # If /open-pr failed, user can see it in terminal logs and open PR manually.
                    ticket = await self.state.get_ticket(self.ticket_id)
                    current_state = ticket.state if ticket else None
                    exit_code = self._get_exit_code()
                    if current_state in (TicketState.REVIEW, TicketState.DONE):
                        logger.info(
                            "Process exited (code=%s) during %s, ticket in %s — keeping state",
                            exit_code,
                            phase_name,
                            current_state,
                        )
                        await self._notify_viewers_exit(exit_code)
                        return True
                    error = f"CLI exited with code {exit_code} during {phase_name}"
                    await self.state.update_ticket(self.ticket_id, error=error)
                    await self.state.update_ticket_state(self.ticket_id, TicketState.FAILED)
                    await self.broadcaster.broadcast_ticket_update(
                        self.run_id, self.ticket_id, TicketState.FAILED, error=error
                    )
                    await self._notify_viewers_exit(exit_code)
                    return False

            if self._cancelled:
                return False

            # All phases complete — keep session open for further interaction
            await self.state.append_log(
                self.ticket_id,
                "[worker] All phases complete — session stays open for further interaction",
            )
            await self.broadcaster.broadcast_log(
                self.run_id,
                self.ticket_id,
                "[worker] All phases complete — session stays open for further interaction",
            )

            # Wait for the process to exit naturally (user closes it or it's killed)
            if self._use_tmux:
                # Poll tmux session existence — asyncio.Event not applicable (external process)
                while await tmux_mgr.session_exists(self._tmux_session):  # noqa: ASYNC110
                    await asyncio.sleep(1)
            elif self.process:
                await self.process.wait()

            # Drain any remaining PTY data
            await self._drain_pty()

            await self._notify_viewers_exit(0)
            return True

        except Exception as e:
            if self._cancelled:
                return False  # Killed intentionally — don't overwrite state
            # Don't overwrite review/done state with FAILED (phase pipeline path)
            ticket = await self.state.get_ticket(self.ticket_id)
            if ticket and ticket.state in (TicketState.REVIEW, TicketState.DONE):
                logger.info("Exception during %s — keeping state: %s", ticket.state, e)
                return True
            error = str(e)
            await self.state.update_ticket(self.ticket_id, error=error)
            with contextlib.suppress(ValueError):
                await self.state.update_ticket_state(self.ticket_id, TicketState.FAILED)
            with contextlib.suppress(RuntimeError, OSError):
                await self.broadcaster.broadcast_ticket_update(
                    self.run_id, self.ticket_id, TicketState.FAILED, error=error
                )
            return False
        finally:
            # Kill process / tmux session
            if self._use_tmux:
                await tmux_mgr.kill_session(self._tmux_session)
                # Close all viewer sessions
                for vs in list(self._viewer_sessions.values()):
                    if vs.read_task:
                        vs.read_task.cancel()
                    with contextlib.suppress(OSError):
                        os.close(vs.master_fd)
                self._viewer_sessions.clear()
            elif self.process and self.process.returncode is None:
                try:
                    self.process.send_signal(signal.SIGTERM)
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        self.process.kill()
                        await self.process.wait()
                except ProcessLookupError:
                    pass
            self._close_pty()
            if hasattr(self, "_log_fh") and self._log_fh:
                self._log_fh.close()
            if not self._cancelled:
                await self.state.update_ticket(self.ticket_id, worker_pid=None)

    async def kill(self) -> None:
        """Kill the worker process (or tmux session)."""
        self._cancelled = True
        if self._use_tmux:
            await tmux_mgr.kill_session(self._tmux_session)
            for vs in list(self._viewer_sessions.values()):
                if vs.read_task:
                    vs.read_task.cancel()
                with contextlib.suppress(OSError):
                    os.close(vs.master_fd)
            self._viewer_sessions.clear()
        elif self.process and self.process.returncode is None:
            try:
                self.process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            except ProcessLookupError:
                pass
        self._close_pty()

    # --- PTY I/O ---

    async def _pty_read_loop(self) -> None:
        """Read from PTY master, forward to viewers, parse for logs/state.

        Uses run_in_executor for blocking I/O to avoid starving the event loop
        when multiple workers run concurrently. Coalesces available data per
        frame (~30fps) so xterm.js receives complete escape sequences.
        """
        frame_interval = 0.033  # ~30fps
        loop = asyncio.get_event_loop()

        def _read_batch():
            """Blocking I/O — runs in thread executor to keep event loop free."""
            batch = bytearray()
            while True:
                ready, _, _ = select.select([self._master_fd], [], [], 0)
                if not ready:
                    break
                try:
                    chunk = os.read(self._master_fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                batch.extend(chunk)
            return bytes(batch) if batch else None

        while not self._cancelled:
            if not self._use_tmux and self.process and self.process.returncode is not None:
                break
            if self._use_tmux and not await tmux_mgr.session_exists(self._tmux_session):
                break

            try:
                # Non-blocking wait for data, then read batch in executor
                ready = await loop.run_in_executor(
                    None, lambda: select.select([self._master_fd], [], [], frame_interval)[0]
                )
                if not ready:
                    if not self._use_tmux and self.process and self.process.returncode is not None:
                        break
                    continue

                data = await loop.run_in_executor(None, _read_batch)
                if not data:
                    continue

                # Store in scrollback buffer
                self._output_buffer.extend(data)
                if len(self._output_buffer) > self._max_buffer:
                    self._output_buffer = self._output_buffer[-self._max_buffer :]

                # Forward entire batch to viewers
                with contextlib.suppress(RuntimeError, OSError):
                    await self._send_to_viewers(data)  # Never let viewer errors kill the read loop

                # Write to log file
                if hasattr(self, "_log_fh") and self._log_fh:
                    self._log_fh.write(data.decode("utf-8", errors="replace"))
                    self._log_fh.flush()

                # Parse for state transitions, PR URLs, and log broadcasting
                text = data.decode("utf-8", errors="replace")
                await self._process_output(text)
            except (OSError, ValueError):
                break

    async def _drain_pty(self) -> None:
        """Read any remaining data from PTY after process exit."""
        if self._master_fd is None:
            return
        try:
            while True:
                ready, _, _ = select.select([self._master_fd], [], [], 0)
                if not ready:
                    break
                data = os.read(self._master_fd, 65536)
                if not data:
                    break
                self._output_buffer.extend(data)
                if len(self._output_buffer) > self._max_buffer:
                    self._output_buffer = self._output_buffer[-self._max_buffer :]
                with contextlib.suppress(RuntimeError, OSError):
                    await self._send_to_viewers(data)
                if hasattr(self, "_log_fh") and self._log_fh:
                    self._log_fh.write(data.decode("utf-8", errors="replace"))
                    self._log_fh.flush()
                text = data.decode("utf-8", errors="replace")
                await self._process_output(text)
        except OSError:
            pass

    async def _send_to_viewers(self, data: bytes) -> None:
        """Send raw PTY output to all connected WebSocket viewers in parallel.

        In tmux mode, each viewer has its own read loop — this only sends to
        raw PTY fallback viewers. tmux viewers are handled by _viewer_read_loop.
        """
        if not self._viewers:
            return
        viewers = list(self._viewers)
        results = await asyncio.gather(
            *(ws.send_bytes(data) for ws in viewers),
            return_exceptions=True,
        )
        disconnected = {ws for ws, r in zip(viewers, results, strict=False) if isinstance(r, Exception)}
        if disconnected:
            self._viewers -= disconnected

    async def _notify_viewers_exit(self, code: int) -> None:
        """Notify viewers that the process has exited."""
        msg = json.dumps({"type": "process_exit", "code": code})
        disconnected = set()
        for ws in list(self._viewers):  # Copy to avoid "Set changed size during iteration"
            try:
                await ws.send_text(msg)
            except Exception:
                disconnected.add(ws)
        self._viewers -= disconnected

    def _close_pty(self) -> None:
        """Close the PTY master fd."""
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None

    # --- Viewer management (attach/detach) ---

    async def attach_viewer(self, ws: WebSocket, rows: int = 24, cols: int = 80) -> None:
        """Attach a WebSocket viewer.

        tmux mode: creates a grouped session with its own PTY for independent sizing.
        Raw PTY mode: adds to shared viewer set with scrollback replay.
        """
        if self._use_tmux:
            vid = _next_viewer_id()
            viewer_session_name = f"{self._tmux_session}-v{vid}"
            ok = await tmux_mgr.create_grouped_session(self._tmux_session, viewer_session_name, rows, cols)
            if not ok:
                # Fallback: add to raw viewers
                self._viewers.add(ws)
                return

            result = await tmux_mgr.attach_pty(viewer_session_name, rows, cols)
            if not result:
                await tmux_mgr.kill_session(viewer_session_name)
                self._viewers.add(ws)
                return

            master_fd, pid = result
            vs = ViewerSession(
                ws=ws,
                session_name=viewer_session_name,
                master_fd=master_fd,
                pid=pid,
            )
            vs.read_task = asyncio.create_task(self._viewer_read_loop(vs))
            self._viewer_sessions[ws] = vs
        else:
            # Raw PTY fallback — send scrollback buffer
            if self._output_buffer:
                buf = bytes(self._output_buffer)
                chunk_size = 32768  # 32KB chunks to avoid blocking
                try:
                    for i in range(0, len(buf), chunk_size):
                        await ws.send_bytes(buf[i : i + chunk_size])
                except Exception:
                    return
            self._viewers.add(ws)

    def detach_viewer(self, ws: WebSocket) -> None:
        """Detach a WebSocket viewer. Does NOT affect the running process."""
        if ws in self._viewer_sessions:
            vs = self._viewer_sessions.pop(ws)
            if vs.read_task:
                vs.read_task.cancel()
            with contextlib.suppress(OSError):
                os.close(vs.master_fd)
            asyncio.create_task(tmux_mgr.kill_session(vs.session_name))  # noqa: RUF006 — fire-and-forget cleanup from sync method
        else:
            self._viewers.discard(ws)

    def write_input(self, data: bytes) -> None:
        """Write user input to the PTY (from a terminal viewer)."""
        if self._cancelled:
            return
        self._user_active = True

        if self._use_tmux:
            # In tmux mode, write to the main session's tmux — all grouped sessions see it
            # Find the viewer's own PTY fd for direct write (lower latency than send-keys)
            # Actually, any viewer's input goes to the shared tmux window, so we can
            # write to the monitor fd or any viewer fd — they all share the same pane
            if self._master_fd is not None:
                with contextlib.suppress(OSError):
                    os.write(self._master_fd, data)
        elif self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.write(self._master_fd, data)

    def write_input_from_viewer(self, ws: WebSocket, data: bytes) -> None:
        """Write user input from a specific viewer's PTY (tmux mode).

        Each viewer has their own PTY attached to a grouped session that shares
        the same tmux window — writing to any of them reaches the process.
        """
        if self._cancelled:
            return
        self._user_active = True

        vs = self._viewer_sessions.get(ws)
        if vs:
            with contextlib.suppress(OSError):
                os.write(vs.master_fd, data)
        elif self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.write(self._master_fd, data)

    def interrupt(self) -> bool:
        """Send Escape key to the PTY to interrupt Claude's current operation.
        This cancels the current tool call without killing the session."""
        if self._cancelled:
            return False
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, b"\x1b")
                self._user_active = True
                return True
            except OSError:
                pass
        return False

    def resize_pty(self, rows: int, cols: int) -> None:
        """Resize the PTY terminal (raw PTY mode only — shared by all viewers)."""
        if self._master_fd is not None:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    def resize_viewer_pty(self, ws: WebSocket, rows: int, cols: int) -> None:
        """Resize a specific viewer's PTY (tmux mode — independent per viewer)."""
        vs = self._viewer_sessions.get(ws)
        if vs:
            tmux_mgr.resize_pty(vs.master_fd, rows, cols)
        else:
            # Fallback to shared PTY resize
            self.resize_pty(rows, cols)

    async def _viewer_read_loop(self, vs: ViewerSession) -> None:
        """Read from a viewer's tmux grouped session PTY and forward to their WebSocket."""
        frame_interval = 0.033
        loop = asyncio.get_event_loop()

        def _read_batch():
            batch = bytearray()
            while True:
                ready, _, _ = select.select([vs.master_fd], [], [], 0)
                if not ready:
                    break
                try:
                    chunk = os.read(vs.master_fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                batch.extend(chunk)
            return bytes(batch) if batch else None

        try:
            while not self._cancelled:
                if not await tmux_mgr.session_exists(vs.session_name):
                    break
                try:
                    ready = await loop.run_in_executor(
                        None, lambda: select.select([vs.master_fd], [], [], frame_interval)[0]
                    )
                    if not ready:
                        continue
                    data = await loop.run_in_executor(None, _read_batch)
                    if not data:
                        continue
                    await vs.ws.send_bytes(data)
                except (OSError, ValueError):
                    break
                except Exception:
                    break
        finally:
            # Notify viewer that session ended
            with contextlib.suppress(Exception):
                await vs.ws.send_text(json.dumps({"type": "process_exit", "code": 0}))

    @property
    def is_running(self) -> bool:
        """Check if the worker process is still running."""
        if self._use_tmux:
            # Synchronous check — for async use session_exists directly
            return not self._cancelled
        return self.process is not None and self.process.returncode is None

    def _process_has_exited(self) -> bool:
        """Check if the underlying process has exited (works for both modes)."""
        if self._use_tmux:
            return self._cancelled
        return self.process is not None and self.process.returncode is not None

    def _get_exit_code(self) -> int:
        """Get exit code of the process (0 if unknown/tmux)."""
        if self._use_tmux:
            return 0
        if self.process and self.process.returncode is not None:
            return self.process.returncode
        return -1

    # --- PTY send helper ---

    def _send_to_pty(self, text: str) -> None:
        """Write a command/prompt to the PTY (or tmux session)."""
        if self._cancelled:
            return
        if self._use_tmux:
            # Send via tmux send-keys to the main session
            # Strip trailing \r — tmux send-keys adds Enter
            clean = text.rstrip("\r\n")
            if clean:
                asyncio.create_task(tmux_mgr.send_keys(self._tmux_session, clean))  # noqa: RUF006 — fire-and-forget from sync method
        elif self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.write(self._master_fd, text.encode())

    # --- Phase completion ---

    async def _wait_for_phase_completion(self, marker: str | None) -> bool:
        """Wait for phase to complete via marker detection or idle debounce.

        Returns True if phase completed, False if process died.
        """
        self._marker_detected.clear()
        self._last_output_time = time.time()

        while not self._cancelled:
            # Process died — check via tmux or subprocess
            if self._use_tmux:
                if not await tmux_mgr.session_exists(self._tmux_session):
                    return False
            elif self.process and self.process.returncode is not None:
                return False

            # Marker detected
            if marker and self._marker_detected.is_set():
                return True

            # Idle debounce fallback (only if no marker configured)
            if not marker and self.idle_timeout > 0:
                idle_duration = time.time() - self._last_output_time
                if idle_duration >= self.idle_timeout and not self._user_active:
                    return True

            # Reset user_active flag after checking
            self._user_active = False

            await asyncio.sleep(0.5)

        return False

    # --- Output parsing ---

    async def _process_output(self, text: str) -> None:
        """Parse PTY output for markers, PR URLs, and log broadcasting."""
        self._last_output_time = time.time()

        self._line_buffer += text
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue

            # Strip ANSI codes for parsing
            clean = _clean_ansi(line)
            if not clean:
                continue

            # Interactive mode: skip PTY log broadcasting entirely.
            # The live terminal (xterm.js) handles full visual output.
            # Only structured worker events (phase transitions, errors, PR URLs) are broadcast.
            if self.phases_config:
                parsed = clean
            else:
                # Legacy (--print) mode: parse stream-json for structured log lines
                parsed = self._parse_stream_line(clean)
                if not parsed:
                    continue
                await self.state.append_log(self.ticket_id, parsed)
                await self.broadcaster.broadcast_log(self.run_id, self.ticket_id, parsed)

            # Check for phase marker
            if self._phase_marker and self._phase_marker in clean:
                self._marker_detected.set()

            # PR URL detection
            pr_url = self._extract_pr_url(parsed)
            if pr_url:
                await self.state.update_ticket(self.ticket_id, pr_url=pr_url)
                await self.broadcaster.broadcast_ticket_update(self.run_id, self.ticket_id, None, pr_url=pr_url)

    def _parse_stream_line(self, raw: str) -> str | None:
        """Parse a line from claude --output-format stream-json. Falls back to raw text."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw  # Not JSON — plain text mode, return as-is

        msg_type = data.get("type", "")

        # Assistant text content
        if msg_type == "assistant":
            content = data.get("message", {}).get("content", [])
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            return "\n".join(texts) if texts else None

        # Tool use — show what tool is being called
        if msg_type == "tool_use":
            tool = data.get("name", data.get("tool", "unknown"))
            inp = data.get("input", {})
            if tool == "Bash":
                cmd = inp.get("command", "")
                return f"[tool] Bash: {cmd[:200]}"
            if tool in ("Edit", "Write"):
                path = inp.get("file_path", "")
                return f"[tool] {tool}: {path}"
            if tool == "Read":
                path = inp.get("file_path", "")
                return f"[tool] Read: {path}"
            if tool == "Grep":
                pattern = inp.get("pattern", "")
                return f"[tool] Grep: {pattern}"
            return f"[tool] {tool}"

        # Tool result
        if msg_type == "tool_result":
            return None  # Skip verbose tool results

        # System/status messages
        if msg_type == "system":
            text = data.get("message", data.get("text", ""))
            if text:
                return f"[system] {text}"

        # Result message (final output)
        if msg_type == "result":
            text = data.get("result", "")
            if text:
                return text[:500]

        return None

    def _extract_pr_url(self, line: str) -> str | None:
        """Extract PR/pull-request URL from output line."""
        patterns = [
            r"(https?://bitbucket\.org/[^\s]+/pull-requests/\d+)",
            r"(https?://github\.com/[^\s]+/pull/\d+)",
            r"(https?://[^\s]*pull[_-]?request[^\s]*\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                return match.group(1)
        return None

    async def _sync_jira_status(self, board_state: str) -> None:
        """Sync ticket status to Jira based on board state mapping."""
        target = self.jira_status_mapping.get(board_state)
        if not target:
            return
        try:
            # Prefer direct API, fall back to Claude CLI
            if self.jira_client and await self.jira_client.is_configured():
                success = await self.jira_client.transition_issue(self.jira_key, target)
            else:
                success = await self.claude_helper.transition_jira_issue(self.jira_key, target)
            if success:
                logger.info("Synced %s -> %s on Jira", self.jira_key, target)
                await self.state.append_log(self.ticket_id, f"[jira] Transitioned to {target}")
        except (RuntimeError, ValueError, OSError) as e:
            logger.warning("Jira sync failed for %s: %s", self.jira_key, e)


class AdHocTerminal:
    """Lightweight interactive Claude session for review/done tickets.

    Spawns `claude --dangerously-skip-permissions` in the worktree with a PTY.
    No phase pipeline, no state transitions — just a live terminal.
    Exposes the same viewer interface as Worker so the WebSocket handler works.

    Supports tmux mode for multi-viewer independent sizing, with raw PTY fallback.
    """

    _adhoc_counter: int = 0

    def __init__(self, worktree_path: str, claude_command: str = "claude"):
        self.worktree_path = worktree_path
        self.claude_command = claude_command
        self.process: asyncio.subprocess.Process | None = None
        self._master_fd: int | None = None
        self._viewers: set = set()
        self._output_buffer = bytearray()
        self._max_buffer = 256 * 1024
        self._read_task: asyncio.Task | None = None

        # tmux support
        self._use_tmux: bool = _TMUX_AVAILABLE
        AdHocTerminal._adhoc_counter += 1
        self._tmux_session: str = f"tn-adhoc-{AdHocTerminal._adhoc_counter}"
        self._viewer_sessions: dict = {}

    async def start(self) -> None:
        """Spawn an interactive Claude session in the worktree."""
        import shlex

        parts = shlex.split(self.claude_command) if " " in self.claude_command else [self.claude_command]
        cmd = [*parts, "--dangerously-skip-permissions"]

        if self._use_tmux:
            ok = await tmux_mgr.create_session(self._tmux_session, cmd, self.worktree_path)
            if ok:
                session_pid = await tmux_mgr.get_session_pid(self._tmux_session)
                # Create a dummy process object for compatibility (pid tracking)
                self._tmux_pid = session_pid
                logger.info("Spawned tmux ad-hoc session %s in %s (pid=%s)", self._tmux_session, self.worktree_path, session_pid)
                return
            logger.warning("tmux ad-hoc session failed, falling back to raw PTY")
            self._use_tmux = False

        # Raw PTY fallback
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd

        winsize = struct.pack("HHHH", 24, 80, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.worktree_path,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        )
        os.close(slave_fd)

        self._read_task = asyncio.create_task(self._pty_read_loop())
        logger.info("Spawned raw PTY ad-hoc session in %s (pid=%s)", self.worktree_path, self.process.pid)

    async def _pty_read_loop(self) -> None:
        """Read from PTY and forward to viewers (raw PTY fallback only)."""
        loop = asyncio.get_event_loop()
        while True:
            try:
                ready = await loop.run_in_executor(None, lambda: select.select([self._master_fd], [], [], 0.5)[0])
                if not ready:
                    if self.process and self.process.returncode is not None:
                        break
                    continue
                data = os.read(self._master_fd, 65536)
                if not data:
                    break
                self._output_buffer.extend(data)
                if len(self._output_buffer) > self._max_buffer:
                    self._output_buffer = self._output_buffer[-self._max_buffer :]
                with contextlib.suppress(RuntimeError, OSError):
                    await self._send_to_viewers(data)
            except (OSError, ValueError):
                break

        # Notify viewers that process exited, then close PTY
        exit_msg = json.dumps({"type": "process_exit", "code": self.process.returncode if self.process else -1})
        for ws in list(self._viewers):
            with contextlib.suppress(Exception):
                await ws.send_text(exit_msg)
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None

    async def _send_to_viewers(self, data: bytes) -> None:
        if not self._viewers:
            return
        viewers = list(self._viewers)
        results = await asyncio.gather(
            *(ws.send_bytes(data) for ws in viewers),
            return_exceptions=True,
        )
        disconnected = {ws for ws, r in zip(viewers, results, strict=False) if isinstance(r, Exception)}
        if disconnected:
            self._viewers -= disconnected

    async def _viewer_read_loop(self, vs: ViewerSession) -> None:
        """Read from a viewer's tmux grouped session PTY and forward to their WebSocket."""
        frame_interval = 0.033
        loop = asyncio.get_event_loop()

        def _read_batch():
            batch = bytearray()
            while True:
                ready, _, _ = select.select([vs.master_fd], [], [], 0)
                if not ready:
                    break
                try:
                    chunk = os.read(vs.master_fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                batch.extend(chunk)
            return bytes(batch) if batch else None

        try:
            while True:
                if not await tmux_mgr.session_exists(vs.session_name):
                    break
                try:
                    ready = await loop.run_in_executor(
                        None, lambda: select.select([vs.master_fd], [], [], frame_interval)[0]
                    )
                    if not ready:
                        continue
                    data = await loop.run_in_executor(None, _read_batch)
                    if not data:
                        continue
                    await vs.ws.send_bytes(data)
                except (OSError, ValueError):
                    break
                except Exception:
                    break
        finally:
            with contextlib.suppress(Exception):
                await vs.ws.send_text(json.dumps({"type": "process_exit", "code": 0}))

    async def attach_viewer(self, ws, rows: int = 24, cols: int = 80) -> None:
        if self._use_tmux:
            vid = _next_viewer_id()
            viewer_session_name = f"{self._tmux_session}-v{vid}"
            ok = await tmux_mgr.create_grouped_session(self._tmux_session, viewer_session_name, rows, cols)
            if not ok:
                self._viewers.add(ws)
                return
            result = await tmux_mgr.attach_pty(viewer_session_name, rows, cols)
            if not result:
                await tmux_mgr.kill_session(viewer_session_name)
                self._viewers.add(ws)
                return
            master_fd, pid = result
            vs = ViewerSession(ws=ws, session_name=viewer_session_name, master_fd=master_fd, pid=pid)
            vs.read_task = asyncio.create_task(self._viewer_read_loop(vs))
            self._viewer_sessions[ws] = vs
        else:
            if self._output_buffer:
                buf = bytes(self._output_buffer)
                chunk_size = 32768
                try:
                    for i in range(0, len(buf), chunk_size):
                        await ws.send_bytes(buf[i : i + chunk_size])
                except Exception:
                    return
            self._viewers.add(ws)

    def detach_viewer(self, ws) -> None:
        if ws in self._viewer_sessions:
            vs = self._viewer_sessions.pop(ws)
            if vs.read_task:
                vs.read_task.cancel()
            with contextlib.suppress(OSError):
                os.close(vs.master_fd)
            asyncio.create_task(tmux_mgr.kill_session(vs.session_name))  # noqa: RUF006 — fire-and-forget cleanup from sync method
        else:
            self._viewers.discard(ws)

    def write_input(self, data: bytes) -> None:
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.write(self._master_fd, data)

    def write_input_from_viewer(self, ws, data: bytes) -> None:
        vs = self._viewer_sessions.get(ws)
        if vs:
            with contextlib.suppress(OSError):
                os.write(vs.master_fd, data)
        elif self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.write(self._master_fd, data)

    def resize_pty(self, rows: int, cols: int) -> None:
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)

    def resize_viewer_pty(self, ws, rows: int, cols: int) -> None:
        vs = self._viewer_sessions.get(ws)
        if vs:
            tmux_mgr.resize_pty(vs.master_fd, rows, cols)
        else:
            self.resize_pty(rows, cols)

    @property
    def is_running(self) -> bool:
        if self._use_tmux:
            return True  # Checked async via session_exists where needed
        return self.process is not None and self.process.returncode is None

    async def stop(self) -> None:
        """Terminate the session."""
        if self._use_tmux:
            await tmux_mgr.kill_session(self._tmux_session)
            for vs in list(self._viewer_sessions.values()):
                if vs.read_task:
                    vs.read_task.cancel()
                with contextlib.suppress(OSError):
                    os.close(vs.master_fd)
            self._viewer_sessions.clear()
        else:
            if self.process and self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.process.kill()
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None
        if self._read_task:
            self._read_task.cancel()
