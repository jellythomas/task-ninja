"""SQLite state management for runs, tickets, schedules, and logs."""

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from models.ticket import (
    Run, RunStatus, Schedule, Ticket, TicketState,
    VALID_TRANSITIONS,
)

DB_PATH = "autonomous_task.db"
MIGRATIONS_PATH = Path(__file__).parent.parent / "migrations" / "init.sql"


async def init_db(db_path: str = DB_PATH) -> None:
    """Initialize the database with schema."""
    async with aiosqlite.connect(db_path) as db:
        sql = MIGRATIONS_PATH.read_text()
        await db.executescript(sql)
        await db.commit()


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

    async def create_run(self, name: str, project_path: str, max_parallel: int = 2, epic_key: str = None) -> Run:
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

    async def get_run(self, run_id: str) -> Optional[Run]:
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
            await db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", vals)
            await db.commit()

    async def delete_run(self, run_id: str) -> None:
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            await db.commit()

    # --- Tickets ---

    async def add_ticket(self, run_id: str, jira_key: str, summary: str = None, state: TicketState = TicketState.PENDING) -> Ticket:
        ticket_id = _generate_id()
        now = datetime.utcnow().isoformat()
        # Get next rank
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                "SELECT COALESCE(MAX(rank), -1) + 1 FROM tickets WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            rank = row[0] if row else 0

            await db.execute(
                "INSERT INTO tickets (id, run_id, jira_key, summary, state, rank, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, run_id, jira_key, summary, state, rank, now, now),
            )
            await db.commit()
        return await self.get_ticket(ticket_id)

    async def get_ticket(self, ticket_id: str) -> Optional[Ticket]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return Ticket(**dict(row))

    async def get_ticket_by_jira_key(self, run_id: str, jira_key: str) -> Optional[Ticket]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                "SELECT * FROM tickets WHERE run_id = ? AND jira_key = ?", (run_id, jira_key)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return Ticket(**dict(row))

    async def get_tickets_for_run(self, run_id: str) -> list[Ticket]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute(
                "SELECT * FROM tickets WHERE run_id = ? ORDER BY rank", (run_id,)
            )
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

        if new_state in {TicketState.PLANNING} and not ticket.started_at:
            updates["started_at"] = now
        if new_state == TicketState.DONE:
            updates["completed_at"] = now
        if new_state in {TicketState.PENDING, TicketState.QUEUED}:
            updates["paused"] = False
            updates["worker_pid"] = None
            updates["error"] = None

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [ticket_id]

        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(f"UPDATE tickets SET {set_clause} WHERE id = ?", vals)
            await db.commit()
        return await self.get_ticket(ticket_id)

    async def update_ticket(self, ticket_id: str, **kwargs) -> None:
        now = datetime.utcnow().isoformat()
        kwargs["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [ticket_id]
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute(f"UPDATE tickets SET {set_clause} WHERE id = ?", vals)
            await db.commit()

    async def update_ticket_rank(self, ticket_id: str, rank: int) -> None:
        await self.update_ticket(ticket_id, rank=rank)

    async def delete_ticket(self, ticket_id: str) -> Optional[Ticket]:
        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            return None
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            await db.commit()
        return ticket

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
                    schedule_id, run_id, schedule_type,
                    kwargs.get("cron_expression"),
                    kwargs.get("start_time"),
                    kwargs.get("end_time"),
                    True, now,
                ),
            )
            await db.commit()
        return await self.get_schedule(schedule_id)

    async def get_schedule(self, schedule_id: str) -> Optional[Schedule]:
        async with self._connect() as db:
            await self._setup_db(db)
            cursor = await db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return Schedule(**dict(row))

    async def list_schedules(self, run_id: str = None) -> list[Schedule]:
        async with self._connect() as db:
            await self._setup_db(db)
            if run_id:
                cursor = await db.execute("SELECT * FROM schedules WHERE run_id = ?", (run_id,))
            else:
                cursor = await db.execute("SELECT * FROM schedules")
            rows = await cursor.fetchall()
            return [Schedule(**dict(r)) for r in rows]

    async def delete_schedule(self, schedule_id: str) -> None:
        async with self._connect() as db:
            await self._setup_db(db)
            await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            await db.commit()
