"""Ticket and Run models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class TicketState(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    PLANNING = "planning"
    DEVELOPING = "developing"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


class RunStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


# Valid state transitions
VALID_TRANSITIONS = {
    TicketState.PENDING: {TicketState.QUEUED, TicketState.PENDING},
    TicketState.QUEUED: {TicketState.PLANNING, TicketState.PENDING, TicketState.QUEUED, TicketState.FAILED},
    TicketState.PLANNING: {TicketState.DEVELOPING, TicketState.QUEUED, TicketState.FAILED, TicketState.PENDING},
    TicketState.DEVELOPING: {TicketState.REVIEW, TicketState.QUEUED, TicketState.FAILED, TicketState.PENDING},
    TicketState.REVIEW: {TicketState.DEVELOPING, TicketState.QUEUED, TicketState.DONE, TicketState.PENDING},
    TicketState.DONE: {TicketState.PENDING, TicketState.QUEUED},
    TicketState.FAILED: {TicketState.QUEUED, TicketState.PENDING},
}

# States where a worker is active
ACTIVE_STATES = {TicketState.PLANNING, TicketState.DEVELOPING}


class Ticket(BaseModel):
    id: str
    run_id: str
    jira_key: str
    summary: Optional[str] = None
    state: TicketState = TicketState.PENDING
    rank: int = 0
    branch_name: Optional[str] = None
    worktree_path: Optional[str] = None
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    worker_pid: Optional[int] = None
    paused: bool = False
    log_file: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Run(BaseModel):
    id: str
    name: str
    epic_key: Optional[str] = None
    max_parallel: int = 2
    status: RunStatus = RunStatus.IDLE
    project_path: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Schedule(BaseModel):
    id: str
    run_id: str
    schedule_type: str  # one-time | recurring
    cron_expression: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    next_run: Optional[datetime] = None
    enabled: bool = True
    created_at: Optional[datetime] = None


# --- Request/Response models ---

class CreateRunRequest(BaseModel):
    name: str
    project_path: str
    max_parallel: int = 2


class LoadEpicRequest(BaseModel):
    epic_key: str


class AddTicketsRequest(BaseModel):
    keys: list[str]
    summaries: Optional[dict[str, str]] = None  # jira_key -> summary


class MoveTicketRequest(BaseModel):
    state: TicketState


class UpdateRankRequest(BaseModel):
    rank: int


class UpdateConfigRequest(BaseModel):
    max_parallel: Optional[int] = None
    skip_permissions: Optional[bool] = None
    worker_timeout: Optional[int] = None


class CreateScheduleRequest(BaseModel):
    run_id: str
    schedule_type: str
    cron_expression: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
