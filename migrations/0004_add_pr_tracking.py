"""
Add pr_status and pr_last_checked_at columns to tickets for PR status tracking.
"""
from yoyo import step

__depends__ = {"0003_add_blocked_by_and_phase_timestamps"}

steps = [
    # Feature A5: PR status tracking
    step(
        "ALTER TABLE tickets ADD COLUMN pr_status TEXT DEFAULT NULL",
        ignore_errors="apply",
    ),
    step(
        "ALTER TABLE tickets ADD COLUMN pr_last_checked_at TIMESTAMP DEFAULT NULL",
        ignore_errors="apply",
    ),
]
