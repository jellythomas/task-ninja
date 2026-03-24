"""SQLite state management for runs, tickets, schedules, and logs."""

from __future__ import annotations

import logging
import uuid

from datetime import datetime
from pathlib import Path

import aiosqlite

from models.ticket import (
    VALID_TRANSITIONS,
    AgentProfile,
    LabelRepoMapping,
    Repository,
    Run,
    RunStatus,
    Schedule,
    Ticket,
    TicketState,
)

logger = logging.getLogger(__name__)

# Use absolute path to ensure worker subprocesses can find the database
# even when running with a different cwd (e.g., git worktree directory)
DB_PATH = str(Path(__file__).parent.parent / "task_ninja.db")
MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def init_db(db_path: str = DB_PATH) -> None:
    """Initialize the database with schema and run migrations using yoyo."""
    from engine.migrator import ensure_yoyo_installed, run_migrations

    # Ensure yoyo is installed
    ensure_yoyo_installed()

    # Run migrations
    applied, _pending = run_migrations(db_path)
    if applied > 0:
        logger.info("Applied %d migration(s)", applied)


def _generate_id() -> str:
    return str(uuid.uuid4())[:8]


class StateManager:
    """Manages all database state operations."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def _connect(self) -> aiosqlite.Connection:
        db = aiosqlite.connect(self.db_path)
        return db

    async def _setup_db(self, db: aiosqlite.Connection) -> None:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

    # --- Runs ---

    async def create_run(self, name: str, project_path: str, max_parallel: int = 2, epic_key: str | None = None) -> Run:
        run_id = _generate_id()
        now = datetime.utcnow().isoformat()
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(
                "INSERT INTO runs (id, name, epic_key, max_parallel, status, project_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, name, epic_key, max_parallel, RunStatus.IDLE, project_path, now, now),
            )
            await db.commit()
        return await self.get_run(run_id)

    async def get_run(self, run_id: str) -> Run | None:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return Run(**dict(row))

    async def list_runs(self) -> list[Run]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM runs ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            return [Run(**dict(r)) for r in rows]

    async def update_run_status(self, run_id: str, status: RunStatus) -> None:
        now = datetime.utcnow().isoformat()
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, run_id),
            )
            await db.commit()

    async def update_run_config(self, run_id: str, **kwargs) -> None:
        now = datetime.utcnow().isoformat()
        sets = ["updated_at = ?"]
        vals = [now]
        for key, val in kwargs.items():
            if val is not None:
                sets.append(f"{key} = ?")
                vals.append(val)
        vals.append(run_id)
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", vals)  # noqa: S608
            await db.commit()

    async def delete_run(self, run_id: str) -> None:
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            await db.commit()

    # --- Tickets ---

    async def add_ticket(
        self, run_id: str, jira_key: str, summary: str | None = None, state: TicketState = TicketState.TODO
    ) -> Ticket:
        ticket_id = _generate_id()
        now = datetime.utcnow().isoformat()
        # Get next rank
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT COALESCE(MAX(rank), -1) + 1 FROM tickets WHERE run_id = ?", (run_id,))
            row = await cursor.fetchone()
            rank = row[0] if row else 0

            await db.execute(
                "INSERT INTO tickets (id, run_id, jira_key, summary, state, rank, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, run_id, jira_key, summary, state, rank, now, now),
            )
            await db.commit()
        return await self.get_ticket(ticket_id)

    async def get_ticket(self, ticket_id: str) -> Ticket | None:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return Ticket(**dict(row))

    async def get_ticket_by_jira_key(self, run_id: str, jira_key: str) -> Ticket | None:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM tickets WHERE run_id = ? AND jira_key = ?", (run_id, jira_key))
            row = await cursor.fetchone()
            if not row:
                return None
            return Ticket(**dict(row))

    async def get_tickets_for_run(self, run_id: str) -> list[Ticket]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM tickets WHERE run_id = ? ORDER BY rank", (run_id,))
            rows = await cursor.fetchall()
            return [Ticket(**dict(r)) for r in rows]

    async def get_tickets_by_state(self, run_id: str, state: TicketState) -> list[Ticket]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                "SELECT * FROM tickets WHERE run_id = ? AND state = ? ORDER BY rank",
                (run_id, state),
            )
            rows = await cursor.fetchall()
            return [Ticket(**dict(r)) for r in rows]

    async def update_ticket_state(self, ticket_id: str, new_state: TicketState) -> Ticket:
        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")

        current = TicketState(ticket.state)
        if new_state not in VALID_TRANSITIONS.get(current, set()):
            raise ValueError(f"Invalid transition: {current} -> {new_state}")

        now = datetime.utcnow().isoformat()
        updates = {"state": new_state, "updated_at": now}

        if new_state == TicketState.PLANNING:
            if not ticket.started_at:
                updates["started_at"] = now
        if new_state == TicketState.DONE:
            updates["completed_at"] = now
        # Phase timestamps (started_at / completed_at) are set explicitly by
        # the worker at the actual lifecycle moments — not during state transitions.
        if new_state in {TicketState.TODO, TicketState.QUEUED}:
            updates["paused"] = False
            updates["worker_pid"] = None
            updates["error"] = None

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = [*list(updates.values()), ticket_id]

        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(f"UPDATE tickets SET {set_clause} WHERE id = ?", vals)  # noqa: S608
            await db.commit()
        return await self.get_ticket(ticket_id)

    async def update_ticket(self, ticket_id: str, **kwargs) -> None:
        now = datetime.utcnow().isoformat()
        kwargs["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        vals = [*list(kwargs.values()), ticket_id]
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(f"UPDATE tickets SET {set_clause} WHERE id = ?", vals)  # noqa: S608
            await db.commit()

    async def update_ticket_rank(self, ticket_id: str, rank: int) -> None:
        await self.update_ticket(ticket_id, rank=rank)

    async def delete_ticket(self, ticket_id: str) -> Ticket | None:
        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            return None
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            await db.commit()
        return ticket

    async def get_tickets_by_jira_keys(self, run_id: str, jira_keys: list[str]) -> list[Ticket]:
        """Return tickets in a run whose jira_key matches any of the given keys."""
        if not jira_keys:
            return []
        placeholders = ", ".join("?" * len(jira_keys))
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                f"SELECT * FROM tickets WHERE run_id = ? AND jira_key IN ({placeholders})",  # noqa: S608
                [run_id, *jira_keys],
            )
            rows = await cursor.fetchall()
            return [Ticket(**dict(r)) for r in rows]

    async def count_dependents(self, run_id: str, jira_key: str) -> int:
        """Count tickets in the run that list jira_key as a blocker in blocked_by_keys."""
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                'SELECT COUNT(*) FROM tickets WHERE run_id = ? AND blocked_by_keys LIKE ?',  # noqa: S608
                (run_id, f'%"{jira_key}"%'),
            )
            row = await cursor.fetchone()
            return row[0]

    async def count_active_tickets(self, run_id: str) -> int:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE run_id = ? AND state IN (?, ?) AND paused = FALSE",
                (run_id, TicketState.PLANNING, TicketState.DEVELOPING),
            )
            row = await cursor.fetchone()
            return row[0]

    # --- Logs ---

    async def append_log(self, ticket_id: str, line: str) -> None:
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(
                "INSERT INTO logs (ticket_id, line) VALUES (?, ?)",
                (ticket_id, line),
            )
            # Auto-trim: keep only last 500 lines per ticket in DB
            await db.execute(
                "DELETE FROM logs WHERE ticket_id = ? AND id NOT IN "
                "(SELECT id FROM logs WHERE ticket_id = ? ORDER BY id DESC LIMIT 500)",
                (ticket_id, ticket_id),
            )
            await db.commit()

    async def get_logs(self, ticket_id: str, tail: int = 200) -> list[dict]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                "SELECT timestamp, line FROM logs WHERE ticket_id = ? ORDER BY id DESC LIMIT ?",
                (ticket_id, tail),
            )
            rows = await cursor.fetchall()
            return [{"timestamp": r["timestamp"], "line": r["line"]} for r in reversed(rows)]

    # --- Schedules ---

    async def create_schedule(self, run_id: str, schedule_type: str, **kwargs) -> Schedule:
        schedule_id = _generate_id()
        now = datetime.utcnow().isoformat()
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(
                "INSERT INTO schedules (id, run_id, schedule_type, cron_expression, start_time, end_time, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    schedule_id,
                    run_id,
                    schedule_type,
                    kwargs.get("cron_expression"),
                    kwargs.get("start_time"),
                    kwargs.get("end_time"),
                    True,
                    now,
                ),
            )
            await db.commit()
        return await self.get_schedule(schedule_id)

    async def get_schedule(self, schedule_id: str) -> Schedule | None:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return Schedule(**dict(row))

    async def list_schedules(self, run_id: str | None = None) -> list[Schedule]:
        async with self._connect() as db:
            await self._setup_db(db)
            if run_id:
                cursor = await db.execute("SELECT * FROM schedules WHERE run_id = ?", (run_id,))
            else:
                cursor = await db.execute("SELECT * FROM schedules")
            rows = await cursor.fetchall()
            return [Schedule(**dict(r)) for r in rows]

    async def update_schedule(self, schedule_id: str, **kwargs) -> Schedule | None:
        updates = {k: v for k, v in kwargs.items() if v is not None}
        if not updates:
            return await self.get_schedule(schedule_id)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = [*list(updates.values()), schedule_id]
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(f"UPDATE schedules SET {set_clause} WHERE id = ?", values)  # noqa: S608
            await db.commit()
        return await self.get_schedule(schedule_id)

    async def delete_schedule(self, schedule_id: str) -> None:
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            await db.commit()

    # --- Repositories ---

    async def create_repository(
        self,
        name: str,
        path: str,
        default_branch: str = "main",
        jira_label: str | None = None,
        default_profile_id: int | None = None,
    ) -> Repository:
        now = datetime.utcnow().isoformat()
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                "INSERT INTO repositories (name, path, default_branch, jira_label, default_profile_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, path, default_branch, jira_label, default_profile_id, now, now),
            )
            repo_id = cursor.lastrowid
            await db.commit()
        return await self.get_repository(repo_id)

    async def get_repository(self, repo_id: int) -> Repository | None:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM repositories WHERE id = ?", (repo_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return Repository(**dict(row))

    async def list_repositories(self, include_deleted: bool = False) -> list[Repository]:
        async with self._connect() as db:
            await self._setup_db(db)
            if include_deleted:
                cursor = await db.execute("SELECT * FROM repositories ORDER BY name")
            else:
                cursor = await db.execute("SELECT * FROM repositories WHERE is_deleted = 0 ORDER BY name")
            rows = await cursor.fetchall()
            return [Repository(**dict(r)) for r in rows]

    async def update_repository(self, repo_id: int, **kwargs) -> Repository | None:
        now = datetime.utcnow().isoformat()
        kwargs["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        vals = [*list(kwargs.values()), repo_id]
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(f"UPDATE repositories SET {set_clause} WHERE id = ?", vals)  # noqa: S608
            await db.commit()
        return await self.get_repository(repo_id)

    async def delete_repository(self, repo_id: int) -> None:
        """Soft-delete if tickets reference this repo, hard-delete otherwise."""
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT COUNT(*) FROM tickets WHERE repository_id = ?", (repo_id,))
            count = (await cursor.fetchone())[0]
            if count > 0:
                now = datetime.utcnow().isoformat()
                await db.execute(
                    "UPDATE repositories SET is_deleted = 1, updated_at = ? WHERE id = ?",
                    (now, repo_id),
                )
            else:
                await db.execute("DELETE FROM label_repo_mappings WHERE repository_id = ?", (repo_id,))
                await db.execute("DELETE FROM repositories WHERE id = ?", (repo_id,))
            await db.commit()

    # --- Label-Repo Mappings ---

    async def create_label_mapping(self, jira_label: str, repository_id: int) -> LabelRepoMapping:
        now = datetime.utcnow().isoformat()
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                "INSERT INTO label_repo_mappings (jira_label, repository_id, created_at) VALUES (?, ?, ?)",
                (jira_label, repository_id, now),
            )
            mapping_id = cursor.lastrowid
            await db.commit()
        return await self.get_label_mapping(mapping_id)

    async def get_label_mapping(self, mapping_id: int) -> LabelRepoMapping | None:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM label_repo_mappings WHERE id = ?", (mapping_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return LabelRepoMapping(**dict(row))

    async def list_label_mappings(self) -> list[LabelRepoMapping]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM label_repo_mappings ORDER BY jira_label")
            rows = await cursor.fetchall()
            return [LabelRepoMapping(**dict(r)) for r in rows]

    async def delete_label_mapping(self, mapping_id: int) -> None:
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute("DELETE FROM label_repo_mappings WHERE id = ?", (mapping_id,))
            await db.commit()

    # --- Agent Profiles ---

    async def create_agent_profile(
        self,
        name: str,
        command: str,
        args_template: str,
        log_format: str = "plain-text",
        phases_config: str | None = None,
    ) -> AgentProfile:
        now = datetime.utcnow().isoformat()
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                "INSERT INTO agent_profiles (name, command, args_template, log_format, phases_config, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, command, args_template, log_format, phases_config, now, now),
            )
            profile_id = cursor.lastrowid
            await db.commit()
        return await self.get_agent_profile(profile_id)

    async def get_agent_profile(self, profile_id: int) -> AgentProfile | None:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM agent_profiles WHERE id = ?", (profile_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return AgentProfile(**dict(row))

    async def get_default_agent_profile(self) -> AgentProfile | None:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM agent_profiles WHERE is_default = 1 LIMIT 1")
            row = await cursor.fetchone()
            if not row:
                return None
            return AgentProfile(**dict(row))

    async def list_agent_profiles(self) -> list[AgentProfile]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM agent_profiles ORDER BY name")
            rows = await cursor.fetchall()
            return [AgentProfile(**dict(r)) for r in rows]

    async def update_agent_profile(self, profile_id: int, **kwargs) -> AgentProfile | None:
        now = datetime.utcnow().isoformat()
        kwargs["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        vals = [*list(kwargs.values()), profile_id]
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(f"UPDATE agent_profiles SET {set_clause} WHERE id = ?", vals)  # noqa: S608
            await db.commit()
        return await self.get_agent_profile(profile_id)

    async def set_default_agent_profile(self, profile_id: int) -> None:
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute("UPDATE agent_profiles SET is_default = 0")
            await db.execute("UPDATE agent_profiles SET is_default = 1 WHERE id = ?", (profile_id,))
            await db.commit()

    async def delete_agent_profile(self, profile_id: int) -> None:
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute("DELETE FROM agent_profiles WHERE id = ?", (profile_id,))
            await db.commit()

    # --- Settings (key-value) ---

    async def get_setting(self, key: str) -> str | None:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_all_settings(self) -> dict[str, str]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT key, value FROM settings")
            rows = await cursor.fetchall()
            return {r["key"]: r["value"] for r in rows}

    async def set_setting(self, key: str, value: str) -> None:
        now = datetime.utcnow().isoformat()
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
                (key, value, now, value, now),
            )
            await db.commit()

    async def set_settings(self, settings: dict[str, str]) -> None:
        now = datetime.utcnow().isoformat()
        async with self._connect() as db:
            await self._setup_db(db)
            for key, value in settings.items():
                await db.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
                    (key, value, now, value, now),
                )
            await db.commit()

    async def delete_setting(self, key: str) -> None:
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute("DELETE FROM settings WHERE key = ?", (key,))
            await db.commit()

    # --- Analytics ---

    async def get_run_analytics(self, run_id: str) -> dict:
        """Compute analytics for a run's tickets."""
        async with self._connect() as db:
            await self._setup_db(db)

            # Main stats query
            cursor = await db.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN state = 'done' THEN 1 ELSE 0 END) as done,
                    SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) as failed,
                    SUM(CASE WHEN state = 'review' THEN 1 ELSE 0 END) as review,
                    AVG(CASE WHEN planning_started_at IS NOT NULL AND planning_completed_at IS NOT NULL
                        THEN (CAST(strftime('%s', planning_completed_at) AS REAL) -
                              CAST(strftime('%s', planning_started_at) AS REAL)) / 60.0
                        END) as avg_planning_min,
                    AVG(CASE WHEN developing_started_at IS NOT NULL AND developing_completed_at IS NOT NULL
                        THEN (CAST(strftime('%s', developing_completed_at) AS REAL) -
                              CAST(strftime('%s', developing_started_at) AS REAL)) / 60.0
                        END) as avg_developing_min,
                    AVG(CASE WHEN review_started_at IS NOT NULL AND review_completed_at IS NOT NULL
                        THEN (CAST(strftime('%s', review_completed_at) AS REAL) -
                              CAST(strftime('%s', review_started_at) AS REAL)) / 60.0
                        END) as avg_review_min,
                    AVG(CASE WHEN started_at IS NOT NULL AND completed_at IS NOT NULL
                        THEN (CAST(strftime('%s', completed_at) AS REAL) -
                              CAST(strftime('%s', started_at) AS REAL)) / 60.0
                        END) as avg_total_min
                FROM tickets
                WHERE run_id = ? AND state IN ('done', 'failed', 'review')
                """,
                (run_id,),
            )
            row = await cursor.fetchone()

            total = row["total"] or 0
            done = row["done"] or 0
            failed = row["failed"] or 0
            review = row["review"] or 0
            success_rate = round(done / total * 100, 1) if total > 0 else 0.0

            avg_planning_min = round(row["avg_planning_min"], 1) if row["avg_planning_min"] is not None else None
            avg_developing_min = round(row["avg_developing_min"], 1) if row["avg_developing_min"] is not None else None
            avg_review_min = round(row["avg_review_min"], 1) if row["avg_review_min"] is not None else None
            avg_total_min = round(row["avg_total_min"], 1) if row["avg_total_min"] is not None else None

            # Determine bottleneck (phase with longest average duration)
            phases = {
                "planning": avg_planning_min,
                "developing": avg_developing_min,
                "review": avg_review_min,
            }
            bottleneck = max(
                (k for k, v in phases.items() if v is not None),
                key=lambda k: phases[k],
                default=None,
            )

            # Fastest/slowest tickets by total duration
            cursor2 = await db.execute(
                """
                SELECT jira_key,
                    (CAST(strftime('%s', completed_at) AS REAL) -
                     CAST(strftime('%s', started_at) AS REAL)) / 60.0 as duration_min
                FROM tickets
                WHERE run_id = ? AND state = 'done'
                    AND started_at IS NOT NULL AND completed_at IS NOT NULL
                ORDER BY duration_min ASC
                """,
                (run_id,),
            )
            duration_rows = await cursor2.fetchall()

            fastest = None
            slowest = None
            if duration_rows:
                fastest = {
                    "jira_key": duration_rows[0]["jira_key"],
                    "duration_min": round(duration_rows[0]["duration_min"], 1),
                }
                slowest = {
                    "jira_key": duration_rows[-1]["jira_key"],
                    "duration_min": round(duration_rows[-1]["duration_min"], 1),
                }

            return {
                "run_id": run_id,
                "total": total,
                "done": done,
                "failed": failed,
                "review": review,
                "success_rate": success_rate,
                "avg_planning_min": avg_planning_min,
                "avg_developing_min": avg_developing_min,
                "avg_review_min": avg_review_min,
                "avg_total_min": avg_total_min,
                "bottleneck": bottleneck,
                "fastest": fastest,
                "slowest": slowest,
            }

    async def get_weekly_trends(self, weeks: int = 12) -> list[dict]:
        """Get weekly aggregated trends across all runs."""
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                """
                SELECT
                    strftime('%Y-W%W', completed_at) as week,
                    COUNT(*) as total,
                    SUM(CASE WHEN state = 'done' THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) as failed,
                    AVG(CASE WHEN started_at IS NOT NULL AND completed_at IS NOT NULL
                        THEN (CAST(strftime('%s', completed_at) AS REAL) -
                              CAST(strftime('%s', started_at) AS REAL)) / 60.0
                        END) as avg_duration_min
                FROM tickets
                WHERE completed_at IS NOT NULL AND state IN ('done', 'failed')
                GROUP BY week
                ORDER BY week DESC
                LIMIT ?
                """,
                (weeks,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "week": r["week"],
                    "total": r["total"],
                    "success": r["success"],
                    "failed": r["failed"],
                    "avg_duration_min": round(r["avg_duration_min"] or 0, 1),
                }
                for r in rows
            ]

    # --- Queue Estimates ---

    async def get_avg_ticket_duration(self, run_id: str) -> float | None:
        """Get average total duration in seconds for completed tickets in this run."""
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                "SELECT AVG(CAST(strftime('%s', completed_at) AS REAL) - "
                "CAST(strftime('%s', started_at) AS REAL)) "
                "FROM tickets WHERE run_id = ? AND state = ? AND completed_at IS NOT NULL AND started_at IS NOT NULL",
                (run_id, TicketState.DONE),
            )
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None
