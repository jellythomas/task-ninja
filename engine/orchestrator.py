"""Orchestrator — manages worker pool and ticket execution lifecycle."""

import asyncio
import json
import sys
from typing import Optional

import yaml

from engine.broadcaster import Broadcaster
from engine.git_manager import GitManager
from engine.jira_client import JiraClient
from engine.state import StateManager
from engine.ticket_watchdog import TicketWatchdog
from engine.worker import Worker
from models.ticket import RunStatus, TicketState


class Orchestrator:
    """Main orchestration loop that picks tickets and spawns workers."""

    def __init__(self, state: StateManager, broadcaster: Broadcaster, config: dict):
        self.state = state
        self.broadcaster = broadcaster
        self.config = config
        self._workers: dict[str, Worker] = {}  # ticket_id -> Worker
        self._tasks: dict[str, asyncio.Task] = {}  # ticket_id -> Task
        self._running = False
        self._run_id: Optional[str] = None
        self.jira_client = JiraClient()
        self.notifier = None  # Set by server.py after construction
        self.watchdog = TicketWatchdog(state, broadcaster)
        self.watchdog.set_callbacks(requeue_cb=self._watchdog_requeue)

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

        print(f"[orchestrator] Started for run {run_id}", file=sys.stderr)
        asyncio.create_task(self._loop())

    async def _recover_stale_tickets(self, run_id: str) -> None:
        """Move orphaned planning/developing tickets back to queued."""
        import os
        for st in (TicketState.PLANNING, TicketState.DEVELOPING):
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
                    print(f"[orchestrator] Recovering stale ticket {ticket.jira_key} ({st}) -> queued", file=sys.stderr)
                    await self.state.update_ticket_state(ticket.id, TicketState.QUEUED)
                    await self.broadcaster.broadcast_ticket_update(run_id, ticket.id, TicketState.QUEUED)

    async def pause(self, run_id: str = None) -> None:
        """Stop picking new tickets. Active workers continue."""
        self._running = False
        rid = run_id or self._run_id
        if rid:
            self._run_id = rid
            await self.state.update_run_status(rid, RunStatus.PAUSED)
            await self.broadcaster.broadcast_run_status(rid, RunStatus.PAUSED)
        print("[orchestrator] Paused", file=sys.stderr)

    async def resume(self, run_id: str = None) -> None:
        """Resume picking new tickets."""
        rid = run_id or self._run_id
        if rid:
            self._run_id = rid
            self._running = True
            await self.state.update_run_status(rid, RunStatus.RUNNING)
            await self.broadcaster.broadcast_run_status(rid, RunStatus.RUNNING)
            asyncio.create_task(self._loop())
            print("[orchestrator] Resumed", file=sys.stderr)

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
        await self.broadcaster.broadcast_ticket_update(
            self._run_id, ticket_id, ticket.state, paused=True
        )

    async def resume_ticket(self, ticket_id: str) -> None:
        """Resume a paused ticket — move back to queued for re-pickup."""
        ticket = await self.state.get_ticket(ticket_id)
        if not ticket:
            return

        await self.state.update_ticket(ticket_id, paused=False)
        # Move back to queued so orchestrator picks it up
        await self.state.update_ticket_state(ticket_id, TicketState.QUEUED)
        await self.broadcaster.broadcast_ticket_update(
            self._run_id, ticket_id, TicketState.QUEUED, paused=False
        )

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
                except Exception:
                    pass

            await self.broadcaster.broadcast(
                self._run_id, "ticket_deleted", {"ticket_id": ticket_id}
            )

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
                print(f"[orchestrator] Error in tick: {e}", file=sys.stderr)

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
                    await self.notifier.notify_ticket_failed(
                        ticket.jira_key, tid, ticket.error or ""
                    )
            elif ticket and ticket.state in (TicketState.DONE, TicketState.REVIEW):
                self.watchdog.on_ticket_completed(tid)
                if self.notifier:
                    await self.notifier.notify_ticket_completed(ticket.jira_key, tid)

        # Check available slots
        active_count = await self.state.count_active_tickets(self._run_id)
        available_slots = run.max_parallel - active_count

        if available_slots <= 0:
            return

        # Skip spawning if outside working hours
        if not self.watchdog.is_within_working_hours():
            return

        # Pick next queued tickets
        spawned_any = False
        queued = await self.state.get_tickets_by_state(self._run_id, TicketState.QUEUED)
        for ticket in queued[:available_slots]:
            if ticket.paused:
                continue
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
                t.state in {TicketState.DONE, TicketState.REVIEW, TicketState.FAILED}
                for t in work_tickets
            )
            active = any(t.state in {TicketState.PLANNING, TicketState.DEVELOPING, TicketState.QUEUED} for t in work_tickets)

            if all_terminal and not active:
                self._running = False
                await self.state.update_run_status(self._run_id, RunStatus.COMPLETED)
                await self.broadcaster.broadcast_run_status(self._run_id, RunStatus.COMPLETED)
                if self.notifier:
                    await self.notifier.notify_run_completed(run.name or self._run_id)

    async def _fail_ticket(self, ticket_id: str, error: str) -> None:
        """Mark a ticket as failed with error message, broadcast update."""
        await self.state.update_ticket(ticket_id, error=error)
        await self.state.update_ticket_state(ticket_id, TicketState.FAILED)
        try:
            await self.broadcaster.broadcast_ticket_update(
                self._run_id, ticket_id, TicketState.FAILED, error=error
            )
        except Exception:
            pass  # DB state is already FAILED — broadcast is best-effort
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
            await self._fail_ticket(ticket_id, "No project path configured. Assign a repository or set project_path on the run.")
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
            worktree_path = await git.create_worktree(jira_key, parent_branch)
        except RuntimeError as e:
            await self._fail_ticket(ticket_id, f"Failed to create worktree: {e}")
            return

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

        # Build worker config from profile or config.yaml fallback
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
            worker_skip_permissions = False  # already in args_template if needed
            worker_execute_command = None  # already in args_template
        else:
            worker_command = claude_cfg.get("command", "claude")
            worker_flags = claude_cfg.get("flags", ["--print"])
            worker_skip_permissions = claude_cfg.get("skip_permissions", True)
            worker_execute_command = claude_cfg.get("execute_command", "/execute-jira-task")

        # Parse phases_config from profile
        phases_config = None
        idle_timeout = claude_cfg.get("idle_timeout", 10)
        if profile and profile.phases_config:
            try:
                phases_config = json.loads(profile.phases_config)
            except (json.JSONDecodeError, TypeError):
                phases_config = None

        # Fall back to config.yaml phases if profile has none
        if not phases_config and "phases" in claude_cfg:
            yaml_phases = claude_cfg["phases"]
            phases_config = []
            for phase_name in ["planning", "developing", "review"]:
                phase_def = yaml_phases.get(phase_name, {})
                if phase_def:
                    phases_config.append({
                        "phase": phase_name,
                        "prompts": phase_def.get("commands", []),
                        "marker": phase_def.get("marker"),
                    })

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
            skip_permissions=worker_skip_permissions if worker_execute_command else False,
            execute_command=worker_execute_command or "",
            jira_status_mapping=mcp_cfg.get("jira_status_mapping", {}),
            auto_create_pr=claude_cfg.get("auto_create_pr", True),
            pr_base_branch=parent_branch,
            phases_config=phases_config,
            idle_timeout=idle_timeout,
        )
        worker.jira_client = self.jira_client

        self._workers[ticket_id] = worker
        self._tasks[ticket_id] = asyncio.create_task(worker.run())
        self.watchdog.on_ticket_active(ticket_id)
        print(f"[orchestrator] Spawned worker for {jira_key} in {worktree_path}", file=sys.stderr)

    async def _watchdog_requeue(self, run_id: str) -> None:
        """Called by watchdog when a ticket is re-queued for retry."""
        if self._running:
            return  # _tick will pick it up
        # If run is completed/idle, restart the loop
        run = await self.state.get_run(run_id)
        if run and run.status != RunStatus.RUNNING:
            await self.start(run_id)
