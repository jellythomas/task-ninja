"""
Add prompt_submit_requeues column to tickets for deterministic prompt submission retries.
"""
from yoyo import step

__depends__ = {"0006_add_predicted_files"}

steps = [
    step(
        "ALTER TABLE tickets ADD COLUMN prompt_submit_requeues INTEGER DEFAULT 0",
        ignore_errors="apply",
    ),
]
