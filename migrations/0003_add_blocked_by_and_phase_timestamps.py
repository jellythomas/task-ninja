"""
Add blocked_by_keys column and phase duration tracking timestamps to tickets.
"""
from yoyo import step

__depends__ = {"0002_seed_default_profile"}

steps = [
    # Feature A1: Ticket dependency graph (blocked_by_keys)
    step(
        "ALTER TABLE tickets ADD COLUMN blocked_by_keys TEXT DEFAULT NULL",
        ignore_errors="apply",
    ),

    # Feature C2: Phase duration tracking
    step(
        "ALTER TABLE tickets ADD COLUMN planning_started_at TIMESTAMP DEFAULT NULL",
        ignore_errors="apply",
    ),
    step(
        "ALTER TABLE tickets ADD COLUMN planning_completed_at TIMESTAMP DEFAULT NULL",
        ignore_errors="apply",
    ),
    step(
        "ALTER TABLE tickets ADD COLUMN developing_started_at TIMESTAMP DEFAULT NULL",
        ignore_errors="apply",
    ),
    step(
        "ALTER TABLE tickets ADD COLUMN developing_completed_at TIMESTAMP DEFAULT NULL",
        ignore_errors="apply",
    ),
    step(
        "ALTER TABLE tickets ADD COLUMN review_started_at TIMESTAMP DEFAULT NULL",
        ignore_errors="apply",
    ),
    step(
        "ALTER TABLE tickets ADD COLUMN review_completed_at TIMESTAMP DEFAULT NULL",
        ignore_errors="apply",
    ),
]
