"""Ticket and Run models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class TicketState(str, Enum):
    TODO = "todo"
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


# Valid state transitions — permissive for kanban board manual moves
_ALL_STATES = set(TicketState)
VALID_TRANSITIONS = {state: _ALL_STATES for state in TicketState}

# States where a worker is active
ACTIVE_STATES = {TicketState.PLANNING, TicketState.DEVELOPING}


class Ticket(BaseModel):
    id: str
    run_id: str
    jira_key: str
    summary: Optional[str] = None
    state: TicketState = TicketState.TODO
    rank: int = 0
    branch_name: Optional[str] = None
    worktree_path: Optional[str] = None
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    worker_pid: Optional[int] = None
    paused: bool = False
    log_file: Optional[str] = None
    repository_id: Optional[int] = None
    parent_branch: Optional[str] = None
    profile_id: Optional[int] = None
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
    parent_branch: Optional[str] = None
    repository_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Repository(BaseModel):
    id: Optional[int] = None
    name: str
    path: str
    default_branch: str = "main"
    jira_label: Optional[str] = None  # e.g. "MC", "CKYC" — prefix for matching tickets
    default_profile_id: Optional[int] = None
    is_deleted: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class LabelRepoMapping(BaseModel):
    id: Optional[int] = None
    jira_label: str
    repository_id: int
    created_at: Optional[datetime] = None


class AgentProfile(BaseModel):
    id: Optional[int] = None
    name: str
    command: str
    args_template: str
    log_format: str = "plain-text"
    is_default: bool = False
    phases_config: Optional[str] = None  # JSON blob of phase pipeline config
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
    project_path: Optional[str] = None
    repository_id: Optional[int] = None
    parent_branch: Optional[str] = None
    max_parallel: int = 2


class LoadEpicRequest(BaseModel):
    epic_key: str


class FetchTicketsRequest(BaseModel):
    keys: list[str]


class TicketAssignment(BaseModel):
    repository_id: Optional[int] = None
    parent_branch: Optional[str] = None
    profile_id: Optional[int] = None


class AddTicketsRequest(BaseModel):
    keys: list[str]
    summaries: Optional[dict[str, str]] = None  # jira_key -> summary
    # Global fallback fields
    repository_id: Optional[int] = None
    parent_branch: Optional[str] = None
    profile_id: Optional[int] = None
    # Per-ticket overrides (takes precedence over global)
    assignments: Optional[dict[str, TicketAssignment]] = None


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


class UpdateScheduleRequest(BaseModel):
    enabled: Optional[bool] = None
    cron_expression: Optional[str] = None
    end_time: Optional[datetime] = None


# --- Repository & Settings request models ---

class CreateRepositoryRequest(BaseModel):
    name: str
    path: str
    default_branch: str = "main"
    jira_label: Optional[str] = None
    default_profile_id: Optional[int] = None


class UpdateRepositoryRequest(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None
    default_branch: Optional[str] = None
    jira_label: Optional[str] = None
    default_profile_id: Optional[int] = None


class CreateLabelMappingRequest(BaseModel):
    jira_label: str
    repository_id: int


class CreateAgentProfileRequest(BaseModel):
    name: str
    command: str
    args_template: str
    log_format: str = "plain-text"
    phases_config: Optional[str] = None


class UpdateAgentProfileRequest(BaseModel):
    name: Optional[str] = None
    command: Optional[str] = None
    args_template: Optional[str] = None
    log_format: Optional[str] = None
    phases_config: Optional[str] = None


class UpdateSettingsRequest(BaseModel):
    settings: dict[str, str]


class UpdateTicketAssignmentRequest(BaseModel):
    repository_id: Optional[int] = None
    parent_branch: Optional[str] = None
    profile_id: Optional[int] = None
