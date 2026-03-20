"""
Add predicted_files column to tickets for conflict-prevention batching.
"""
from yoyo import step

__depends__ = {"0005_add_pr_lifecycle"}

steps = [
    # Feature B1: Ticket batching / conflict prevention
    step(
        "ALTER TABLE tickets ADD COLUMN predicted_files TEXT DEFAULT NULL",
        ignore_errors="apply",
    ),
]
