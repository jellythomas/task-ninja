"""
Add pr_approvals and pr_comment_count columns to tickets for PR lifecycle tracking.
"""
from yoyo import step

__depends__ = {"0004_add_pr_tracking"}

steps = [
    # Feature B3: PR lifecycle management
    step(
        "ALTER TABLE tickets ADD COLUMN pr_approvals INTEGER DEFAULT NULL",
        ignore_errors="apply",
    ),
    step(
        "ALTER TABLE tickets ADD COLUMN pr_comment_count INTEGER DEFAULT NULL",
        ignore_errors="apply",
    ),
]
