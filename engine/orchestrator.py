"""Orchestrator — manages worker pool and ticket execution lifecycle."""

import asyncio
import sys
from typing import Optional

import yaml

from engine.broadcaster import Broadcaster
from engine.git_manager import GitManager
from engine.state import StateManager
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

    async def start(self, run_id: str) -> None:
        """Start the orchestration loop for a run."""
        run = await self.state.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        self._run_id = run_id
        self._running = True
        await self.state.update_run_status(run_id, RunStatus.RUNNING)
        await self.broadcaster.broadcast_run_status(run_id, RunStatus.RUNNING)

        print(f"[orchestrator] Started for run {run_id}", file=sys.stderr)
        asyncio.create_task(self._loop())

    async def pause(self) -> None:
        """Stop picking new tickets. Active workers continue."""
        self._running = False
        if self._run_id:
            await self.state.update_run_status(self._run_id, RunStatus.PAUSED)
            await self.broadcaster.broadcast_run_status(self._run_id, RunStatus.PAUSED)
        print("[orchestrator] Paused", file=sys.stderr)

    async def resume(self) -> None:
        """Resume picking new tickets."""
        if self._run_id:
            self._running = True
            await self.state.update_run_status(self._run_id, RunStatus.RUNNING)
            await self.broadcaster.broadcast_run_status(self._run_id, RunStatus.RUNNING)
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
            del self._workers[ticket_id]

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

        # Clean up finished tasks
        finished = [tid for tid, task in self._tasks.items() if task.done()]
        for tid in finished:
            self._tasks.pop(tid, None)
            self._workers.pop(tid, None)

        # Check available slots
        active_count = await self.state.count_active_tickets(self._run_id)
        available_slots = run.max_parallel - active_count

        if available_slots <= 0:
            return

        # Pick next queued tickets
        queued = await self.state.get_tickets_by_state(self._run_id, TicketState.QUEUED)
        for ticket in queued[:available_slots]:
            if ticket.paused:
                continue
            await self._spawn_worker(ticket.id, ticket.jira_key, run)

        # Check if all done
        all_tickets = await self.state.get_tickets_for_run(self._run_id)
        all_terminal = all(
            t.state in {TicketState.DONE, TicketState.REVIEW, TicketState.FAILED, TicketState.PENDING}
            for t in all_tickets
        )
        active = any(t.state in {TicketState.PLANNING, TicketState.DEVELOPING, TicketState.QUEUED} for t in all_tickets)

        if all_terminal and not active:
            self._running = False
            await self.state.update_run_status(self._run_id, RunStatus.COMPLETED)
            await self.broadcaster.broadcast_run_status(self._run_id, RunStatus.COMPLETED)

    async def _spawn_worker(self, ticket_id: str, jira_key: str, run: object) -> None:
        """Spawn a Claude CLI worker for a ticket."""
        claude_cfg = self.config.get("claude", {})
        git_cfg = self.config.get("git", {})

        git = GitManager(
            run.project_path,
            git_cfg.get("worktree_dir", ".worktrees"),
            git_cfg.get("branch_prefix", "feat"),
        )

        try:
            worktree_path = await git.create_worktree(jira_key)
        except RuntimeError as e:
            error = f"Failed to create worktree: {e}"
            await self.state.update_ticket(ticket_id, error=error)
            await self.state.update_ticket_state(ticket_id, TicketState.FAILED)
            await self.broadcaster.broadcast_ticket_update(
                self._run_id, ticket_id, TicketState.FAILED, error=error
            )
            return

        branch_name = await git.get_branch_name(jira_key)
        await self.state.update_ticket(
            ticket_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

        worker = Worker(
            ticket_id=ticket_id,
            run_id=self._run_id,
            jira_key=jira_key,
            worktree_path=worktree_path,
            state_manager=self.state,
            broadcaster=self.broadcaster,
            claude_command=claude_cfg.get("command", "claude"),
            claude_flags=claude_cfg.get("flags", ["--print"]),
            skip_permissions=claude_cfg.get("skip_permissions", True),
            execute_command=claude_cfg.get("execute_command", "/execute-jira-task"),
        )

        self._workers[ticket_id] = worker
        self._tasks[ticket_id] = asyncio.create_task(worker.run())
        print(f"[orchestrator] Spawned worker for {jira_key} in {worktree_path}", file=sys.stderr)
