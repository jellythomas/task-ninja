"""CLI worker — spawns an AI agent process per ticket with PTY for live terminal access."""

import asyncio
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path
from typing import Optional

from fastapi import WebSocket

from engine.broadcaster import Broadcaster
from engine.claude_helper import ClaudeHelper
from engine.jira_client import JiraClient
from engine.state import StateManager
from models.ticket import TicketState

# Cursor-forward sequences → replace with spaces (preserves word spacing)
_CURSOR_FWD_RE = re.compile(r'\x1b\[(\d+)C')
_CURSOR_FWD_1_RE = re.compile(r'\x1b\[C')
# Strip remaining ANSI escape codes for log parsing
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b\[\?[0-9;]*[hl]|\r')


def _clean_ansi(text: str) -> str:
    """Strip ANSI codes, converting cursor-forward to spaces."""
    text = _CURSOR_FWD_RE.sub(lambda m: ' ' * int(m.group(1)), text)
    text = _CURSOR_FWD_1_RE.sub(' ', text)
    return _ANSI_RE.sub('', text)


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
        claude_flags: list[str] = None,
        skip_permissions: bool = True,
        execute_command: str = "/execute-jira-task",
        jira_status_mapping: dict = None,
        auto_create_pr: bool = True,
        pr_base_branch: str = "master",
        phases_config: list[dict] = None,
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
        self.skip_permissions = skip_permissions
        self.execute_command = execute_command
        self.jira_status_mapping = jira_status_mapping or {}
        self.auto_create_pr = auto_create_pr
        self.pr_base_branch = pr_base_branch
        self.claude_helper = ClaudeHelper(claude_command, skip_permissions)
        self.jira_client: Optional[JiraClient] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self._cancelled = False
        self._detected_pr_url: Optional[str] = None

        # Phase pipeline config
        self.phases_config = phases_config  # None = legacy --print mode
        self.idle_timeout = idle_timeout
        self._current_phase: Optional[str] = None
        self._phase_marker: Optional[str] = None
        self._marker_detected = asyncio.Event()
        self._last_output_time: float = 0
        self._user_active: bool = False

        # PTY and viewer management
        self._master_fd: Optional[int] = None
        self._viewers: set[WebSocket] = set()
        self._output_buffer = bytearray()  # Scrollback for late-joining viewers
        self._max_buffer = 256 * 1024  # 256KB
        self._line_buffer = ""  # Partial line accumulator for parsing

    async def run(self) -> bool:
        """Execute the ticket. Dispatches to interactive or legacy mode."""
        if self.phases_config:
            return await self._run_interactive()
        else:
            return await self._run_legacy()

    async def _run_legacy(self) -> bool:
        """Execute the ticket using legacy --print mode. Returns True if successful."""
        self._moved_to_developing = False
        try:
            # Build command
            cmd = [self.claude_command] + self.claude_flags
            if self.skip_permissions:
                cmd.append("--dangerously-skip-permissions")
            cmd.append(f"{self.execute_command} {self.jira_key}")

            # Update state to planning
            await self.state.update_ticket_state(self.ticket_id, TicketState.PLANNING)
            await self.broadcaster.broadcast_ticket_update(
                self.run_id, self.ticket_id, TicketState.PLANNING
            )
            await self._sync_jira_status("planning")

            # Log the command being run
            cmd_str = " ".join(cmd)
            print(f"[worker] Running: {cmd_str} in {self.worktree_path}", file=sys.stderr)
            await self.state.append_log(self.ticket_id, f"[worker] $ {cmd_str}")
            await self.broadcaster.broadcast_log(self.run_id, self.ticket_id, f"[worker] $ {cmd_str}")
            await self.state.append_log(self.ticket_id, f"[worker] cwd: {self.worktree_path}")
            await self.broadcaster.broadcast_log(self.run_id, self.ticket_id, f"[worker] cwd: {self.worktree_path}")

            # Set up log file for persistence
            log_dir = Path(self.worktree_path).parent
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_file_path = log_dir / f"log-{self.jira_key.lower()}.txt"
            self._log_fh = open(self._log_file_path, "a", buffering=1)

            # Create PTY pair for the process
            master_fd, slave_fd = pty.openpty()
            self._master_fd = master_fd

            # Set initial terminal size (reasonable default; viewer sends resize on connect)
            winsize = struct.pack("HHHH", 24, 80, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

            # Set non-blocking on master
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            # Spawn process inside PTY
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.worktree_path,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
            )
            os.close(slave_fd)  # Parent doesn't need slave end

            # Store PID and log file path, broadcast to UI
            await self.state.update_ticket(
                self.ticket_id,
                worker_pid=self.process.pid,
                log_file=str(self._log_file_path),
            )
            await self.broadcaster.broadcast_ticket_update(
                self.run_id, self.ticket_id, None, worker_pid=self.process.pid
            )

            # Read PTY output until process exits
            await self._pty_read_loop()

            # Wait for process to finish
            await self.process.wait()

            # Final drain of any remaining PTY data
            await self._drain_pty()

            if self._cancelled:
                return False

            if self.process.returncode == 0:
                # Auto-create draft PR if not already detected from output
                if self.auto_create_pr and not self._detected_pr_url:
                    await self._create_draft_pr()

                # Move to review
                await self.state.update_ticket_state(self.ticket_id, TicketState.REVIEW)
                await self.broadcaster.broadcast_ticket_update(
                    self.run_id, self.ticket_id, TicketState.REVIEW
                )
                await self._sync_jira_status("review")
                await self._notify_viewers_exit(0)
                return True
            else:
                error = f"CLI exited with code {self.process.returncode}"
                await self.state.update_ticket(self.ticket_id, error=error)
                await self.state.update_ticket_state(self.ticket_id, TicketState.FAILED)
                await self.broadcaster.broadcast_ticket_update(
                    self.run_id, self.ticket_id, TicketState.FAILED, error=error
                )
                await self._notify_viewers_exit(self.process.returncode)
                return False

        except Exception as e:
            if self._cancelled:
                return False  # Killed intentionally — don't overwrite state
            # Don't overwrite review/done state with FAILED (legacy path)
            ticket = await self.state.get_ticket(self.ticket_id)
            if ticket and ticket.state in (TicketState.REVIEW, TicketState.DONE):
                print(f"[worker] Exception during {ticket.state} — keeping state: {e}", file=sys.stderr)
                return True
            error = str(e)
            await self.state.update_ticket(self.ticket_id, error=error)
            try:
                await self.state.update_ticket_state(self.ticket_id, TicketState.FAILED)
            except ValueError:
                pass
            try:
                await self.broadcaster.broadcast_ticket_update(
                    self.run_id, self.ticket_id, TicketState.FAILED, error=error
                )
            except Exception:
                pass
            return False
        finally:
            self._close_pty()
            if hasattr(self, '_log_fh') and self._log_fh:
                self._log_fh.close()
            if not self._cancelled:
                await self.state.update_ticket(self.ticket_id, worker_pid=None)

    async def _run_interactive(self) -> bool:
        """Execute ticket using interactive mode with phase pipeline."""
        try:
            # Build command — interactive mode (no --print)
            cmd = [self.claude_command] + self.claude_flags
            if self.skip_permissions:
                cmd.append("--dangerously-skip-permissions")

            # Log
            cmd_str = " ".join(cmd)
            print(f"[worker] Running (interactive): {cmd_str} in {self.worktree_path}", file=sys.stderr)
            await self.state.append_log(self.ticket_id, f"[worker] $ {cmd_str}")
            await self.broadcaster.broadcast_log(self.run_id, self.ticket_id, f"[worker] $ {cmd_str}")
            await self.state.append_log(self.ticket_id, f"[worker] cwd: {self.worktree_path}")
            await self.broadcaster.broadcast_log(self.run_id, self.ticket_id, f"[worker] cwd: {self.worktree_path}")

            # Set up log file
            log_dir = Path(self.worktree_path).parent
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_file_path = log_dir / f"log-{self.jira_key.lower()}.txt"
            self._log_fh = open(self._log_file_path, "a", buffering=1)

            # Create PTY
            master_fd, slave_fd = pty.openpty()
            self._master_fd = master_fd
            winsize = struct.pack("HHHH", 24, 80, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            # Spawn interactive process
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.worktree_path,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
            )
            os.close(slave_fd)

            await self.state.update_ticket(
                self.ticket_id,
                worker_pid=self.process.pid,
                log_file=str(self._log_file_path),
            )
            await self.broadcaster.broadcast_ticket_update(
                self.run_id, self.ticket_id, None, worker_pid=self.process.pid
            )

            # Start PTY read loop in background
            asyncio.create_task(self._pty_read_loop())

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
                        await self.state.append_log(self.ticket_id, f"[worker] === Phase: {phase_name} (already completed, skipping) ===")
                        await self.broadcaster.broadcast_log(
                            self.run_id, self.ticket_id, f"[worker] === Phase: {phase_name} (already completed, skipping) ==="
                        )
                        skip_until_after = None  # Next phase will run
                        continue
                    else:
                        await self.state.append_log(self.ticket_id, f"[worker] === Phase: {phase_name} (already completed, skipping) ===")
                        await self.broadcaster.broadcast_log(
                            self.run_id, self.ticket_id, f"[worker] === Phase: {phase_name} (already completed, skipping) ==="
                        )
                        continue

                # Transition state
                if ticket_state:
                    await self.state.update_ticket_state(self.ticket_id, ticket_state)
                    await self.broadcaster.broadcast_ticket_update(
                        self.run_id, self.ticket_id, ticket_state
                    )
                    await self._sync_jira_status(phase_name)

                self._current_phase = phase_name
                self._phase_marker = marker

                await self.state.append_log(self.ticket_id, f"[worker] === Phase: {phase_name} ===")
                await self.broadcaster.broadcast_log(
                    self.run_id, self.ticket_id, f"[worker] === Phase: {phase_name} ==="
                )

                # Send all prompts for this phase as a single block
                full_prompt = "\n".join(
                    p.replace("{JIRA_KEY}", self.jira_key).replace("{PARENT_BRANCH}", self.pr_base_branch) for p in prompts
                )
                self._send_to_pty(full_prompt + "\r")

                # Wait for phase completion
                completed = await self._wait_for_phase_completion(marker)

                if self._cancelled:
                    break

                if completed:
                    # Persist phase completion for resume on retry
                    await self.state.update_ticket(self.ticket_id, last_completed_phase=phase_name)
                elif self.process.returncode is not None:
                    # Process died during phase — always keep review/done state
                    # Review is the last phase; core work (planning+developing) is done.
                    # If /open-pr failed, user can see it in terminal logs and open PR manually.
                    ticket = await self.state.get_ticket(self.ticket_id)
                    current_state = ticket.state if ticket else None
                    if current_state in (TicketState.REVIEW, TicketState.DONE):
                        print(f"[worker] Process exited (code={self.process.returncode}) during {phase_name}, ticket in {current_state} — keeping state", file=sys.stderr)
                        await self._notify_viewers_exit(self.process.returncode)
                        return True
                    error = f"CLI exited with code {self.process.returncode} during {phase_name}"
                    await self.state.update_ticket(self.ticket_id, error=error)
                    await self.state.update_ticket_state(self.ticket_id, TicketState.FAILED)
                    await self.broadcaster.broadcast_ticket_update(
                        self.run_id, self.ticket_id, TicketState.FAILED, error=error
                    )
                    await self._notify_viewers_exit(self.process.returncode)
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
                print(f"[worker] Exception during {ticket.state} — keeping state: {e}", file=sys.stderr)
                return True
            error = str(e)
            await self.state.update_ticket(self.ticket_id, error=error)
            try:
                await self.state.update_ticket_state(self.ticket_id, TicketState.FAILED)
            except ValueError:
                pass
            try:
                await self.broadcaster.broadcast_ticket_update(
                    self.run_id, self.ticket_id, TicketState.FAILED, error=error
                )
            except Exception:
                pass
            return False
        finally:
            # Kill process if still running
            if self.process and self.process.returncode is None:
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
            if hasattr(self, '_log_fh') and self._log_fh:
                self._log_fh.close()
            if not self._cancelled:
                await self.state.update_ticket(self.ticket_id, worker_pid=None)

    async def kill(self) -> None:
        """Kill the worker process."""
        self._cancelled = True
        if self.process and self.process.returncode is None:
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

        Coalesces all available data per frame (~30fps) so xterm.js receives
        complete escape sequences instead of partial fragments.
        """
        FRAME_INTERVAL = 0.033  # ~30fps
        while not self._cancelled:
            if self.process.returncode is not None:
                break

            try:
                # Coalesce: read ALL available data in one batch
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

                if not batch:
                    await asyncio.sleep(FRAME_INTERVAL)
                    continue

                data = bytes(batch)

                # Store in scrollback buffer
                self._output_buffer.extend(data)
                if len(self._output_buffer) > self._max_buffer:
                    self._output_buffer = self._output_buffer[-self._max_buffer:]

                # Forward entire batch to viewers
                try:
                    await self._send_to_viewers(data)
                except Exception:
                    pass  # Never let viewer errors kill the read loop

                # Write to log file
                if hasattr(self, '_log_fh') and self._log_fh:
                    self._log_fh.write(data.decode('utf-8', errors='replace'))
                    self._log_fh.flush()

                # Parse for state transitions, PR URLs, and log broadcasting
                text = data.decode('utf-8', errors='replace')
                await self._process_output(text)

                await asyncio.sleep(FRAME_INTERVAL)
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
                    self._output_buffer = self._output_buffer[-self._max_buffer:]
                try:
                    await self._send_to_viewers(data)
                except Exception:
                    pass
                if hasattr(self, '_log_fh') and self._log_fh:
                    self._log_fh.write(data.decode('utf-8', errors='replace'))
                    self._log_fh.flush()
                text = data.decode('utf-8', errors='replace')
                await self._process_output(text)
        except OSError:
            pass

    async def _send_to_viewers(self, data: bytes) -> None:
        """Send raw PTY output to all connected WebSocket viewers."""
        disconnected = set()
        for ws in list(self._viewers):  # Copy to avoid "Set changed size during iteration"
            try:
                await ws.send_bytes(data)
            except Exception:
                disconnected.add(ws)
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
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    # --- Viewer management (attach/detach) ---

    async def attach_viewer(self, ws: WebSocket) -> None:
        """Attach a WebSocket viewer. Sends scrollback buffer first."""
        # Send buffered output so viewer sees recent history
        if self._output_buffer:
            try:
                await ws.send_bytes(bytes(self._output_buffer))
            except Exception:
                return
        self._viewers.add(ws)

    def detach_viewer(self, ws: WebSocket) -> None:
        """Detach a WebSocket viewer. Does NOT affect the running process."""
        self._viewers.discard(ws)

    def write_input(self, data: bytes) -> None:
        """Write user input to the PTY (from a terminal viewer)."""
        if self._master_fd is not None and not self._cancelled:
            try:
                os.write(self._master_fd, data)
                self._user_active = True  # Reset debounce timer
            except OSError:
                pass

    def interrupt(self) -> bool:
        """Send Escape key to the PTY to interrupt Claude's current operation.
        This cancels the current tool call without killing the session."""
        if self._master_fd is not None and not self._cancelled:
            try:
                os.write(self._master_fd, b'\x1b')  # Escape key
                self._user_active = True
                return True
            except OSError:
                pass
        return False

    def resize_pty(self, rows: int, cols: int) -> None:
        """Resize the PTY terminal."""
        if self._master_fd is not None:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    @property
    def is_running(self) -> bool:
        """Check if the worker process is still running."""
        return self.process is not None and self.process.returncode is None

    # --- PTY send helper ---

    def _send_to_pty(self, text: str) -> None:
        """Write a command/prompt to the PTY."""
        if self._master_fd is not None and not self._cancelled:
            try:
                os.write(self._master_fd, text.encode())
            except OSError:
                pass

    # --- Phase completion ---

    async def _wait_for_phase_completion(self, marker: Optional[str]) -> bool:
        """Wait for phase to complete via marker detection or idle debounce.

        Returns True if phase completed, False if process died.
        """
        self._marker_detected.clear()
        self._last_output_time = time.time()

        while not self._cancelled:
            # Process died
            if self.process.returncode is not None:
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
        while '\n' in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split('\n', 1)
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

            # PR URL detection (works in both modes)
            pr_url = self._extract_pr_url(parsed)
            if pr_url:
                self._detected_pr_url = pr_url
                await self.state.update_ticket(self.ticket_id, pr_url=pr_url)
                await self.broadcaster.broadcast_ticket_update(
                    self.run_id, self.ticket_id, None, pr_url=pr_url
                )

            # Legacy mode: keyword-based developing detection
            if not self.phases_config and not getattr(self, '_moved_to_developing', False):
                if self._is_developing_signal(parsed):
                    self._moved_to_developing = True
                    await self.state.update_ticket_state(self.ticket_id, TicketState.DEVELOPING)
                    await self.broadcaster.broadcast_ticket_update(
                        self.run_id, self.ticket_id, TicketState.DEVELOPING
                    )
                    await self._sync_jira_status("developing")

    def _parse_stream_line(self, raw: str) -> Optional[str]:
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
            elif tool in ("Edit", "Write"):
                path = inp.get("file_path", "")
                return f"[tool] {tool}: {path}"
            elif tool == "Read":
                path = inp.get("file_path", "")
                return f"[tool] Read: {path}"
            elif tool == "Grep":
                pattern = inp.get("pattern", "")
                return f"[tool] Grep: {pattern}"
            else:
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

    def _is_developing_signal(self, line: str) -> bool:
        """Detect if output indicates we've moved past planning into development."""
        signals = [
            "implementing", "writing code", "creating branch",
            "edit", "write", "creating file", "modifying",
            "running spec", "running test", "bundle exec",
        ]
        lower = line.lower()
        return any(s in lower for s in signals)

    def _extract_pr_url(self, line: str) -> Optional[str]:
        """Extract PR/pull-request URL from output line."""
        patterns = [
            r'(https?://bitbucket\.org/[^\s]+/pull-requests/\d+)',
            r'(https?://github\.com/[^\s]+/pull/\d+)',
            r'(https?://[^\s]*pull[_-]?request[^\s]*\d+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                return match.group(1)
        return None

    async def _create_draft_pr(self) -> None:
        """Create a draft PR after successful execution."""
        try:
            ticket = await self.state.get_ticket(self.ticket_id)
            if not ticket or not ticket.branch_name:
                return

            await self.state.append_log(self.ticket_id, "[pr] Creating draft pull request...")
            await self.broadcaster.broadcast_log(
                self.run_id, self.ticket_id, "[pr] Creating draft pull request..."
            )

            result = await self.claude_helper.create_draft_pr(
                self.jira_key, ticket.branch_name, self.worktree_path, self.pr_base_branch
            )
            if result and result.get("url"):
                pr_url = result["url"]
                pr_id = result.get("id")
                await self.state.update_ticket(self.ticket_id, pr_url=pr_url, pr_number=pr_id)
                await self.broadcaster.broadcast_ticket_update(
                    self.run_id, self.ticket_id, None, pr_url=pr_url
                )
                await self.state.append_log(self.ticket_id, f"[pr] Draft PR created: {pr_url}")
                print(f"[worker] Draft PR created for {self.jira_key}: {pr_url}", file=sys.stderr)
            else:
                await self.state.append_log(self.ticket_id, "[pr] Failed to create draft PR")
        except Exception as e:
            print(f"[worker] PR creation failed for {self.jira_key}: {e}", file=sys.stderr)
            await self.state.append_log(self.ticket_id, f"[pr] Error: {e}")

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
                print(f"[worker] Synced {self.jira_key} -> {target} on Jira", file=sys.stderr)
                await self.state.append_log(self.ticket_id, f"[jira] Transitioned to {target}")
        except Exception as e:
            print(f"[worker] Jira sync failed for {self.jira_key}: {e}", file=sys.stderr)


class AdHocTerminal:
    """Lightweight interactive Claude session for review/done tickets.

    Spawns `claude --dangerously-skip-permissions` in the worktree with a PTY.
    No phase pipeline, no state transitions — just a live terminal.
    Exposes the same viewer interface as Worker so the WebSocket handler works.
    """

    def __init__(self, worktree_path: str, claude_command: str = "claude"):
        self.worktree_path = worktree_path
        self.claude_command = claude_command
        self.process: Optional[asyncio.subprocess.Process] = None
        self._master_fd: Optional[int] = None
        self._viewers: set = set()
        self._output_buffer = bytearray()
        self._max_buffer = 256 * 1024
        self._read_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Spawn an interactive Claude session in the worktree."""
        cmd = [self.claude_command, "--dangerously-skip-permissions"]

        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd

        winsize = struct.pack("HHHH", 24, 80, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.worktree_path,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        )
        os.close(slave_fd)

        self._read_task = asyncio.create_task(self._pty_read_loop())
        print(f"[adhoc] Spawned interactive session in {self.worktree_path} (pid={self.process.pid})", file=sys.stderr)

    async def _pty_read_loop(self) -> None:
        """Read from PTY and forward to viewers."""
        loop = asyncio.get_event_loop()
        while True:
            try:
                ready = await loop.run_in_executor(
                    None, lambda: select.select([self._master_fd], [], [], 0.5)[0]
                )
                if not ready:
                    if self.process.returncode is not None:
                        break
                    continue
                data = os.read(self._master_fd, 65536)
                if not data:
                    break
                self._output_buffer.extend(data)
                if len(self._output_buffer) > self._max_buffer:
                    self._output_buffer = self._output_buffer[-self._max_buffer:]
                try:
                    await self._send_to_viewers(data)
                except Exception:
                    pass
            except (OSError, ValueError):
                break

    async def _send_to_viewers(self, data: bytes) -> None:
        disconnected = set()
        for ws in list(self._viewers):
            try:
                await ws.send_bytes(data)
            except Exception:
                disconnected.add(ws)
        self._viewers -= disconnected

    async def attach_viewer(self, ws) -> None:
        if self._output_buffer:
            try:
                await ws.send_bytes(bytes(self._output_buffer))
            except Exception:
                return
        self._viewers.add(ws)

    def detach_viewer(self, ws) -> None:
        self._viewers.discard(ws)

    def write_input(self, data: bytes) -> None:
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass

    def resize_pty(self, rows: int, cols: int) -> None:
        if self._master_fd is not None:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def stop(self) -> None:
        """Terminate the session."""
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        if self._read_task:
            self._read_task.cancel()
