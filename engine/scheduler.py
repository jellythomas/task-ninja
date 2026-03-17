"""Scheduler — triggers runs on a schedule using APScheduler."""

from __future__ import annotations

import logging

from collections.abc import Callable
from datetime import datetime
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from engine.state import StateManager
from models.ticket import RunStatus

logger = logging.getLogger(__name__)


class RunScheduler:
    """Manages scheduled run execution."""

    def __init__(self, state: StateManager, start_callback: Callable[..., Any]) -> None:
        self.state = state
        self.start_callback = start_callback  # async def start(run_id) -> None
        self.scheduler = AsyncIOScheduler()
        self._started = False

    def start(self) -> None:
        if not self._started:
            self.scheduler.start()
            self._started = True
            logger.info("Started")

    def stop(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False
            logger.info("Stopped")

    async def add_schedule(
        self,
        schedule_id: str,
        run_id: str,
        schedule_type: str,
        cron_expression: str | None = None,
        start_time: str | None = None,
    ) -> None:
        """Add a scheduled job."""
        job_id = f"schedule_{schedule_id}"

        if schedule_type == "recurring" and cron_expression:
            trigger = CronTrigger.from_crontab(cron_expression)
            self.scheduler.add_job(
                self._execute_run,
                trigger,
                id=job_id,
                args=[run_id],
                replace_existing=True,
            )
            logger.info("Added recurring job %s: %s", job_id, cron_expression)

        elif schedule_type == "one-time" and start_time:
            run_at = datetime.fromisoformat(start_time)
            trigger = DateTrigger(run_date=run_at)
            self.scheduler.add_job(
                self._execute_run,
                trigger,
                id=job_id,
                args=[run_id],
                replace_existing=True,
            )
            logger.info("Added one-time job %s: %s", job_id, start_time)

    def remove_schedule(self, schedule_id: str) -> None:
        job_id = f"schedule_{schedule_id}"
        try:
            self.scheduler.remove_job(job_id)
            logger.info("Removed job %s", job_id)
        except JobLookupError:
            logger.debug("Job %s not found, already removed", job_id)

    async def _execute_run(self, run_id: str) -> None:
        """Execute a scheduled run if it's not already running."""
        run = await self.state.get_run(run_id)
        if not run:
            logger.warning("Run %s not found, skipping", run_id)
            return

        if run.status == RunStatus.RUNNING:
            logger.info("Run %s already running, skipping", run_id)
            return

        logger.info("Triggering run %s", run_id)
        await self.start_callback(run_id)

    async def load_existing_schedules(self) -> None:
        """Load all enabled schedules from DB on startup."""
        schedules = await self.state.list_schedules()
        for s in schedules:
            if s.enabled:
                await self.add_schedule(
                    s.id,
                    s.run_id,
                    s.schedule_type,
                    cron_expression=s.cron_expression,
                    start_time=s.start_time.isoformat() if s.start_time else None,
                )
        logger.info("Loaded %d schedule(s)", len(schedules))
