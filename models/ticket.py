"""Ticket and Run models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class TicketState(str, Enum):
    TODO = "todo"
    QUEUED = "queued"
    AWAITING_INPUT = "awaiting_input"
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
VALID_TRANSITIONS = dict.fromkeys(TicketState, _ALL_STATES)


class Ticket(BaseModel):
    id: str
    run_id: str
    jira_key: str
    summary: str | None = None
    state: TicketState = TicketState.TODO
    prompt_submit_requeues: int = 0
    rank: int = 0
    branch_name: str | None = None
    worktree_path: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    worker_pid: int | None = None
    paused: bool = False
    log_file: str | None = None
    repository_id: int | None = None
    parent_branch: str | None = None
    profile_id: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    input_type: str | None = None
    input_data: str | None = None  # JSON blob
    last_completed_phase: str | None = None  # planning, developing, review
    error: str | None = None
    blocked_by_keys: str | None = None  # JSON array: '["MC-101","MC-102"]'
    predicted_files: str | None = None  # JSON array of predicted file paths
    planning_started_at: datetime | None = None
    planning_completed_at: datetime | None = None
    developing_started_at: datetime | None = None
    developing_completed_at: datetime | None = None
    review_started_at: datetime | None = None
    review_completed_at: datetime | None = None
    pr_status: str | None = None  # "open", "merged", "declined", "draft"
    pr_approvals: int | None = None
    pr_comment_count: int | None = None
    pr_last_checked_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Run(BaseModel):
    id: str
    name: str
    epic_key: str | None = None
    max_parallel: int = 2
    status: RunStatus = RunStatus.IDLE
    project_path: str | None = None
    parent_branch: str | None = None
    repository_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Repository(BaseModel):
    id: int | None = None
    name: str
    path: str
    default_branch: str = "main"
    jira_label: str | None = None  # e.g. "MC", "CKYC" — prefix for matching tickets
    default_profile_id: int | None = None
    is_deleted: bool = False
    # PR creation config
    pr_template: str | None = None  # Markdown template with {variables}
    pr_title_format: str = "${type}(${ticket}): ${summary} [FULL_COPILOT]"
    default_reviewers: str | None = None  # JSON array of email addresses
    # GChat notification config
    gchat_webhook_url: str | None = None
    gchat_space_name: str | None = None
    gchat_events: str = '["pr_created","pr_merged","ticket_failed","run_completed","review_comments"]'
    # Review triage config
    review_bot_filter: str = '["jenkins","ci-bot","bitbucket-pipelines"]'
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LabelRepoMapping(BaseModel):
    id: int | None = None
    jira_label: str
    repository_id: int
    created_at: datetime | None = None


class AgentProfile(BaseModel):
    id: int | None = None
    name: str
    command: str
    args_template: str
    log_format: str = "plain-text"
    is_default: bool = False
    phases_config: str | None = None  # JSON blob of phase pipeline config
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Schedule(BaseModel):
    id: str
    run_id: str
    schedule_type: str  # one-time | recurring
    cron_expression: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    next_run: datetime | None = None
    enabled: bool = True
    created_at: datetime | None = None


# --- Request/Response models ---


class CreateRunRequest(BaseModel):
    name: str
    project_path: str | None = None
    repository_id: int | None = None
    parent_branch: str | None = None
    max_parallel: int = 2


class LoadEpicRequest(BaseModel):
    epic_key: str


class FetchTicketsRequest(BaseModel):
    keys: list[str]


class TicketAssignment(BaseModel):
    repository_id: int | None = None
    parent_branch: str | None = None
    profile_id: int | None = None


class AddTicketsRequest(BaseModel):
    keys: list[str]
    summaries: dict[str, str] | None = None  # jira_key -> summary
    blocked_by_keys: dict[str, list[str]] | None = None  # jira_key -> list of blocker keys
    predicted_files: dict[str, list[str]] | None = None  # jira_key -> list of predicted file paths
    # Global fallback fields
    repository_id: int | None = None
    parent_branch: str | None = None
    profile_id: int | None = None
    # Per-ticket overrides (takes precedence over global)
    assignments: dict[str, TicketAssignment] | None = None


class MoveTicketRequest(BaseModel):
    state: TicketState


class UpdateRankRequest(BaseModel):
    rank: int


class UpdateConfigRequest(BaseModel):
    max_parallel: int | None = None


class CreateScheduleRequest(BaseModel):
    run_id: str
    schedule_type: str
    cron_expression: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


class UpdateScheduleRequest(BaseModel):
    enabled: bool | None = None
    cron_expression: str | None = None
    end_time: datetime | None = None


# --- Repository & Settings request models ---


class CreateRepositoryRequest(BaseModel):
    name: str
    path: str
    default_branch: str = "main"
    jira_label: str | None = None
    default_profile_id: int | None = None


class UpdateRepositoryRequest(BaseModel):
    name: str | None = None
    path: str | None = None
    default_branch: str | None = None
    jira_label: str | None = None
    default_profile_id: int | None = None
    # PR creation config
    pr_template: str | None = None
    pr_title_format: str | None = None
    default_reviewers: str | None = None
    # GChat notification config
    gchat_webhook_url: str | None = None
    gchat_space_name: str | None = None
    gchat_events: str | None = None
    # Review triage config
    review_bot_filter: str | None = None


class CreateLabelMappingRequest(BaseModel):
    jira_label: str
    repository_id: int


class CreateAgentProfileRequest(BaseModel):
    name: str
    command: str
    args_template: str
    log_format: str = "plain-text"
    phases_config: str | None = None


class UpdateAgentProfileRequest(BaseModel):
    name: str | None = None
    command: str | None = None
    args_template: str | None = None
    log_format: str | None = None
    phases_config: str | None = None


class UpdateSettingsRequest(BaseModel):
    settings: dict[str, str]


class UpdateTicketAssignmentRequest(BaseModel):
    repository_id: int | None = None
    parent_branch: str | None = None
    profile_id: int | None = None


class ResolveInputRequest(BaseModel):
    choice: str  # "use_as_is" | "rebase" | "fresh_start"
