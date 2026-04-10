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
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from fastapi import WebSocket

from engine import tmux as tmux_mgr
from engine.broadcaster import Broadcaster
from engine.claude_helper import ClaudeHelper
from engine.jira_client import JiraClient
from engine.state import StateManager
from models.ticket import Ticket, TicketState

logger = logging.getLogger(__name__)

# Cursor-forward sequences → replace with spaces (preserves word spacing)
_CURSOR_FWD_RE = re.compile(r"\x1b\[(\d+)C")
_CURSOR_FWD_1_RE = re.compile(r"\x1b\[C")
# Strip remaining ANSI escape codes for log parsing
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b\[\?[0-9;]*[hl]|\r")
_COPILOT_PENDING_STATUS_RE = re.compile(r"^[\s│├└╰╭╮╯]*\[pending\]\s*$", re.IGNORECASE)

DETERMINISTIC_PROMPT_SUBMISSION_ERROR_PREFIX = "Interactive prompt submission failed:"
VIEWER_TERMINAL_ENDED_CLOSE_CODE = 4101
VIEWER_TERMINAL_ENDED_CLOSE_REASON = "viewer terminal ended"


class DeterministicPromptSubmissionError(RuntimeError):
    """Prompt submission failed deterministically after the inline handshake retry."""


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
        self._max_log_file_bytes = 10 * 1024 * 1024  # 10MB cap for raw log files

        # tmux multi-viewer support
        self._use_tmux: bool = _TMUX_AVAILABLE
        self._tmux_session: str = f"tn-{ticket_id}"
        self._tmux_target: str | None = None
        self._viewer_sessions: dict[WebSocket, ViewerSession] = {}
        self._phase_submit_in_progress = False
        self._last_phase_prompt: str | None = None
        self._phase_resubmitted = False

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
            # On resume, the tmux session may already exist (from a previous worker).
            # Reuse it instead of creating a new Claude Code instance.
            self._resumed_session = False
            if self._use_tmux:
                if await tmux_mgr.session_exists(self._tmux_session):
                    # Verify the AI CLI is actually running, not a stale shell
                    pane_cmd = await tmux_mgr.get_pane_command(self._tmux_session)
                    if pane_cmd and self.claude_command in pane_cmd:
                        logger.info(
                            "Reusing existing tmux session %s (resume, running %s)", self._tmux_session, pane_cmd
                        )
                        self._resumed_session = True
                    else:
                        logger.warning(
                            "Stale tmux session %s found (running %s, expected %s) — recreating",
                            self._tmux_session,
                            pane_cmd,
                            self.claude_command,
                        )
                        await tmux_mgr.kill_session(self._tmux_session)
                        ok = await tmux_mgr.create_session(self._tmux_session, cmd, self.worktree_path)
                        if not ok:
                            logger.warning("tmux session creation failed, falling back to raw PTY")
                            self._use_tmux = False
                else:
                    ok = await tmux_mgr.create_session(self._tmux_session, cmd, self.worktree_path)
                    if not ok:
                        logger.warning("tmux session creation failed, falling back to raw PTY")
                        self._use_tmux = False

            # Verify CLI actually started inside new tmux session
            if self._use_tmux and not self._resumed_session:
                await asyncio.sleep(1.5)
                pane_cmd = await tmux_mgr.get_pane_command(self._tmux_session)
                expected_bin = self.claude_command.split("/")[-1]
                if pane_cmd and expected_bin not in pane_cmd:
                    logger.warning(
                        "Session %s: CLI failed to start (pane=%s) — recreating once",
                        self._tmux_session,
                        pane_cmd,
                    )
                    await tmux_mgr.kill_session(self._tmux_session)
                    ok = await tmux_mgr.create_session(self._tmux_session, cmd, self.worktree_path)
                    if ok:
                        await asyncio.sleep(2)
                        pane_cmd = await tmux_mgr.get_pane_command(self._tmux_session)
                        if pane_cmd and expected_bin not in pane_cmd:
                            logger.error("Session %s: CLI failed twice, falling back to raw PTY", self._tmux_session)
                            await tmux_mgr.kill_session(self._tmux_session)
                            self._use_tmux = False
                    else:
                        self._use_tmux = False

            if self._use_tmux:
                # Get PID of the process inside tmux
                session_pid = await tmux_mgr.get_session_pid(self._tmux_session)
                self._tmux_pid = session_pid
                self._tmux_target = await tmux_mgr.get_primary_pane_id(self._tmux_session) or self._tmux_session

                # Create (or recreate) a monitoring PTY for log parsing / phase detection
                monitor_session = f"{self._tmux_session}-monitor"
                if await tmux_mgr.session_exists(monitor_session):
                    # Kill stale monitor from previous worker run
                    await tmux_mgr.kill_session(monitor_session)
                await tmux_mgr.create_grouped_session(self._tmux_session, monitor_session)
                result = await tmux_mgr.attach_pty(monitor_session)
                if result:
                    self._master_fd, _ = result
                else:
                    logger.warning("tmux monitor attach failed, falling back to raw PTY")
                    if not self._resumed_session:
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
            await self.broadcaster.broadcast_ticket_update(self.run_id, self.ticket_id, None, worker_pid=worker_pid)

            # Start PTY read loop in background (task ref prevents GC)
            self._pty_task = asyncio.create_task(self._pty_read_loop())

            # Wait for the CLI to fully initialize (MCP servers, configs, etc.)
            # Uses cursor position stability — output-based detection is
            # unreliable for CLIs with animations (e.g. Copilot's blinking logo).
            await self._wait_for_startup_ready()

            # Execute phase pipeline
            phase_map = {
                "planning": TicketState.PLANNING,
                "developing": TicketState.DEVELOPING,
                "review": TicketState.REVIEW,
            }

            # Determine which phases to skip (resume from last completed)
            ticket = await self.state.get_ticket(self.ticket_id)
            last_completed = ticket.last_completed_phase if ticket else None
            phase_order = [p.get("phase") for p in self.phases_config if p.get("phase")]
            if not phase_order:
                logger.error("No valid phases in phases_config — skipping pipeline")
                return False
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

                self._current_phase = phase_name
                self._phase_marker = marker

                await self.state.append_log(self.ticket_id, f"[worker] === Phase: {phase_name} ===")
                await self.broadcaster.broadcast_log(
                    self.run_id, self.ticket_id, f"[worker] === Phase: {phase_name} ==="
                )

                # Wait for Claude to be truly idle before injecting the next prompt.
                # A Stop hook may cause Claude to continue processing (e.g. code review)
                # after the phase marker is emitted — a fixed sleep is not enough.
                if phase_name != phase_order[0]:
                    await self._wait_for_idle(min_quiet=10, timeout=600)

                full_prompt = self._build_phase_prompt(prompts, marker)

                self._last_phase_prompt = full_prompt
                self._phase_resubmitted = False  # Reset per phase
                try:
                    await self._submit_phase_prompt(full_prompt)
                except DeterministicPromptSubmissionError as exc:
                    return await self._handle_prompt_submission_failure(phase_name, exc)

                # Mark phase as STARTED (prompt injected)
                started_col = f"{phase_name}_started_at"
                await self.state.update_ticket(
                    self.ticket_id, **{started_col: datetime.now(tz=timezone.utc).isoformat()}
                )
                logger.info("Phase %s started for ticket %s", phase_name, self.ticket_id)

                # Transition state AFTER prompt is sent — not before.
                # If we set state to REVIEW before injection and the worker dies,
                # the orchestrator treats REVIEW as terminal and never retries.
                if ticket_state:
                    await self.state.update_ticket_state(self.ticket_id, ticket_state)
                    await self.broadcaster.broadcast_ticket_update(self.run_id, self.ticket_id, ticket_state)
                    await self._sync_jira_status(phase_name)

                # Wait for phase completion (marker detection)
                completed = await self._wait_for_phase_completion(marker)

                if self._cancelled:
                    break

                if completed:
                    # Mark phase as COMPLETED (marker detected) and persist for resume
                    completed_col = f"{phase_name}_completed_at"
                    await self.state.update_ticket(
                        self.ticket_id,
                        last_completed_phase=phase_name,
                        **{completed_col: datetime.now(tz=timezone.utc).isoformat()},
                    )
                    logger.info("Phase %s completed for ticket %s", phase_name, self.ticket_id)
                elif self._process_has_exited():
                    # Process died during phase — check if the phase actually completed
                    ticket = await self.state.get_ticket(self.ticket_id)
                    current_state = ticket.state if ticket else None
                    exit_code = self._get_exit_code()
                    last_done = ticket.last_completed_phase if ticket else None

                    if current_state == TicketState.DONE:
                        logger.info(
                            "Process exited (code=%s) during %s, ticket DONE — keeping state", exit_code, phase_name
                        )
                        await self._notify_viewers_exit(exit_code)
                        return True

                    # Check if the current phase completed using config-driven signals:
                    # 1. last_completed_phase matches phase name from profile config
                    # 2. {phase}_completed_at timestamp is set (marker detected)
                    # 3. pr_url is set for review phase (PR created, marker may have been missed)
                    phase_done = self._is_phase_completed(ticket, current_state)
                    if (
                        current_state in (TicketState.PLANNING, TicketState.DEVELOPING, TicketState.REVIEW)
                        and phase_done
                    ):
                        logger.info(
                            "Process exited (code=%s) during %s, phase completed — keeping state", exit_code, phase_name
                        )
                        await self._notify_viewers_exit(exit_code)
                        return True

                    if current_state == TicketState.REVIEW and not phase_done:
                        # Review started but never completed — re-queue for retry
                        logger.warning(
                            "Process exited (code=%s) during %s, review NOT completed (last_completed=%s) — re-queuing",
                            exit_code,
                            phase_name,
                            last_done,
                        )
                        await self.state.update_ticket_state(self.ticket_id, TicketState.QUEUED)
                        await self.broadcaster.broadcast_ticket_update(self.run_id, self.ticket_id, TicketState.QUEUED)
                        await self._notify_viewers_exit(exit_code)
                        return False

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
                # Kill monitor session (created for log parsing / phase detection)
                with contextlib.suppress(Exception):
                    await tmux_mgr.kill_session(f"{self._tmux_session}-monitor")
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
        # Cancel PTY read loop task
        if hasattr(self, "_pty_task") and self._pty_task and not self._pty_task.done():
            self._pty_task.cancel()
        if self._use_tmux:
            await tmux_mgr.kill_session(self._tmux_session)
            with contextlib.suppress(Exception):
                await tmux_mgr.kill_session(f"{self._tmux_session}-monitor")
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

        def _read_batch(fd: int):
            """Blocking I/O — runs in thread executor to keep event loop free."""
            batch = bytearray()
            while True:
                if self._master_fd != fd:
                    return None
                ready, _, _ = select.select([fd], [], [], 0)
                if self._master_fd != fd:
                    return None
                if not ready:
                    break
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if self._master_fd != fd:
                    return None
                if not chunk:
                    break
                batch.extend(chunk)
            return bytes(batch) if batch else None

        _session_check_interval = 5.0  # Check tmux session existence every 5s, not every frame
        _last_session_check = time.time()

        while not self._cancelled:
            if not self._use_tmux and self.process and self.process.returncode is not None:
                break
            if self._use_tmux and (time.time() - _last_session_check) > _session_check_interval:
                _last_session_check = time.time()
                if not await tmux_mgr.session_exists(self._tmux_session):
                    break

            try:
                # Non-blocking wait for data, then read batch in executor
                fd = self._master_fd
                if fd is None:
                    break
                ready = await loop.run_in_executor(None, lambda fd=fd: select.select([fd], [], [], frame_interval)[0])
                if self._master_fd != fd:
                    break
                if not ready:
                    if not self._use_tmux and self.process and self.process.returncode is not None:
                        break
                    continue

                data = await loop.run_in_executor(None, lambda fd=fd: _read_batch(fd))
                if self._master_fd != fd:
                    break
                if not data:
                    continue

                # Store in scrollback buffer
                self._output_buffer.extend(data)
                if len(self._output_buffer) > self._max_buffer:
                    self._output_buffer = self._output_buffer[-self._max_buffer :]

                # Forward entire batch to viewers
                with contextlib.suppress(RuntimeError, OSError):
                    await self._send_to_viewers(data)  # Never let viewer errors kill the read loop

                # Write to log file (capped at _max_log_file_bytes)
                if hasattr(self, "_log_fh") and self._log_fh:
                    if self._log_fh.tell() < self._max_log_file_bytes:
                        self._log_fh.write(data.decode("utf-8", errors="replace"))
                        self._log_fh.flush()

                # Parse for state transitions, PR URLs, and log broadcasting
                text = data.decode("utf-8", errors="replace")
                await self._process_output(text)
            except (OSError, ValueError) as exc:
                logger.warning("PTY read loop exiting due to %s: %s", type(exc).__name__, exc)
                break
            except Exception:
                logger.exception("PTY read loop unexpected error")
                break

        logger.info("PTY read loop ended for ticket %s (cancelled=%s)", self.ticket_id, self._cancelled)

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
                    if self._log_fh.tell() < self._max_log_file_bytes:
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
        fd = self._master_fd
        if fd is None:
            return
        self._master_fd = None
        with contextlib.suppress(OSError):
            os.close(fd)

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

    async def scroll_viewer_to_bottom(self, ws: WebSocket) -> None:
        """Exit tmux copy-mode and refresh the viewer to show live content."""
        if not self._use_tmux:
            return
        vs = self._viewer_sessions.get(ws)
        if not vs:
            return
        await tmux_mgr.cancel_copy_mode(vs.session_name)
        # Force the client to redraw with live pane content after exiting
        # copy-mode — without this, the viewer may still show stale output.
        await tmux_mgr.refresh_client(vs.session_name)

    async def refresh_viewer(self, ws: WebSocket) -> None:
        """Redraw a viewer's tmux client without sending input to the CLI process.

        Safe alternative to Ctrl-L (\\x0c) which passes through tmux to the
        running process — Copilot CLI interprets \\x0c as an interrupt.
        """
        if not self._use_tmux:
            return
        vs = self._viewer_sessions.get(ws)
        if not vs:
            return
        await tmux_mgr.refresh_client(vs.session_name)

    async def _close_viewer_session(self, vs: ViewerSession, code: int, reason: str) -> None:
        """Close a grouped viewer session and its WebSocket exactly once."""
        if self._viewer_sessions.get(vs.ws) is vs:
            self._viewer_sessions.pop(vs.ws, None)
        with contextlib.suppress(OSError):
            os.close(vs.master_fd)
        with contextlib.suppress(Exception):
            await tmux_mgr.kill_session(vs.session_name)
        with contextlib.suppress(Exception):
            await vs.ws.close(code=code, reason=reason)

    def write_input(self, data: bytes) -> None:
        """Write user input to the PTY (from a terminal viewer)."""
        if self._cancelled or self._should_ignore_viewer_input(data):
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
        if self._cancelled or self._should_ignore_viewer_input(data):
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
        frame_interval = 0.016  # ~60fps for smooth scrolling
        loop = asyncio.get_event_loop()
        close_viewer_session = False

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

        _session_check_interval = 5.0
        _last_session_check = time.time()

        try:
            while not self._cancelled:
                if (time.time() - _last_session_check) > _session_check_interval:
                    _last_session_check = time.time()
                    if not await tmux_mgr.session_exists(vs.session_name):
                        close_viewer_session = True
                        break
                try:
                    ready = await loop.run_in_executor(
                        None, lambda: select.select([vs.master_fd], [], [], frame_interval)[0]
                    )
                    if not ready:
                        continue
                    data = await loop.run_in_executor(None, _read_batch)
                    if not data:
                        close_viewer_session = True
                        break
                    await vs.ws.send_bytes(data)
                except (OSError, ValueError):
                    close_viewer_session = True
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    close_viewer_session = True
                    break
        finally:
            if close_viewer_session:
                await self._close_viewer_session(
                    vs,
                    code=VIEWER_TERMINAL_ENDED_CLOSE_CODE,
                    reason=VIEWER_TERMINAL_ENDED_CLOSE_REASON,
                )

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

    def _is_phase_completed(self, ticket: Ticket, phase_state: TicketState) -> bool:
        """Check if a ticket's current phase has completed using config-driven signals.

        Derives the phase name from the worker's phases_config (loaded from
        the agent profile) and checks multiple completion signals:
        1. last_completed_phase matches the phase name
        2. {phase}_completed_at timestamp is set (marker was detected)
        3. pr_url is set for review phase (PR created, marker may have been missed)
        """
        # Resolve phase name from config
        state_to_phase = {
            TicketState.PLANNING: "planning",
            TicketState.DEVELOPING: "developing",
            TicketState.REVIEW: "review",
        }
        phase_name = None
        target = state_to_phase.get(phase_state)
        if self.phases_config:
            for p in self.phases_config:
                if p.get("phase") == target:
                    phase_name = target
                    break
        if not phase_name:
            phase_name = phase_state.value

        if ticket.last_completed_phase == phase_name:
            return True

        completed_at = getattr(ticket, f"{phase_name}_completed_at", None)
        if completed_at:
            return True

        if phase_state == TicketState.REVIEW and ticket.pr_url:
            return True

        return False

    def _get_exit_code(self) -> int:
        """Get exit code of the process (0 if unknown/tmux)."""
        if self._use_tmux:
            return 0
        if self.process and self.process.returncode is not None:
            return self.process.returncode
        return -1

    # --- PTY send helper ---

    @property
    def _use_csi_u(self) -> bool:
        """Use CSI u Shift+Enter for multiline input (proven for Claude Code).

        Falls back to literal single-line for other CLIs (Copilot, Cursor, etc.).
        """
        return self.claude_command in ("claude", "claude-code")

    def _normalize_prompt_for_submit(self, prompt: str) -> str:
        """Collapse multiline prompts into a single-line submission string."""
        return " ".join(prompt.split())

    def _build_phase_prompt(self, prompts: list[str], marker: str | None) -> str:
        """Build the prompt block sent for a phase.

        Slash commands are sent as-is. Their command files are expected to
        print the configured marker themselves, which matches the documented
        recommended setup and avoids leaving Copilot slash commands stuck in
        a pending state with extra inline instructions appended.
        """
        rendered_prompts = [
            p.replace("{JIRA_KEY}", self.jira_key).replace("{PARENT_BRANCH}", self.pr_base_branch) for p in prompts
        ]
        full_prompt = "\n".join(rendered_prompts)
        if marker and self._should_append_marker_instruction(rendered_prompts):
            full_prompt += f"\n\nWhen you are completely done, print exactly: {marker}"
        return full_prompt

    def _should_append_marker_instruction(self, prompts: list[str]) -> bool:
        """Return whether Task Ninja should append a runtime marker instruction."""
        non_empty_prompts = [prompt.strip() for prompt in prompts if prompt.strip()]
        if not non_empty_prompts:
            return False
        return not non_empty_prompts[0].startswith("/")

    def _submission_echo_prefix(self, prompt_text: str) -> str:
        """Derive a short visible prefix for pre-submit composer echo checks.

        Must be short enough to fit on ONE pane line.  With an 80-column pane
        and a CLI prompt prefix (e.g. Copilot's ``> ``), only ~76 chars of
        user text fit on the first line.  Using 40 chars gives ample margin
        for any CLI prefix and avoids false negatives from line-wrapping.
        """
        if prompt_text.startswith("/"):
            parts = prompt_text.split()
            return " ".join(parts[:3]) if len(parts) >= 3 else prompt_text
        return prompt_text[:40].strip()

    async def _capture_submission_state(self) -> str:
        """Capture the current visible pane state for prompt submission checks."""
        if not self._use_tmux:
            return ""
        tmux_target = self._tmux_target or self._tmux_session
        return await tmux_mgr.capture_pane(tmux_target, history_lines=0) or ""

    def _find_composed_input_line(
        self,
        pane_text: str,
        prompt_text: str,
        *,
        echo_text: str | None = None,
    ) -> str | None:
        """Return the most recent visible line containing the composed prompt echo."""
        probe_text = (echo_text or prompt_text).strip()
        if not probe_text:
            return None
        for line in reversed(pane_text.splitlines()):
            stripped = line.strip()
            if probe_text in stripped:
                return stripped
        return None

    async def _wait_for_prompt_echo(
        self,
        prompt_text: str,
        baseline: str,
        *,
        echo_text: str | None = None,
        timeout: float = 2.0,
    ) -> str | None:
        """Wait for the injected prompt echo to appear in the pane composer."""
        deadline = time.time() + timeout
        baseline_line = self._find_composed_input_line(baseline, prompt_text, echo_text=echo_text)
        while time.time() < deadline:
            pane_text = await self._capture_submission_state()
            composed_line = self._find_composed_input_line(pane_text, prompt_text, echo_text=echo_text)
            if composed_line and (composed_line != baseline_line or pane_text != baseline):
                return composed_line
            await asyncio.sleep(0.1)
        return None

    def _pane_tail_lines(self, pane_text: str, limit: int = 6) -> list[str]:
        """Return the last visible non-empty pane lines for prompt-state checks."""
        return [line.strip() for line in pane_text.splitlines() if line.strip()][-limit:]

    def _line_looks_like_idle_prompt(self, line: str) -> bool:
        """Check whether a pane line belongs to the CLI's idle prompt chrome."""
        stripped = line.strip()
        if not stripped:
            return False
        lowered = stripped.lower()
        if any(phrase in lowered for phrase in self._KNOWN_IDLE_PHRASES):
            return True

        prompt_chars = (">", "\u276f", "$", "%")
        if any(stripped.startswith(pc) for pc in prompt_chars):
            rest = stripped.lstrip(">\u276f$%").strip()
            return len(rest) <= 2
        return False

    def _line_looks_like_non_progress_status(self, line: str) -> bool:
        """Ignore static status chrome that should not count as submit progress."""
        lowered = line.strip().lower()
        return (
            lowered.startswith("!")
            or lowered.startswith("● environment loaded")
            or lowered.startswith("● mcp servers reloaded")
            or _COPILOT_PENDING_STATUS_RE.match(line) is not None
        )

    def _pane_has_fresh_submission_output(
        self, pane_text: str, baseline: str, prompt_text: str, composed_line: str
    ) -> bool:
        """Check whether the pane produced new non-composer output after Enter."""
        baseline_lines = {line.strip() for line in baseline.splitlines() if line.strip()}
        for line in pane_text.splitlines():
            stripped = line.strip()
            if (
                not stripped
                or stripped in baseline_lines
                or stripped == composed_line
                or prompt_text in stripped
                or self._line_looks_like_idle_prompt(stripped)
                or self._line_looks_like_non_progress_status(stripped)
            ):
                continue
            return True
        return False

    # Phrases that appear on-screen ONLY when a CLI is actively running a
    # tool / command.  Checked BEFORE idle phrases to prevent false positives
    # (e.g. Copilot shows "Type @ to mention" at the bottom while running).
    _KNOWN_BUSY_PHRASES: ClassVar[tuple[str, ...]] = (
        "esc to cancel",
        "esc to interrupt",
        "running agent",
        "running tool",
        "running command",
    )

    def _pane_looks_idle(self, pane_text: str) -> bool:
        """Check whether the visible pane tail still matches an idle CLI prompt."""
        lines = [ln for ln in pane_text.splitlines() if ln.strip()]
        if not lines:
            return False

        # Use a wider window for busy PHRASE detection — Copilot renders
        # busy status (e.g. "Esc to cancel") above the always-visible
        # prompt chrome.  Single-char spinners stay in the narrow window
        # because Copilot reuses ● for completed items throughout output.
        wide_tail = lines[-10:]
        wide_lower = "\n".join(ln.strip() for ln in wide_tail).lower()

        # 1. Check busy phrases FIRST (wide window) — these override idle
        #    phrases because Copilot always renders idle-looking prompt
        #    chrome at the bottom even while actively running operations.
        for phrase in self._KNOWN_BUSY_PHRASES:
            if phrase in wide_lower:
                logger.debug("CLI busy detected (known phrase '%s') — NOT idle", phrase)
                return False

        tail = lines[-5:]

        # 2. Check spinner indicators in narrow window (last 2 lines only).
        #    Must stay narrow: Copilot uses ● for completed items throughout
        #    its output, so a wide check would never detect idle.
        for line in tail[-2:]:
            stripped = line.strip()
            if any(ind in stripped for ind in self._BUSY_INDICATORS):
                return False

        # 3. Now check idle phrases
        tail_lower = "\n".join(ln.strip() for ln in tail).lower()
        for phrase in self._KNOWN_IDLE_PHRASES:
            if phrase in tail_lower:
                logger.debug("CLI prompt detected (known phrase '%s')", phrase)
                return True

        prompt_chars = (">", "\u276f", "$", "%")
        for line in reversed(tail):
            stripped = line.strip()
            if any(stripped.startswith(pc) for pc in prompt_chars):
                rest = stripped.lstrip(">\u276f$%").strip()
                if len(rest) <= 2:
                    logger.debug("CLI prompt detected (bare): %s", stripped[:40])
                    return True

        return False

    def _pane_has_positive_submit_signal(
        self,
        pane_text: str,
        baseline_text: str,
        prompt_text: str,
        composed_line: str | None,
    ) -> bool:
        """Require positive post-Enter progress before counting submission as accepted."""
        tail = self._pane_tail_lines(pane_text)
        if any(any(indicator in line for indicator in self._BUSY_INDICATORS) for line in tail[-2:]):
            return True

        return self._pane_has_fresh_submission_output(
            pane_text,
            baseline_text,
            prompt_text,
            composed_line or "",
        )

    def _pane_shows_prompt_processed(self, pane_text: str) -> bool:
        """Check if the pane shows our submitted prompt was printed and processed.

        Looks for the prompt echo in the pane and checks for work output
        after it (tool calls, thinking text, skill invocations).  If found,
        the CLI received and processed our prompt — it is NOT idle, just
        showing idle chrome (e.g. Copilot always shows "type @ to mention").

        Returns False if the prompt echo isn't found or there's no work
        output after it (likely discarded by MCP reload).
        """
        prompt = self._last_phase_prompt
        if not prompt:
            return False

        # Use the first 40 chars of the prompt as an echo fingerprint
        # (full prompt may wrap across pane lines)
        echo_prefix = prompt[:40]
        lines = pane_text.splitlines()

        echo_idx = None
        for i, line in enumerate(lines):
            if echo_prefix in line:
                echo_idx = i
                break

        if echo_idx is None:
            return False  # Prompt not in pane — may have been discarded

        # Check for work output after the echo line
        for line in lines[echo_idx + 1 :]:
            stripped = line.strip()
            if not stripped:
                continue
            if self._line_looks_like_idle_prompt(stripped):
                continue
            if self._line_looks_like_non_progress_status(stripped):
                continue
            # Found substantive content after prompt echo — CLI is working
            logger.debug(
                "Prompt processed — work output found after echo: %s",
                stripped[:60],
            )
            return True

        return False

    def _should_ignore_viewer_input(self, data: bytes) -> bool:
        """Ignore viewer-side redraw controls while worker-owned submit startup runs."""
        return self._phase_submit_in_progress and data == b"\x0c"

    async def _submit_phase_prompt(self, prompt: str) -> None:
        """Submit one phase prompt with tmux verification and a single retry."""
        normalized = self._normalize_prompt_for_submit(prompt)
        self._phase_submit_in_progress = True
        try:
            if not self._use_tmux:
                await self._send_to_pty(normalized + "\r")
                return

            tmux_target = self._tmux_target or self._tmux_session
            last_error = "Interactive prompt submission was not accepted by CLI"

            for attempt in (1, 2):
                used_csi_u_fallback = False
                baseline = await self._capture_submission_state()
                sent = await tmux_mgr.send_literal_text(tmux_target, normalized)
                if not sent and self._use_csi_u:
                    sent = await tmux_mgr.send_keys(tmux_target, normalized, use_csi_u=True)
                    used_csi_u_fallback = sent
                if not sent:
                    last_error = "Interactive prompt submission transport failed"
                else:
                    echo_text = self._submission_echo_prefix(normalized)
                    composed_line = (
                        None
                        if used_csi_u_fallback
                        else await self._wait_for_prompt_echo(
                            normalized,
                            baseline,
                            echo_text=echo_text,
                        )
                    )
                    submitted = bool(used_csi_u_fallback)
                    if composed_line or not used_csi_u_fallback:
                        # Dismiss autocomplete/suggestion dropdowns before
                        # submitting.  Copilot shows slash-command suggestions
                        # that intercept Enter — End moves the cursor to end
                        # of composed text, which implicitly closes dropdowns.
                        #
                        # When echo detection fails (composed_line is None),
                        # the text may still be in the input — long prompts
                        # wrap across pane lines, defeating line-by-line echo
                        # matching.  Send End+Enter as best-effort anyway;
                        # _verify_prompt_submitted will confirm acceptance.
                        if not composed_line:
                            logger.debug(
                                "Echo prefix not found (likely line-wrap) — sending End+Enter as best-effort submit"
                            )
                        await asyncio.sleep(0.15)
                        await tmux_mgr.send_key(tmux_target, "End")
                        await asyncio.sleep(0.1)
                        submitted = await tmux_mgr.send_key(tmux_target, "Enter")
                    if submitted and await self._verify_prompt_submitted(
                        normalized,
                        composed_line=composed_line,
                        baseline=baseline,
                    ):
                        return
                    if not submitted:
                        last_error = "Interactive prompt submission was not accepted by CLI"

                if attempt == 1:
                    await self._wait_for_startup_ready(min_delay=1, stability_secs=1, timeout=10)

            raise DeterministicPromptSubmissionError(last_error)
        finally:
            self._phase_submit_in_progress = False

    async def _handle_prompt_submission_failure(self, phase_name: str, exc: Exception) -> bool:
        """Requeue once for deterministic prompt submission failures, then fail."""
        ticket = await self.state.get_ticket(self.ticket_id)
        count = ticket.prompt_submit_requeues if ticket else 0
        error = f"{DETERMINISTIC_PROMPT_SUBMISSION_ERROR_PREFIX} {exc}"

        logger.warning(
            "Prompt submission failed for ticket %s during %s (requeues=%d): %s",
            self.ticket_id,
            phase_name,
            count,
            exc,
        )

        _MAX_PROMPT_SUBMIT_REQUEUES = 2  # 3 total attempts (initial + 2 retries)
        if count < _MAX_PROMPT_SUBMIT_REQUEUES:
            await self.state.update_ticket_state(self.ticket_id, TicketState.QUEUED)
            await self.state.update_ticket(
                self.ticket_id,
                prompt_submit_requeues=count + 1,
                error=error,
            )
            await self.broadcaster.broadcast_ticket_update(
                self.run_id,
                self.ticket_id,
                TicketState.QUEUED,
                error=error,
            )
            return False

        await self.state.update_ticket(self.ticket_id, error=error)
        await self.state.update_ticket_state(self.ticket_id, TicketState.FAILED)
        await self.broadcaster.broadcast_ticket_update(
            self.run_id,
            self.ticket_id,
            TicketState.FAILED,
            error=error,
        )
        return False

    async def _send_to_pty(self, text: str) -> None:
        """Write a command/prompt to the PTY (or tmux session)."""
        if self._cancelled:
            return
        if self._use_tmux:
            # Strip trailing \r — tmux send-keys adds Enter
            clean = text.rstrip("\r\n")
            tmux_target = self._tmux_target or self._tmux_session
            if clean:
                ok = await tmux_mgr.send_keys(
                    tmux_target,
                    clean,
                    use_csi_u=self._use_csi_u,
                )
                if not ok:
                    logger.error("send_keys failed for target %s, retrying...", tmux_target)
                    await asyncio.sleep(2)
                    ok = await tmux_mgr.send_keys(
                        tmux_target,
                        clean,
                        use_csi_u=self._use_csi_u,
                    )
                    if not ok:
                        logger.error("send_keys retry also failed for target %s", tmux_target)
                else:
                    method = "CSI-u" if self._use_csi_u else "literal"
                    logger.info("send_keys OK (%s): %s → %s", method, tmux_target, clean[:80])
        elif self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.write(self._master_fd, text.encode())

    # --- Phase completion ---

    async def _wait_for_phase_completion(self, marker: str | None) -> bool:
        """Wait for phase to complete via marker detection or idle debounce.

        Returns True if phase completed, False if process died.

        Includes a fallback: if the PTY read loop dies (monitor session PTY
        disconnects during long tasks), periodically use ``tmux capture-pane``
        to scan recent output for the marker.  Also attempts to reconnect the
        monitor PTY so subsequent phases work normally.
        """
        self._marker_detected.clear()
        self._last_output_time = time.time()
        _capture_interval = 2.0  # seconds between capture-pane marker checks
        _last_capture_check = time.time()  # delay first check by _capture_interval
        _idle_streak = 0  # consecutive idle-at-prompt checks (for abort recovery)
        _IDLE_RESUBMIT_STREAK = 5  # ~10s at 2s intervals before re-submitting

        # Count lines containing the marker.  Uses substring match (not strict
        # equality) because CLIs prefix output differently — Copilot uses
        # status bullets (● ○ ◉), Claude prints bare text, etc.
        #
        # Safe against false positives from the input echo ("...print exactly:
        # MARKER") because we capture _initial_marker_count at phase start
        # (which includes the echo) and only trigger when the count INCREASES.
        def _count_marker_lines(text: str) -> int:
            return sum(1 for line in text.splitlines() if marker in line)

        _initial_marker_count = 0
        if marker and self._use_tmux:
            initial_pane = await tmux_mgr.capture_pane(self._tmux_session)
            if initial_pane:
                _initial_marker_count = _count_marker_lines(initial_pane)
                if _initial_marker_count:
                    logger.info(
                        "Marker %s already in pane %d time(s) (stale) — will only match new occurrences",
                        marker,
                        _initial_marker_count,
                    )

        pane_text = None  # Shared across marker check + idle recovery
        while not self._cancelled:
            # Process died — check via tmux or subprocess
            if self._use_tmux:
                if not await tmux_mgr.session_exists(self._tmux_session):
                    return False
            elif self.process and self.process.returncode is not None:
                return False

            # Periodically scan capture-pane for the marker.
            # Input echo embeds the marker inside a sentence; Claude's real
            # output prints it alone — _count_marker_lines only matches the latter.
            if marker and self._use_tmux:
                now = time.time()
                if (now - _last_capture_check) >= _capture_interval:
                    _last_capture_check = now
                    pane_text = await tmux_mgr.capture_pane(self._tmux_session)
                    if pane_text and _count_marker_lines(pane_text) > _initial_marker_count:
                        logger.info(
                            "Marker %s detected (new occurrence in pane output)",
                            marker,
                        )
                        # Reconnect monitor PTY if it died
                        if self._pty_task is not None and self._pty_task.done():
                            await self._reconnect_monitor_pty()
                        return True

            # Idle debounce fallback — ONLY for marker-less phases.
            # When a marker IS configured, we rely exclusively on marker
            # detection (PTY read loop + capture-pane fallback above).
            # The idle timeout must NOT fire for marker phases because
            # Claude Code's TUI always renders the ">" prompt even while
            # agents are actively working, causing false idle detection.
            if not marker and self.idle_timeout > 0 and not self._user_active:
                idle_duration = time.time() - self._last_output_time
                if idle_duration >= self.idle_timeout:
                    return True

            # Recovery: if the CLI unexpectedly returns to idle during a
            # MARKER phase (e.g. Copilot aborted after MCP server reload),
            # re-submit the prompt ONCE.
            #
            # Strategy: piggyback on the existing 2s capture-pane cycle.
            # When the pane looks idle, increment a consecutive counter.
            # After _IDLE_RESUBMIT_STREAK consecutive idle checks (~10s),
            # re-submit.  This is reactive (no flat wait) yet resistant
            # to momentary prompt flashes.
            #
            # IMPORTANT: Copilot always shows idle phrases ("type @ to
            # mention") even while actively working.  To avoid false
            # resubmits, we also check whether the pane already contains
            # output from our submitted prompt.  If the prompt echo is
            # visible with work output after it, Copilot processed our
            # prompt and is working — skip the resubmit.
            #
            # Skip for Claude Code whose ">" prompt appears even while busy.
            # Guarded by _phase_resubmitted to prevent duplicate submissions.
            if (
                marker
                and self._use_tmux
                and not self._use_csi_u
                and not self._user_active
                and not self._phase_resubmitted
            ):
                # Reuse pane_text from marker check above (same loop iteration).
                # Falls back to a fresh capture if marker check didn't run this tick.
                idle_pane = pane_text
                if idle_pane is None:
                    idle_pane = await tmux_mgr.capture_pane(self._tmux_session, history_lines=0)
                if idle_pane and self._pane_looks_idle(idle_pane):
                    # Before counting idle, check if our prompt was already
                    # printed/processed — if yes, Copilot is working, not idle.
                    if self._pane_shows_prompt_processed(idle_pane):
                        _idle_streak = 0
                    else:
                        _idle_streak += 1
                else:
                    _idle_streak = 0

                if _idle_streak >= _IDLE_RESUBMIT_STREAK:
                    self._phase_resubmitted = True
                    logger.warning(
                        "CLI idle at prompt for %d consecutive checks during "
                        "marker phase — re-submitting prompt (likely MCP reload / abort)",
                        _idle_streak,
                    )
                    try:
                        prompt = self._last_phase_prompt or ""
                        if prompt:
                            await self._submit_phase_prompt(prompt)
                            self._last_output_time = time.time()
                            _idle_streak = 0
                    except DeterministicPromptSubmissionError as exc:
                        logger.error("Re-submission also failed: %s", exc)
                        return False

            # Reset user_active flag after checking
            self._user_active = False

            await asyncio.sleep(0.5)

        return False

    # Well-known phrases that appear on-screen ONLY when a CLI is idle at
    # its input prompt.  Checked case-insensitively against the last few
    # visible lines.  Adding new CLIs here is the cheapest way to support
    # them — no config, no migration.
    _KNOWN_IDLE_PHRASES: ClassVar[tuple[str, ...]] = (
        # Claude Code
        "bypass permissions",
        "auto-accept",
        # Copilot CLI
        "type @ to mention",
        "shift+tab switch mode",
        "remaining reqs",
        # Cursor / Windsurf / generic
        "press enter to send",
        "type a message",
        "type your message",
    )

    _BUSY_INDICATORS: ClassVar[tuple[str, ...]] = (
        "●",
        "◐",
        "◑",
        "◒",
        "◓",
        "⠋",
        "⠙",
        "⠹",
        "⠸",
        "⠼",
        "⠴",
        "⠦",
        "⠧",
        "⠇",
        "⠏",
    )

    async def _is_cli_at_prompt(self) -> bool:
        """Check if the CLI is idle at the input prompt via tmux.

        Uses a two-layer detection strategy that works across CLI tools:

        1. **Known idle phrases** — well-known strings that only appear when
           a CLI is waiting for input (e.g. "Type @ to mention" for Copilot,
           "bypass permissions" for Claude Code).
        2. **Bare prompt characters** — ``>``, angle prompt, ``$``, ``%`` with
           minimal trailing text.

        Returns False if busy indicators (spinners) are detected or if
        the screen doesn't match any known idle pattern.
        """
        if not self._use_tmux:
            return False
        pane_text = await tmux_mgr.capture_pane(self._tmux_session, history_lines=0)
        if not pane_text:
            return False

        return self._pane_looks_idle(pane_text)

    async def _verify_prompt_submitted(
        self,
        prompt_text: str,
        *,
        composed_line: str | None = None,
        baseline: str | None = None,
        timeout: float = 8.0,
    ) -> bool:
        """Verify prompt submission via pane-state transitions around the injected prompt."""
        if not self._use_tmux:
            return True  # Can't verify without tmux — assume OK
        baseline_text = baseline if baseline is not None else await self._capture_submission_state()
        current_composed_line = composed_line
        deadline = time.time() + timeout
        while time.time() < deadline:
            pane_text = await self._capture_submission_state()
            if self._pane_has_positive_submit_signal(
                pane_text,
                baseline_text,
                prompt_text,
                current_composed_line,
            ):
                logger.info("Prompt accepted — pane produced positive post-Enter progress")
                return True
            if current_composed_line is None:
                current_composed_line = self._find_composed_input_line(pane_text, prompt_text)
            if current_composed_line and current_composed_line not in pane_text:
                logger.debug("Prompt echo disappeared without post-Enter progress; waiting for positive signal")
            await asyncio.sleep(0.1)
        if current_composed_line and current_composed_line not in (baseline or ""):
            logger.warning("CLI still shows composed input after %.1fs — prompt was not accepted", timeout)
        else:
            logger.warning("CLI never showed a submit transition after %.1fs", timeout)
        return False

    async def _reconnect_monitor_pty(self) -> None:
        """Reconnect the monitor PTY and restart the read loop.

        Called when the fallback capture-pane detects a marker, indicating the
        original monitor PTY died.  This ensures subsequent phases can still
        detect markers via the normal PTY read path.
        """
        if not self._use_tmux:
            return

        # Close old fd
        self._close_pty()

        monitor_session = f"{self._tmux_session}-monitor"

        # Ensure monitor session exists (may have been destroyed)
        if not await tmux_mgr.session_exists(monitor_session):
            ok = await tmux_mgr.create_grouped_session(self._tmux_session, monitor_session)
            if not ok:
                logger.error("Failed to recreate monitor session %s", monitor_session)
                return

        result = await tmux_mgr.attach_pty(monitor_session)
        if result:
            self._master_fd, _ = result
            self._pty_task = asyncio.create_task(self._pty_read_loop())
            logger.info("Reconnected monitor PTY for %s", self._tmux_session)
        else:
            logger.error("Failed to reconnect monitor PTY for %s", self._tmux_session)

    async def _wait_for_startup_ready(self, min_delay: float = 5, stability_secs: int = 3, timeout: float = 60) -> None:
        """Wait for the CLI to finish initializing before first prompt.

        Uses **cursor position stability** — a universal signal that works
        for ANY CLI without parsing terminal content.  During loading, the
        cursor moves as status lines are rendered.  Once the CLI reaches its
        input prompt, the cursor settles at a fixed position.

        Even CLIs with animations (e.g. Copilot's blinking logo) return the
        cursor to the same resting position between frames.  Tested: cursor
        stays at (2,44) for Copilot despite continuous animation output.

        Falls back to *timeout* for raw PTY mode (no tmux).
        """
        # Minimum wait — gives CLI time to start rendering
        await asyncio.sleep(min_delay)

        if not self._use_tmux:
            # Raw PTY fallback — no cursor tracking available, use fixed delay
            logger.info("Raw PTY mode — waiting additional %.0fs for startup", timeout - min_delay)
            await asyncio.sleep(min(15, timeout - min_delay))
            return

        deadline = time.time() + (timeout - min_delay)
        last_pos = None
        stable_count = 0

        while not self._cancelled and time.time() < deadline:
            pos = await tmux_mgr.get_cursor_position(self._tmux_session)
            if pos is not None and pos == last_pos:
                stable_count += 1
                if stable_count >= stability_secs:
                    logger.info(
                        "Cursor stable at %s for %ds — checking input readiness",
                        pos,
                        stability_secs,
                    )
                    # Handle trust/permission dialogs first
                    await self._dismiss_startup_dialogs()
                    # Verify the CLI actually accepts keyboard input.
                    # Some CLIs (Copilot) flush stdin during terminal mode
                    # init, silently discarding buffered keystrokes.
                    if await self._probe_input_readiness():
                        # Guard against late MCP reload: Copilot may reload
                        # MCP servers AFTER the probe passes, flushing stdin
                        # and discarding the real prompt.  Wait for the
                        # reload to appear in the pane, then re-probe.
                        if not self._use_csi_u and await self._wait_for_mcp_reload(timeout=10):
                            if not await self._probe_input_readiness():
                                logger.debug("Re-probe after MCP reload failed — CLI not ready yet")
                                stable_count = 0
                                continue
                        return
                    # Probe failed — keep waiting
                    logger.debug("Input probe failed — CLI not ready yet")
                    stable_count = 0
            else:
                if pos != last_pos:
                    stable_count = 0
                last_pos = pos
            await asyncio.sleep(1)

        if self._cancelled:
            return
        logger.warning("Startup cursor-stability wait timed out after %.0fs — injecting prompt anyway", timeout)

    _PROBE_STR: ClassVar[str] = "~~PROBE~~"

    async def _probe_input_readiness(self) -> bool:
        """Send a test string and verify the CLI accepted it.

        Some CLIs (Copilot) flush stdin during terminal mode initialization,
        silently discarding buffered keystrokes even though tmux reports
        success (rc=0).  This method sends a unique probe string, checks if
        it appeared in the pane, then deletes it.

        Skipped for Claude Code (input handler is always ready immediately).

        Returns True if the CLI is accepting input, False otherwise.
        """
        if not self._use_tmux:
            return True

        # Claude Code doesn't need probing — input is ready immediately
        if self._use_csi_u:
            return True

        # Capture pane BEFORE sending probe (to compare)
        pre_pane = await tmux_mgr.capture_pane(self._tmux_session, history_lines=0) or ""
        tmux_target = self._tmux_target or self._tmux_session

        # Send a unique probe string
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "send-keys",
            "-l",
            "-t",
            tmux_target,
            self._PROBE_STR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        await asyncio.sleep(0.5)

        # Check if probe appeared (compare with pre-capture to avoid false matches)
        post_pane = await tmux_mgr.capture_pane(self._tmux_session, history_lines=0) or ""
        if self._PROBE_STR in post_pane and self._PROBE_STR not in pre_pane:
            # Found — delete it character by character
            for _ in self._PROBE_STR:
                await tmux_mgr.send_keys_raw(tmux_target, "BSpace")
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
            logger.info("Input readiness probe: CLI is accepting keystrokes")
            return True

        logger.debug("Input readiness probe: keystroke not received by CLI")
        return False

    async def _dismiss_startup_dialogs(self) -> None:
        """Dismiss trust/permission dialogs that some CLIs show on first run.

        Copilot shows "Do you trust the files in this folder?" on new
        worktrees.  We detect this by a ONE-TIME pane content check for
        known dialog keywords, then send the appropriate keys to dismiss.

        Does NOT blindly send Enter — that can cause Copilot to exit if
        the wrong option is selected or no dialog is present.
        """
        if not self._use_tmux:
            return

        pane_text = await tmux_mgr.capture_pane(self._tmux_session, history_lines=0)
        if not pane_text:
            return

        pane_lower = pane_text.lower()

        # Copilot trust dialog: "Do you trust the files in this folder?"
        if "trust" in pane_lower and "folder" in pane_lower:
            logger.info("Trust dialog detected — selecting 'Yes and remember'")
            # Navigate to option 2 (Down from default) and select
            await tmux_mgr.send_keys_raw(self._tmux_session, "Down")
            await asyncio.sleep(0.2)
            await tmux_mgr.send_keys_raw(self._tmux_session, "Enter")
            # Wait for Copilot to reload after trusting
            await asyncio.sleep(8)
            # Re-check cursor stability
            for _ in range(15):
                if await self._is_cli_at_prompt():
                    logger.info("CLI at prompt after trust dialog dismissal")
                    return
                await asyncio.sleep(1)
            logger.warning("CLI not at prompt after trust dialog — proceeding anyway")
        else:
            logger.info("No startup dialog detected — proceeding to prompt injection")

    async def _wait_for_mcp_reload(self, timeout: float = 10) -> bool:
        """Wait for Copilot's MCP server reload to complete.

        Copilot reloads MCP servers during startup, which flushes stdin and
        discards any buffered keystrokes.  This method waits for the
        "MCP Servers reloaded" message to appear in the pane.

        Returns True if MCP reload was detected, False if timeout elapsed
        (the CLI may not have MCP servers or already reloaded before we
        started watching).
        """
        if not self._use_tmux:
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            pane = await tmux_mgr.capture_pane(self._tmux_session, history_lines=0)
            if pane and "mcp servers reloaded" in pane.lower():
                logger.info("MCP reload detected — re-probing input readiness")
                # Brief settle time after reload
                await asyncio.sleep(2)
                return True
            await asyncio.sleep(0.5)

        logger.debug("No MCP reload detected within %.0fs — proceeding", timeout)
        return False

    async def _wait_for_idle(self, min_quiet: float = 10, timeout: float = 600) -> None:
        """Wait until the CLI is idle at the input prompt.

        After a phase marker is emitted, a Stop hook may cause the agent to
        continue processing (e.g. running /code-review).  If we inject the
        next phase's prompt while the CLI is still busy, the TUI discards it.

        Uses a two-stage check:
        1. **Output quiescence**: no new output for *min_quiet* seconds.
        2. **Prompt verification** (best-effort): checks for known prompt
           indicators via tmux capture-pane.  If the PTY read loop died,
           ``_last_output_time`` is stale — the prompt check prevents a
           false-positive idle signal.  If prompt detection fails (unknown
           CLI), quiescence alone is accepted after a longer quiet period.

        Fallback for TUI-heavy CLIs (e.g. Copilot): some CLIs continuously
        redraw their status bar / prompt area even when idle, keeping
        ``_last_output_time`` perpetually fresh.  A consecutive prompt-streak
        counter detects this: if the pane shows a known idle prompt for
        ``_PROMPT_READY_STREAK`` consecutive checks, the CLI is considered
        ready regardless of PTY output noise.

        Falls back to *timeout* to avoid blocking forever.
        """
        _PROMPT_READY_STREAK = 5  # consecutive 1s checks ≈ 5s of stable prompt
        _prompt_streak = 0

        deadline = time.time() + timeout
        # Give the CLI at least a brief moment to start any Stop-hook continuation
        await asyncio.sleep(3)

        while not self._cancelled and time.time() < deadline:
            idle = time.time() - self._last_output_time
            if idle >= min_quiet:
                if self._use_tmux:
                    at_prompt = await self._is_cli_at_prompt()
                    if at_prompt:
                        logger.info("CLI idle for %.1fs and at prompt — ready for next phase", idle)
                        return
                    # NOT using quiescence-only fallback here — during
                    # "thinking" states CLIs produce no output but are NOT
                    # ready for input.  Only timeout is safe for unknown CLIs.
                    logger.debug("Output quiet for %.1fs but CLI not at prompt — waiting", idle)
                else:
                    logger.info("CLI idle for %.1fs — injecting next phase prompt", idle)
                    return
            elif self._use_tmux:
                # Fallback: TUI redraws (status bar, cursor blink, counters)
                # keep _last_output_time fresh even when the CLI is truly idle.
                # Use consecutive prompt detections as a secondary readiness signal.
                at_prompt = await self._is_cli_at_prompt()
                if at_prompt:
                    _prompt_streak += 1
                    if _prompt_streak >= _PROMPT_READY_STREAK:
                        logger.info(
                            "CLI at prompt for %d consecutive checks "
                            "(output still arriving — likely TUI redraws) "
                            "— ready for next phase",
                            _prompt_streak,
                        )
                        return
                else:
                    _prompt_streak = 0
            await asyncio.sleep(1)

        if self._cancelled:
            return
        logger.warning("Idle wait timed out after %.0fs — injecting prompt anyway", timeout)

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

            # PR URL detection
            pr_url = self._extract_pr_url(parsed)
            if pr_url:
                # Extract PR number from the URL (last numeric segment)
                pr_number = None
                num_match = re.search(r"/(\d+)/?$", pr_url)
                if num_match:
                    pr_number = int(num_match.group(1))
                await self.state.update_ticket(self.ticket_id, pr_url=pr_url, pr_number=pr_number)
                await self.broadcaster.broadcast_ticket_update(
                    self.run_id, self.ticket_id, None, pr_url=pr_url, pr_number=pr_number
                )

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

        # Tool result — don't log (verbose), but extract PR URLs before discarding
        if msg_type == "tool_result":
            content = data.get("content", data.get("output", ""))
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            pr_url = self._extract_pr_url(str(content))
            if pr_url:
                return f"[pr] {pr_url}"
            return None

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
            # Kill any stale session with the same name (e.g. from a previous server run)
            if await tmux_mgr.session_exists(self._tmux_session):
                logger.info("Cleaning up stale ad-hoc session %s before creating new one", self._tmux_session)
                await tmux_mgr.kill_session(self._tmux_session)
            ok = await tmux_mgr.create_session(self._tmux_session, cmd, self.worktree_path)
            if ok:
                # Verify Claude actually started (not just a shell fallback).
                # Give it a moment to initialise then check pane_current_command.
                await asyncio.sleep(1.0)
                still_alive = await tmux_mgr.session_exists(self._tmux_session)
                if not still_alive:
                    logger.warning(
                        "Ad-hoc tmux session %s disappeared immediately — Claude may have crashed",
                        self._tmux_session,
                    )
                    raise RuntimeError(f"Claude exited immediately in ad-hoc session {self._tmux_session}")

                is_shell = await tmux_mgr.is_shell_fallback(self._tmux_session)
                if is_shell:
                    logger.warning(
                        "Ad-hoc tmux session %s is alive but running a shell "
                        "(Claude exited or failed to start) — killing session",
                        self._tmux_session,
                    )
                    await tmux_mgr.kill_session(self._tmux_session)
                    raise RuntimeError(
                        f"Claude failed to start in ad-hoc session {self._tmux_session} (fell back to shell)"
                    )

                session_pid = await tmux_mgr.get_session_pid(self._tmux_session)
                # Create a dummy process object for compatibility (pid tracking)
                self._tmux_pid = session_pid
                logger.info(
                    "Spawned tmux ad-hoc session %s in %s (pid=%s)", self._tmux_session, self.worktree_path, session_pid
                )
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
        frame_interval = 0.016  # ~60fps for smooth scrolling
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

        _session_check_interval = 5.0
        _last_session_check = time.time()

        try:
            while True:
                if (time.time() - _last_session_check) > _session_check_interval:
                    _last_session_check = time.time()
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

    async def scroll_viewer_to_bottom(self, ws: WebSocket) -> None:
        """Exit tmux copy-mode and refresh the viewer to show live content."""
        if not self._use_tmux:
            return
        vs = self._viewer_sessions.get(ws)
        if not vs:
            return
        await tmux_mgr.cancel_copy_mode(vs.session_name)
        await tmux_mgr.refresh_client(vs.session_name)

    async def refresh_viewer(self, ws) -> None:
        """Redraw a viewer's tmux client without sending input to the CLI process."""
        if not self._use_tmux:
            return
        vs = self._viewer_sessions.get(ws)
        if not vs:
            return
        await tmux_mgr.refresh_client(vs.session_name)

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
            # Synchronous best-effort check: assume running unless explicitly stopped.
            # The async path in terminals.py does the real session_exists check.
            return not getattr(self, "_stopped", False)
        return self.process is not None and self.process.returncode is None

    async def async_is_running(self) -> bool:
        """Async check that verifies the tmux session actually exists."""
        if self._use_tmux:
            if getattr(self, "_stopped", False):
                return False
            return await tmux_mgr.session_exists(self._tmux_session)
        return self.process is not None and self.process.returncode is None

    async def stop(self) -> None:
        """Terminate the session."""
        self._stopped = True
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
