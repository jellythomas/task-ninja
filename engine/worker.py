"""Claude CLI worker — spawns a claude session per ticket."""

import asyncio
import os
import re
import signal
import sys
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

            # Spawn process
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.worktree_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ},
            )

            # Store PID
            await self.state.update_ticket(self.ticket_id, worker_pid=self.process.pid)

            # Stream output
            moved_to_developing = False
            async for line_bytes in self.process.stdout:
                if self._cancelled:
                    break

                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                # Log and broadcast
                await self.state.append_log(self.ticket_id, line)
                await self.broadcaster.broadcast_log(self.run_id, self.ticket_id, line)

                # Detect PR URL in output
                pr_url = self._extract_pr_url(line)
                if pr_url:
                    self._detected_pr_url = pr_url
                    await self.state.update_ticket(self.ticket_id, pr_url=pr_url)
                    await self.broadcaster.broadcast_ticket_update(
                        self.run_id, self.ticket_id, None, pr_url=pr_url
                    )

                # Detect phase transition to developing
                if not moved_to_developing and self._is_developing_signal(line):
                    moved_to_developing = True
                    await self.state.update_ticket_state(self.ticket_id, TicketState.DEVELOPING)
                    await self.broadcaster.broadcast_ticket_update(
                        self.run_id, self.ticket_id, TicketState.DEVELOPING
                    )
                    await self._sync_jira_status("developing")

            # Wait for process to finish
            await self.process.wait()

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
