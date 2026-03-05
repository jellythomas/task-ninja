"""Scheduler — triggers runs on a schedule using APScheduler."""

import sys
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from engine.state import StateManager
from models.ticket import RunStatus


class RunScheduler:
    """Manages scheduled run execution."""

    def __init__(self, state: StateManager, start_callback):
        self.state = state
        self.start_callback = start_callback  # async def start(run_id) -> None
        self.scheduler = AsyncIOScheduler()
        self._started = False

    def start(self) -> None:
        if not self._started:
            self.scheduler.start()
            self._started = True
            print("[scheduler] Started", file=sys.stderr)

    def stop(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False
            print("[scheduler] Stopped", file=sys.stderr)

    async def add_schedule(self, schedule_id: str, run_id: str, schedule_type: str,
                           cron_expression: str = None, start_time: str = None) -> None:
        """Add a scheduled job."""
        job_id = f"schedule_{schedule_id}"

        if schedule_type == "recurring" and cron_expression:
            trigger = CronTrigger.from_crontab(cron_expression)
            self.scheduler.add_job(
                self._execute_run, trigger, id=job_id, args=[run_id],
                replace_existing=True,
            )
            print(f"[scheduler] Added recurring job {job_id}: {cron_expression}", file=sys.stderr)

        elif schedule_type == "one-time" and start_time:
            run_at = datetime.fromisoformat(start_time)
            trigger = DateTrigger(run_date=run_at)
            self.scheduler.add_job(
                self._execute_run, trigger, id=job_id, args=[run_id],
                replace_existing=True,
            )
            print(f"[scheduler] Added one-time job {job_id}: {start_time}", file=sys.stderr)

    def remove_schedule(self, schedule_id: str) -> None:
        job_id = f"schedule_{schedule_id}"
        try:
            self.scheduler.remove_job(job_id)
            print(f"[scheduler] Removed job {job_id}", file=sys.stderr)
        except Exception:
            pass

    async def _execute_run(self, run_id: str) -> None:
        """Execute a scheduled run if it's not already running."""
        run = await self.state.get_run(run_id)
        if not run:
            print(f"[scheduler] Run {run_id} not found, skipping", file=sys.stderr)
            return

        if run.status == RunStatus.RUNNING:
            print(f"[scheduler] Run {run_id} already running, skipping", file=sys.stderr)
            return

        print(f"[scheduler] Triggering run {run_id}", file=sys.stderr)
        await self.start_callback(run_id)

    async def load_existing_schedules(self) -> None:
        """Load all enabled schedules from DB on startup."""
        schedules = await self.state.list_schedules()
        for s in schedules:
            if s.enabled:
                await self.add_schedule(
                    s.id, s.run_id, s.schedule_type,
                    cron_expression=s.cron_expression,
                    start_time=s.start_time.isoformat() if s.start_time else None,
                )
        print(f"[scheduler] Loaded {len(schedules)} schedule(s)", file=sys.stderr)
