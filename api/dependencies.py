"""FastAPI dependency providers — shared singletons injected via app.state."""

from __future__ import annotations

from fastapi import Request

from engine.broadcaster import Broadcaster
from engine.claude_helper import ClaudeHelper
from engine.jira_client import JiraClient
from engine.notifier import Notifier
from engine.orchestrator import Orchestrator
from engine.scheduler import RunScheduler
from engine.state import StateManager
from engine.terminal import TerminalManager


def get_state(request: Request) -> StateManager:
    return request.app.state.state


def get_orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


def get_broadcaster(request: Request) -> Broadcaster:
    return request.app.state.broadcaster


def get_jira_client(request: Request) -> JiraClient:
    return request.app.state.jira_client


def get_claude_helper(request: Request) -> ClaudeHelper:
    return request.app.state.claude_helper


def get_notifier(request: Request) -> Notifier:
    return request.app.state.notifier


def get_run_scheduler(request: Request) -> RunScheduler:
    return request.app.state.run_scheduler


def get_terminal_manager(request: Request) -> TerminalManager:
    return request.app.state.terminal_manager


def get_config(request: Request) -> dict:
    return request.app.state.config
