"""Typed configuration for Task Ninja.

Wraps the existing config.yaml with a structured dataclass so callers get
attribute access and validation instead of raw dict lookups.

Backward compatibility: the raw dict is still available via `AppConfig.raw`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class OrchestratorConfig:
    poll_interval: int = 5


@dataclass
class ClaudeConfig:
    idle_timeout: int = 10


@dataclass
class JiraStatusMapping:
    planning: str = "In Progress"
    developing: str = "In Progress"
    review: str = "In Review"
    done: str = "Done"

    def as_dict(self) -> dict[str, str]:
        return {
            "planning": self.planning,
            "developing": self.developing,
            "review": self.review,
            "done": self.done,
        }


@dataclass
class McpConfig:
    jira_status_mapping: JiraStatusMapping = field(default_factory=JiraStatusMapping)


@dataclass
class GitConfig:
    worktree_dir: str = ".worktrees"
    branch_prefix: str = "feat"
    cleanup_worktrees: bool = True


@dataclass
class DatabaseConfig:
    path: str = "task_ninja.db"


@dataclass
class AppConfig:
    """Typed wrapper around config.yaml.  Access the raw dict via `.raw`."""

    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    git: GitConfig = field(default_factory=GitConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)

    # The raw dict is kept for backward compatibility with code that still
    # passes `config` as a dict (e.g. engine internals).
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, config_path: Path) -> AppConfig:
        """Load from a YAML file, falling back to defaults if file is missing."""
        if not config_path.exists():
            return cls()

        raw = yaml.safe_load(config_path.read_text()) or {}
        orch_raw = raw.get("orchestrator", {})
        claude_raw = raw.get("claude", {})
        mcp_raw = raw.get("mcp", {})
        git_raw = raw.get("git", {})
        db_raw = raw.get("database", {})

        jira_map_raw = mcp_raw.get("jira_status_mapping", {})

        return cls(
            orchestrator=OrchestratorConfig(
                poll_interval=orch_raw.get("poll_interval", 5),
            ),
            claude=ClaudeConfig(
                idle_timeout=claude_raw.get("idle_timeout", 10),
            ),
            mcp=McpConfig(
                jira_status_mapping=JiraStatusMapping(
                    planning=jira_map_raw.get("planning", "In Progress"),
                    developing=jira_map_raw.get("developing", "In Progress"),
                    review=jira_map_raw.get("review", "In Review"),
                    done=jira_map_raw.get("done", "Done"),
                ),
            ),
            git=GitConfig(
                worktree_dir=git_raw.get("worktree_dir", ".worktrees"),
                branch_prefix=git_raw.get("branch_prefix", "feat"),
                cleanup_worktrees=git_raw.get("cleanup_worktrees", True),
            ),
            database=DatabaseConfig(
                path=db_raw.get("path", "task_ninja.db"),
            ),
            raw=raw,
        )

    def resolve_db_path(self, project_root: Path) -> str:
        """Return an absolute path to the database file."""
        db_path = Path(self.database.path)
        if db_path.is_absolute():
            return str(db_path)
        return str(project_root / db_path)
