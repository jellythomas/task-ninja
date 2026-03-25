"""Orchestrator — manages worker pool and ticket execution lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from datetime import datetime, timezone

from engine.bitbucket_client import BitbucketClient
from engine.broadcaster import Broadcaster
from engine.git_manager import GitManager
from engine.jira_client import JiraClient
from engine.state import StateManager
from engine.ticket_watchdog import TicketWatchdog
from engine.worker import Worker
from models.ticket import RunStatus, Ticket, TicketState

logger = logging.getLogger(__name__)


class Orchestrator:
    """Main orchestration loop that picks tickets and spawns workers."""

    def __init__(self, state: StateManager, broadcaster: Broadcaster, config: dict):
        self.state = state
        self.broadcaster = broadcaster
        self.config = config
        self._workers: dict[str, Worker] = {}  # ticket_id -> Worker
        self._tasks: dict[str, asyncio.Task] = {}  # ticket_id -> Task
        self._adhoc_terminals: dict = {}  # ticket_id -> AdHocTerminal
        self._running = False
        self._run_id: str | None = None
        self.jira_client = JiraClient()
        self.bitbucket_client = BitbucketClient()
        self.notifier = None  # Set by server.py after construction
        self.watchdog = TicketWatchdog(state, broadcaster)
        self.watchdog.set_callbacks(requeue_cb=self._watchdog_requeue)
        self._last_pr_check: datetime | None = None

    async def start(self, run_id: str) -> None:
        """Start the orchestration loop for a run."""
        run = await self.state.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        self._run_id = run_id
        self._running = True
        await self.state.update_run_status(run_id, RunStatus.RUNNING)
        await self.broadcaster.broadcast_run_status(run_id, RunStatus.RUNNING)

        # Recover stale tickets: planning/developing with no live worker → back to queued
        await self._recover_stale_tickets(run_id)

        logger.info("Started for run %s", run_id)
        self._loop_task = asyncio.create_task(self._loop())

    async def _recover_stale_tickets(self, run_id: str) -> None:
        """Move orphaned planning/developing/review tickets back to queued.

        REVIEW tickets are only recovered when their last_completed_phase shows
        the review phase never actually ran (i.e. the worker died between setting
        the state and completing the phase).
        """
        for st in (TicketState.PLANNING, TicketState.DEVELOPING, TicketState.REVIEW):
            tickets = await self.state.get_tickets_by_state(run_id, st)
            for ticket in tickets:
                # If we don't have a live worker for this ticket, it's stale
                if ticket.id not in self._workers:
                    # Double-check: is the PID actually dead?
                    if ticket.worker_pid:
                        try:
                            os.kill(ticket.worker_pid, 0)
                            continue  # Process is alive, skip
                        except OSError:
                            pass  # Process is dead

                    # For REVIEW tickets, only recover if the review phase
                    # never completed (last_completed_phase != "review").
                    # If review DID complete, the ticket is genuinely in review.
                    if st == TicketState.REVIEW and ticket.last_completed_phase == "review":
                        continue  # Genuinely in review — leave it alone

                    logger.info("Recovering stale ticket %s (%s) -> queued", ticket.jira_key, st)
                    await self.state.update_ticket_state(ticket.id, TicketState.QUEUED)
                    await self.broadcaster.broadcast_ticket_update(run_id, ticket.id, TicketState.QUEUED)

    async def pause(self, run_id: str | None = None) -> None:
        """Stop picking new tickets. Active workers continue."""
        self._running = False
        rid = run_id or self._run_id
        if rid:
            self._run_id = rid
            await self.state.update_run_status(rid, RunStatus.PAUSED)
            await self.broadcaster.broadcast_run_status(rid, RunStatus.PAUSED)
        logger.info("Paused")

    async def resume(self, run_id: str | None = None) -> None:
        """Resume picking new tickets."""
        rid = run_id or self._run_id
        if rid:
            self._run_id = rid
            self._running = True
            await self.state.update_run_status(rid, RunStatus.RUNNING)
            await self.broadcaster.broadcast_run_status(rid, RunStatus.RUNNING)
            self._loop_task = asyncio.create_task(self._loop())
            logger.info("Resumed")

    async def pause_ticket(self, ticket_id: str) -> None:
        """Pause a specific ticket — kill its worker."""
        worker = self._workers.get(ticket_id)
        if worker:
            await worker.kill()
            task = self._tasks.pop(ticket_id, None)
            if task:
                task.cancel()
            self._workers.pop(ticket_id, None)

        await self.state.update_ticket(ticket_id, paused=True, worker_pid=None)
        ticket = await self.state.get_ticket(ticket_id)
        await self.broadcaster.broadcast_ticket_update(self._run_id, ticket_id, ticket.state, paused=True)

    async def resume_ticket(self, ticket_id: str) -> None:
        """Resume a paused ticket — move back to queued for re-pickup."""
        ticket = await self.state.get_ticket(ticket_id)
        if not ticket:
            return

        await self.state.update_ticket(ticket_id, paused=False)
        # Move back to queued so orchestrator picks it up
        await self.state.update_ticket_state(ticket_id, TicketState.QUEUED)
        await self.broadcaster.broadcast_ticket_update(self._run_id, ticket_id, TicketState.QUEUED, paused=False)

    async def delete_ticket(self, ticket_id: str) -> None:
        """Delete a ticket — kill worker if running, remove from board."""
        worker = self._workers.get(ticket_id)
        if worker:
            await worker.kill()
            task = self._tasks.pop(ticket_id, None)
            if task:
                task.cancel()
            del self._workers[ticket_id]

        ticket = await self.state.delete_ticket(ticket_id)
        if ticket:
            # Cleanup worktree if exists
            if ticket.worktree_path:
                try:
                    run = await self.state.get_run(self._run_id)
                    git = GitManager(run.project_path, self.config.get("git", {}).get("worktree_dir", ".worktrees"))
                    await git.cleanup_worktree(ticket.worktree_path)
                except (RuntimeError, OSError):
                    pass

            await self.broadcaster.broadcast(self._run_id, "ticket_deleted", {"ticket_id": ticket_id})

    def interrupt_worker(self, ticket_id: str) -> bool:
        """Send SIGINT to a worker's process. Returns True if signal sent."""
        worker = self._workers.get(ticket_id)
        if worker:
            return worker.interrupt()
        return False

    async def kill_worker(self, ticket_id: str) -> bool:
        """Kill a worker for a ticket if one is running. Returns True if killed."""
        worker = self._workers.get(ticket_id)
        if worker and worker.is_running:
            await worker.kill()
            task = self._tasks.pop(ticket_id, None)
            if task:
                task.cancel()
            del self._workers[ticket_id]
            return True
        return False

    async def _loop(self) -> None:
        """Main orchestration loop."""
        poll_interval = self.config.get("orchestrator", {}).get("poll_interval", 5)

        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.exception("Error in tick: %s", e)

            await asyncio.sleep(poll_interval)

    async def _tick(self) -> None:
        """Single tick: check for available slots and pick next ticket."""
        if not self._run_id:
            return

        run = await self.state.get_run(self._run_id)
        if not run or run.status != RunStatus.RUNNING:
            self._running = False
            return

        # Clean up finished tasks and notify watchdog
        finished = [tid for tid, task in self._tasks.items() if task.done()]
        for tid in finished:
            self._tasks.pop(tid, None)
            self._workers.pop(tid, None)
            ticket = await self.state.get_ticket(tid)
            if ticket and ticket.state == TicketState.FAILED:
                self.watchdog.on_ticket_failed(tid)
                if self.notifier:
                    await self.notifier.notify_ticket_failed(ticket.jira_key, tid, ticket.error or "")
            elif ticket and ticket.state in (TicketState.DONE, TicketState.REVIEW):
                self.watchdog.on_ticket_completed(tid)
                if self.notifier:
                    await self.notifier.notify_ticket_completed(ticket.jira_key, tid)
                # Cleanup worktree for completed tickets (if setting enabled)
                cleanup_enabled = self.config.get("git", {}).get("cleanup_worktrees", True)
                if cleanup_enabled and ticket.worktree_path:
                    try:
                        run = await self.state.get_run(self._run_id)
                        git = GitManager(run.project_path, self.config.get("git", {}).get("worktree_dir", ".worktrees"))
                        await git.cleanup_worktree(ticket.worktree_path)
                        logger.info("Cleaned up worktree for completed ticket %s", tid)
                    except (RuntimeError, OSError) as e:
                        logger.warning("Failed to cleanup worktree for ticket %s: %s", tid, e)

        # Recover orphaned tickets: in active state but no live worker
        for st in (TicketState.PLANNING, TicketState.DEVELOPING, TicketState.REVIEW):
            orphaned = await self.state.get_tickets_by_state(self._run_id, st)
            for ticket in orphaned:
                if ticket.id in self._workers or ticket.id in self._tasks:
                    continue
                if ticket.paused:
                    continue
                if ticket.worker_pid:
                    try:
                        os.kill(ticket.worker_pid, 0)
                        continue
                    except OSError:
                        pass
                if st == TicketState.REVIEW and ticket.last_completed_phase == "review":
                    continue
                logger.info(
                    "Recovering orphaned ticket %s (%s, last_completed=%s) -> queued",
                    ticket.jira_key, st, ticket.last_completed_phase,
                )
                await self.state.update_ticket_state(ticket.id, TicketState.QUEUED)
                await self.broadcaster.broadcast_ticket_update(self._run_id, ticket.id, TicketState.QUEUED)

        # Check available slots
        active_count = await self.state.count_active_tickets(self._run_id)
        available_slots = run.max_parallel - active_count

        if available_slots <= 0:
            return

        # Skip spawning if outside working hours
        if not self.watchdog.is_within_working_hours():
            return

        # Pick next queued tickets (smart priority ordering)
        spawned_any = False
        queued = await self.state.get_tickets_by_state(self._run_id, TicketState.QUEUED)
        prioritized = await self._prioritize_queue(queued)
        for ticket in prioritized[:available_slots]:
            await self._spawn_worker(ticket.id, ticket.jira_key, run)
            spawned_any = True

        # Don't check completion on the same tick we spawned workers —
        # give them time to update their state from QUEUED to PLANNING.
        if spawned_any:
            return

        # Check if all done (only auto-complete if run is RUNNING, not PAUSED)
        run = await self.state.get_run(self._run_id)
        if run and run.status == RunStatus.RUNNING:
            all_tickets = await self.state.get_tickets_for_run(self._run_id)
            # Ignore TODO tickets — they're backlog, not part of the active run
            work_tickets = [t for t in all_tickets if t.state != TicketState.TODO]
            if not work_tickets:
                return  # No work tickets at all, don't auto-complete

            all_terminal = all(
                t.state in {TicketState.DONE, TicketState.REVIEW, TicketState.FAILED} for t in work_tickets
            )
            active = any(
                t.state
                in {TicketState.PLANNING, TicketState.DEVELOPING, TicketState.QUEUED, TicketState.AWAITING_INPUT}
                for t in work_tickets
            )

            if all_terminal and not active:
                self._running = False
                await self.state.update_run_status(self._run_id, RunStatus.COMPLETED)
                await self.broadcaster.broadcast_run_status(self._run_id, RunStatus.COMPLETED)
                if self.notifier:
                    await self.notifier.notify_run_completed(run.name or self._run_id)

        # Poll PR statuses for REVIEW tickets (at most once every 60 seconds)
        await self._check_pr_statuses()

    async def _prioritize_queue(self, queued: list[Ticket]) -> list[Ticket]:
        """Sort queued tickets by computed priority. Skip blocked and paused tickets."""
        scored = []
        for t in queued:
            if t.paused:
                continue
            # Skip blocked tickets
            if await self._is_blocked(t):
                logger.debug("Ticket %s is blocked by unfinished dependencies — skipping", t.jira_key)
                continue
            score = 1000 - t.rank  # Lower rank = higher score
            # Bonus if other tickets depend on this one
            dependents = await self.state.count_dependents(self._run_id, t.jira_key)
            if dependents > 0:
                score += 50 * dependents  # More dependents = higher priority
            scored.append((score, t))
        scored.sort(key=lambda x: x[0], reverse=True)

        # Collect predicted_files from all active workers to detect file conflicts
        active_files: set[str] = set()
        for tid in self._workers:
            t = await self.state.get_ticket(tid)
            if t and t.predicted_files:
                try:
                    active_files.update(json.loads(t.predicted_files))
                except (json.JSONDecodeError, TypeError):
                    pass

        # Filter: skip candidates whose predicted files overlap with active workers
        result = []
        for score, ticket in scored:
            if ticket.predicted_files:
                try:
                    candidate_files = set(json.loads(ticket.predicted_files))
                except (json.JSONDecodeError, TypeError):
                    candidate_files = set()

                if candidate_files & active_files:
                    logger.debug(
                        "Ticket %s skipped — predicted files conflict with active workers",
                        ticket.jira_key,
                    )
                    continue  # Skip — would cause merge conflict

                # Add this ticket's files to active set (for subsequent candidates)
                active_files.update(candidate_files)

            result.append(ticket)

        return result

    async def _check_pr_statuses(self) -> None:
        """Poll Bitbucket PR status for all REVIEW tickets. Rate-limited to once per 60 seconds."""
        if not self._run_id:
            return

        now = datetime.now(tz=timezone.utc)
        if self._last_pr_check is not None:
            elapsed = (now - self._last_pr_check).total_seconds()
            if elapsed < 60:
                return

        if not await self.bitbucket_client.is_configured():
            return

        self._last_pr_check = now

        review_tickets = await self.state.get_tickets_by_state(self._run_id, TicketState.REVIEW)
        for ticket in review_tickets:
            if not ticket.pr_url or not ticket.pr_number:
                continue

            # Parse repo_slug from pr_url
            repo_slug = None
            # Try bitbucket.org URL format first
            match = re.search(r'bitbucket\.org/[^/]+/([^/]+)/pull-requests/\d+', ticket.pr_url)
            if match:
                repo_slug = match.group(1)
            else:
                # Try API URL format
                match = re.search(r'repositories/[^/]+/([^/]+)/pullrequests/\d+', ticket.pr_url)
                if match:
                    repo_slug = match.group(1)

            if not repo_slug:
                logger.warning("Could not parse repo_slug from pr_url: %s", ticket.pr_url)
                continue

            pr_info = await self.bitbucket_client.get_pr_status(repo_slug, ticket.pr_number)
            if pr_info is None:
                continue

            state = pr_info.get("state", "")
            new_approvals = pr_info.get("approvals")
            new_comment_count = pr_info.get("comment_count") or 0
            checked_at = datetime.utcnow().isoformat()

            # Always update pr_status, approvals, comment_count and pr_last_checked_at
            await self.state.update_ticket(
                ticket.id,
                pr_status=state,
                pr_approvals=new_approvals,
                pr_comment_count=new_comment_count,
                pr_last_checked_at=checked_at,
            )

            # Notify on new comments
            old_count = ticket.pr_comment_count or 0
            if new_comment_count > old_count and self.notifier:
                await self.notifier.notify(
                    title=f"{ticket.jira_key} — PR review comments",
                    body=f"{new_comment_count - old_count} new comment(s) on PR #{ticket.pr_number}",
                    tag=f"pr-comments-{ticket.id}",
                )

            if state == "merged":
                logger.info("PR merged for ticket %s — moving to DONE", ticket.jira_key)
                await self.state.update_ticket_state(ticket.id, TicketState.DONE)
                await self.broadcaster.broadcast_ticket_update(self._run_id, ticket.id, TicketState.DONE)
                # Transition Jira to Done
                mcp_cfg = self.config.get("mcp", {})
                jira_mapping = mcp_cfg.get("jira_status_mapping", {})
                done_status = jira_mapping.get(TicketState.DONE.value)
                if done_status and await self.jira_client.is_configured():
                    try:
                        await self.jira_client.transition_issue(ticket.jira_key, done_status)
                    except Exception as e:
                        logger.warning("Failed to transition Jira issue %s: %s", ticket.jira_key, e)
                # Cleanup worktree
                cleanup_enabled = self.config.get("git", {}).get("cleanup_worktrees", True)
                if cleanup_enabled and ticket.worktree_path:
                    try:
                        run = await self.state.get_run(self._run_id)
                        git = GitManager(
                            run.project_path,
                            self.config.get("git", {}).get("worktree_dir", ".worktrees"),
                        )
                        await git.cleanup_worktree(ticket.worktree_path)
                    except (RuntimeError, OSError) as e:
                        logger.warning("Failed to cleanup worktree for %s: %s", ticket.id, e)
                # Notify
                if self.notifier:
                    await self.notifier.notify_ticket_completed(ticket.jira_key, ticket.id)

            elif state == "declined":
                logger.info("PR declined for ticket %s — moving to FAILED", ticket.jira_key)
                await self.state.update_ticket(ticket.id, error="PR was declined")
                await self.state.update_ticket_state(ticket.id, TicketState.FAILED)
                await self.broadcaster.broadcast_ticket_update(
                    self._run_id, ticket.id, TicketState.FAILED, error="PR was declined"
                )

    async def _is_blocked(self, ticket: Ticket) -> bool:
        """Return True if any of the ticket's blockers are not yet DONE in this run."""
        if not ticket.blocked_by_keys:
            return False
        try:
            blocker_keys: list[str] = json.loads(ticket.blocked_by_keys)
        except (json.JSONDecodeError, TypeError):
            return False
        if not blocker_keys:
            return False
        blockers = await self.state.get_tickets_by_jira_keys(self._run_id, blocker_keys)
        done_keys = {t.jira_key for t in blockers if t.state == TicketState.DONE}
        return not all(key in done_keys for key in blocker_keys)

    async def _fail_ticket(self, ticket_id: str, error: str) -> None:
        """Mark a ticket as failed with error message, broadcast update."""
        await self.state.update_ticket(ticket_id, error=error)
        await self.state.update_ticket_state(ticket_id, TicketState.FAILED)
        with contextlib.suppress(RuntimeError, OSError):
            await self.broadcaster.broadcast_ticket_update(
                self._run_id, ticket_id, TicketState.FAILED, error=error
            )  # DB state is already FAILED — broadcast is best-effort
        self.watchdog.on_ticket_failed(ticket_id)
        if self.notifier:
            await self.notifier.notify_ticket_failed(jira_key=None, ticket_id=ticket_id, error=error)

    async def _spawn_worker(self, ticket_id: str, jira_key: str, run: object) -> None:
        """Spawn a CLI worker for a ticket."""
        claude_cfg = self.config.get("claude", {})
        git_cfg = self.config.get("git", {})

        # Resolve project path: ticket repo > run repo > run.project_path
        ticket = await self.state.get_ticket(ticket_id)
        project_path = run.project_path
        repo = None
        if ticket and ticket.repository_id:
            repo = await self.state.get_repository(ticket.repository_id)
        elif run.repository_id:
            repo = await self.state.get_repository(run.repository_id)
        if repo:
            project_path = repo.path

        if not project_path:
            await self._fail_ticket(
                ticket_id, "No project path configured. Assign a repository or set project_path on the run."
            )
            return

        # Resolve parent branch: ticket > run > repo default > config default
        parent_branch = None
        if ticket and ticket.parent_branch:
            parent_branch = ticket.parent_branch
        elif run.parent_branch:
            parent_branch = run.parent_branch
        elif repo and repo.default_branch:
            parent_branch = repo.default_branch
        else:
            parent_branch = git_cfg.get("base_branch", "master")

        git = GitManager(
            project_path,
            git_cfg.get("worktree_dir", ".worktrees"),
            git_cfg.get("branch_prefix", "feat"),
        )

        try:
            result = await git.create_worktree(jira_key, parent_branch)
        except RuntimeError as e:
            await self._fail_ticket(ticket_id, f"Failed to create worktree: {e}")
            return

        # Check for branch parent mismatch — pause and ask user
        if result.mismatch:
            input_data = json.dumps(
                {
                    "current_parent": result.current_parent,
                    "expected_parent": result.expected_parent,
                    "branch_existed": result.branch_existed,
                }
            )
            await self.state.update_ticket(
                ticket_id,
                worktree_path=result.path,
                branch_name=await git.get_branch_name(jira_key),
                input_type="branch_mismatch",
                input_data=input_data,
            )
            await self.state.update_ticket_state(ticket_id, TicketState.AWAITING_INPUT)
            await self.broadcaster.broadcast_ticket_update(
                self._run_id,
                ticket_id,
                TicketState.AWAITING_INPUT,
                input_type="branch_mismatch",
                input_data=json.loads(input_data),
            )
            logger.warning(
                "Branch mismatch for %s: expected %s, got %s",
                jira_key,
                result.expected_parent,
                result.current_parent,
            )
            return

        worktree_path = result.path
        branch_name = await git.get_branch_name(jira_key)
        await self.state.update_ticket(
            ticket_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

        # Resolve agent profile: ticket > repo > default
        profile = None
        if ticket and ticket.profile_id:
            profile = await self.state.get_agent_profile(ticket.profile_id)
        elif repo and repo.default_profile_id:
            profile = await self.state.get_agent_profile(repo.default_profile_id)
        if not profile:
            profile = await self.state.get_default_agent_profile()

        # Build worker config from profile
        if profile:
            worker_command = profile.command
            # Parse args template, replacing variables
            args_str = profile.args_template
            args_str = args_str.replace("{JIRA_KEY}", jira_key)
            args_str = args_str.replace("{BRANCH_NAME}", branch_name)
            args_str = args_str.replace("{WORKTREE_PATH}", worktree_path)
            args_str = args_str.replace("{PARENT_BRANCH}", parent_branch)
            args_str = args_str.replace("{PROJECT_PATH}", project_path)
            if ticket and ticket.summary:
                args_str = args_str.replace("{JIRA_SUMMARY}", ticket.summary)
            # Split args respecting quoted strings
            import shlex

            worker_flags = shlex.split(args_str)
        else:
            # Fallback when no agent profile exists (DB always seeds one, but just in case)
            worker_command = "claude"
            worker_flags = ["--dangerously-skip-permissions"]

        # Parse phases_config from profile
        phases_config = None
        idle_timeout = claude_cfg.get("idle_timeout", 10)
        if profile and profile.phases_config:
            try:
                phases_config = json.loads(profile.phases_config)
            except (json.JSONDecodeError, TypeError):
                phases_config = None

        mcp_cfg = self.config.get("mcp", {})
        worker = Worker(
            ticket_id=ticket_id,
            run_id=self._run_id,
            jira_key=jira_key,
            worktree_path=worktree_path,
            state_manager=self.state,
            broadcaster=self.broadcaster,
            claude_command=worker_command,
            claude_flags=worker_flags,
            jira_status_mapping=mcp_cfg.get("jira_status_mapping", {}),
            pr_base_branch=parent_branch,
            phases_config=phases_config,
            idle_timeout=idle_timeout,
        )
        worker.jira_client = self.jira_client

        self._workers[ticket_id] = worker
        self._tasks[ticket_id] = asyncio.create_task(worker.run())
        self.watchdog.on_ticket_active(ticket_id)
        logger.info("Spawned worker for %s in %s", jira_key, worktree_path)

    async def resolve_input(self, ticket_id: str, choice: str) -> dict:
        """Resolve an AWAITING_INPUT ticket based on user's choice.

        Args:
            ticket_id: The ticket to resolve
            choice: "use_as_is" | "rebase" | "fresh_start"

        Returns:
            dict with status info
        """
        ticket = await self.state.get_ticket(ticket_id)
        if not ticket:
            raise ValueError("Ticket not found")
        if ticket.state != TicketState.AWAITING_INPUT:
            raise ValueError(f"Ticket is not awaiting input (state: {ticket.state})")
        if ticket.input_type != "branch_mismatch":
            raise ValueError(f"Unknown input_type: {ticket.input_type}")

        input_data = json.loads(ticket.input_data) if ticket.input_data else {}
        expected_parent = input_data.get("expected_parent")

        # Resolve project path
        run = await self.state.get_run(ticket.run_id)
        git_cfg = self.config.get("git", {})
        repo = None
        if ticket.repository_id:
            repo = await self.state.get_repository(ticket.repository_id)
        elif run and run.repository_id:
            repo = await self.state.get_repository(run.repository_id)
        project_path = (repo.path if repo else None) or (run.project_path if run else None) or "."

        git = GitManager(
            project_path,
            git_cfg.get("worktree_dir", ".worktrees"),
            git_cfg.get("branch_prefix", "feat"),
        )

        result_msg = ""
        if choice == "use_as_is":
            result_msg = "Keeping existing branch as-is"

        elif choice == "rebase":
            if not expected_parent:
                raise ValueError("No expected_parent in input_data for rebase")
            try:
                await git.rebase_onto(ticket.jira_key, expected_parent)
                result_msg = f"Rebased onto origin/{expected_parent}"
            except RuntimeError as e:
                await self._fail_ticket(ticket_id, f"Rebase failed: {e}")
                return {"status": "failed", "error": str(e)}

        elif choice == "fresh_start":
            if not expected_parent:
                raise ValueError("No expected_parent in input_data for fresh_start")
            try:
                fresh_result = await git.fresh_start(ticket.jira_key, expected_parent)
                await self.state.update_ticket(ticket_id, worktree_path=fresh_result.path)
                result_msg = f"Fresh start from origin/{expected_parent}"
            except RuntimeError as e:
                await self._fail_ticket(ticket_id, f"Fresh start failed: {e}")
                return {"status": "failed", "error": str(e)}

        else:
            raise ValueError(f"Unknown choice: {choice}")

        # Clear input fields and move back to queued
        await self.state.update_ticket(ticket_id, input_type=None, input_data=None)
        await self.state.update_ticket_state(ticket_id, TicketState.QUEUED)
        await self.broadcaster.broadcast_ticket_update(ticket.run_id, ticket_id, TicketState.QUEUED)

        # Auto-resume orchestrator if needed
        if not self._running and ticket.run_id:
            await self.resume(ticket.run_id)

        logger.info("Resolved input for %s: %s — %s", ticket.jira_key, choice, result_msg)
        return {"status": "resolved", "choice": choice, "message": result_msg}

    async def _watchdog_requeue(self, run_id: str) -> None:
        """Called by watchdog when a ticket is re-queued for retry."""
        if self._running:
            return  # _tick will pick it up
        # If run is completed/idle, restart the loop
        run = await self.state.get_run(run_id)
        if run and run.status != RunStatus.RUNNING:
            await self.start(run_id)
