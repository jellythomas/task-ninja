"""Claude CLI worker — spawns a claude session per ticket."""

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Optional

from engine.broadcaster import Broadcaster
from engine.claude_helper import ClaudeHelper
from engine.state import StateManager
from models.ticket import TicketState


class Worker:
    """Manages a single Claude CLI process for a ticket."""

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
    ):
        self.ticket_id = ticket_id
        self.run_id = run_id
        self.jira_key = jira_key
        self.worktree_path = worktree_path
        self.state = state_manager
        self.broadcaster = broadcaster
        self.claude_command = claude_command
        self.claude_flags = claude_flags or ["--print"]
        self.skip_permissions = skip_permissions
        self.execute_command = execute_command
        self.jira_status_mapping = jira_status_mapping or {}
        self.auto_create_pr = auto_create_pr
        self.pr_base_branch = pr_base_branch
        self.claude_helper = ClaudeHelper(claude_command, skip_permissions)
        self.process: Optional[asyncio.subprocess.Process] = None
        self._cancelled = False
        self._detected_pr_url: Optional[str] = None

    async def run(self) -> bool:
        """Execute the ticket. Returns True if successful."""
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
            self._log_fh = open(self._log_file_path, "a", buffering=1)  # line-buffered

            # Spawn process — stdout goes to log file, we tail the file
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.worktree_path,
                stdout=self._log_fh,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ},
            )

            # Store PID and log file path, broadcast to UI
            await self.state.update_ticket(
                self.ticket_id,
                worker_pid=self.process.pid,
                log_file=str(self._log_file_path),
            )
            await self.broadcaster.broadcast_ticket_update(
                self.run_id, self.ticket_id, None, worker_pid=self.process.pid
            )

            # Tail the log file for real-time output
            moved_to_developing = False
            await self._tail_log_file(self._log_file_path, moved_to_developing)

            # Wait for process to finish
            await self.process.wait()
            # Final flush — read any remaining lines
            await asyncio.sleep(0.5)
            await self._tail_log_file(self._log_file_path, moved_to_developing, final=True)

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
                return True
            else:
                error = f"Claude CLI exited with code {self.process.returncode}"
                await self.state.update_ticket(self.ticket_id, error=error)
                await self.state.update_ticket_state(self.ticket_id, TicketState.FAILED)
                await self.broadcaster.broadcast_ticket_update(
                    self.run_id, self.ticket_id, TicketState.FAILED, error=error
                )
                return False

        except Exception as e:
            error = str(e)
            await self.state.update_ticket(self.ticket_id, error=error)
            try:
                await self.state.update_ticket_state(self.ticket_id, TicketState.FAILED)
            except ValueError:
                pass
            await self.broadcaster.broadcast_ticket_update(
                self.run_id, self.ticket_id, TicketState.FAILED, error=error
            )
            return False
        finally:
            if hasattr(self, '_log_fh') and self._log_fh:
                self._log_fh.close()
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

    _tail_offset: int = 0
    _moved_to_developing: bool = False

    async def _tail_log_file(self, log_path: Path, moved_to_developing: bool, final: bool = False) -> None:
        """Tail the log file, parse lines, store in DB, broadcast via SSE."""
        while not self._cancelled:
            try:
                with open(log_path, "r") as f:
                    f.seek(self._tail_offset)
                    new_data = f.read()
                    self._tail_offset = f.tell()
            except FileNotFoundError:
                if final:
                    return
                await asyncio.sleep(0.5)
                continue

            if new_data:
                for raw in new_data.splitlines():
                    raw = raw.rstrip()
                    if not raw:
                        continue

                    line = self._parse_stream_line(raw)
                    if not line:
                        continue

                    await self.state.append_log(self.ticket_id, line)
                    await self.broadcaster.broadcast_log(self.run_id, self.ticket_id, line)

                    pr_url = self._extract_pr_url(line)
                    if pr_url:
                        self._detected_pr_url = pr_url
                        await self.state.update_ticket(self.ticket_id, pr_url=pr_url)
                        await self.broadcaster.broadcast_ticket_update(
                            self.run_id, self.ticket_id, None, pr_url=pr_url
                        )

                    if not self._moved_to_developing and self._is_developing_signal(line):
                        self._moved_to_developing = True
                        await self.state.update_ticket_state(self.ticket_id, TicketState.DEVELOPING)
                        await self.broadcaster.broadcast_ticket_update(
                            self.run_id, self.ticket_id, TicketState.DEVELOPING
                        )
                        await self._sync_jira_status("developing")

            # If process finished or final read, stop tailing
            if final or (self.process and self.process.returncode is not None):
                return

            await asyncio.sleep(1)  # Poll every 1 second

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
            # Show a short summary
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
            success = await self.claude_helper.transition_jira_issue(self.jira_key, target)
            if success:
                print(f"[worker] Synced {self.jira_key} -> {target} on Jira", file=sys.stderr)
                await self.state.append_log(self.ticket_id, f"[jira] Transitioned to {target}")
        except Exception as e:
            print(f"[worker] Jira sync failed for {self.jira_key}: {e}", file=sys.stderr)
