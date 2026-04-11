"""
Add PR template, GChat notification, and default reviewer config to repositories.

Enables task-ninja engine to handle PR creation and GChat notifications
directly (instead of burning AI context on these deterministic tasks).
"""
from yoyo import step

__depends__ = {"0007_add_prompt_submission_requeues"}

steps = [
    # PR description template (Markdown with {variables})
    step(
        "ALTER TABLE repositories ADD COLUMN pr_template TEXT DEFAULT NULL",
        ignore_errors="apply",
    ),
    # PR title format string, e.g. "{type}({ticket}): {summary} [FULL_COPILOT]"
    step(
        "ALTER TABLE repositories ADD COLUMN pr_title_format TEXT DEFAULT '${type}(${ticket}): ${summary} [FULL_COPILOT]'",
        ignore_errors="apply",
    ),
    # Default reviewers — JSON array of email addresses
    step(
        "ALTER TABLE repositories ADD COLUMN default_reviewers TEXT DEFAULT NULL",
        ignore_errors="apply",
    ),
    # Google Chat webhook URL for this repo's notifications
    step(
        "ALTER TABLE repositories ADD COLUMN gchat_webhook_url TEXT DEFAULT NULL",
        ignore_errors="apply",
    ),
    # Google Chat space name (display only)
    step(
        "ALTER TABLE repositories ADD COLUMN gchat_space_name TEXT DEFAULT NULL",
        ignore_errors="apply",
    ),
    # JSON array of event types to notify on
    step(
        """ALTER TABLE repositories ADD COLUMN gchat_events TEXT DEFAULT '["pr_created","pr_merged","ticket_failed","run_completed","review_comments"]'""",
        ignore_errors="apply",
    ),
    # Bot usernames to filter out from review comment triage (e.g. jenkins, ci-bot)
    step(
        """ALTER TABLE repositories ADD COLUMN review_bot_filter TEXT DEFAULT '["jenkins","ci-bot","bitbucket-pipelines"]'""",
        ignore_errors="apply",
    ),
]
