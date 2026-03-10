"""
Seed default Claude Code agent profile.
"""
import json
from yoyo import step

__depends__ = {"0001_initial_schema"}


def apply_seed(conn):
    """Seed default Claude Code agent profile if none exist."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM agent_profiles")
    count = cursor.fetchone()[0]

    if count == 0:
        default_phases = json.dumps([
            {"phase": "planning", "prompts": ["/planning-task {JIRA_KEY} parent:{PARENT_BRANCH}"], "marker": "[PLANNING_COMPLETE]"},
            {"phase": "developing", "prompts": ["/developing-task {JIRA_KEY} parent:{PARENT_BRANCH}"], "marker": "[DEVELOPING_COMPLETE]"},
            {"phase": "review", "prompts": ["/open-pr --draft parent:{PARENT_BRANCH}"], "marker": "[PR_COMPLETE]"},
        ])
        cursor.execute(
            """
            INSERT INTO agent_profiles (name, command, args_template, log_format, is_default, phases_config)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("Claude Code", "claude", "--dangerously-skip-permissions", "plain-text", 1, default_phases),
        )


def rollback_seed(conn):
    """Remove default agent profile."""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM agent_profiles WHERE name = 'Claude Code' AND is_default = 1")


steps = [
    step(apply_seed, rollback_seed),
]
