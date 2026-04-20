"""
Re-add review phase with empty prompts to all agent profiles.

The review phase keeps CLIs open during code review without submitting
any prompts.  The Worker auto-completes it (empty prompts) which
transitions the ticket to REVIEW state and triggers PR creation via
the orchestrator's post-review sweep.
"""
import json
from yoyo import step

__depends__ = {"0011_restrict_gchat_events_default"}


def apply_step(conn):
    """Append review phase with empty prompts to every profile."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, phases_config FROM agent_profiles WHERE phases_config IS NOT NULL")
    rows = cursor.fetchall()

    for row in rows:
        profile_id, phases_json = row
        try:
            phases = json.loads(phases_json)
        except (json.JSONDecodeError, TypeError):
            continue

        # Skip if review phase already exists
        if any(p.get("phase") == "review" for p in phases):
            continue

        phases.append({"phase": "review", "prompts": []})

        cursor.execute(
            "UPDATE agent_profiles SET phases_config = ? WHERE id = ?",
            (json.dumps(phases), profile_id),
        )


def rollback_step(conn):
    """Remove the review phase from all profiles."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, phases_config FROM agent_profiles WHERE phases_config IS NOT NULL")
    rows = cursor.fetchall()

    for row in rows:
        profile_id, phases_json = row
        try:
            phases = json.loads(phases_json)
        except (json.JSONDecodeError, TypeError):
            continue

        phases = [p for p in phases if p.get("phase") != "review"]

        cursor.execute(
            "UPDATE agent_profiles SET phases_config = ? WHERE id = ?",
            (json.dumps(phases), profile_id),
        )


steps = [
    step(apply_step, rollback_step),
]
