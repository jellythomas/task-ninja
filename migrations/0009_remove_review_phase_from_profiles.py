"""
Remove review phase from agent profiles — PR creation is now handled by task-ninja engine.

Also removes marker config from phases since markers are now system-defined
(injected by the engine, not user-configurable).
"""
import json
from yoyo import step

__depends__ = {"0008_add_repo_pr_and_gchat_config"}


def apply_step(conn):
    """Remove review phase from all agent profiles."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, phases_config FROM agent_profiles WHERE phases_config IS NOT NULL")
    rows = cursor.fetchall()

    for row in rows:
        profile_id, phases_json = row
        try:
            phases = json.loads(phases_json)
        except (json.JSONDecodeError, TypeError):
            continue

        # Remove review phase
        new_phases = [p for p in phases if p.get("phase") != "review"]

        # Remove marker config — system handles this now
        for p in new_phases:
            p.pop("marker", None)

        cursor.execute(
            "UPDATE agent_profiles SET phases_config = ? WHERE id = ?",
            (json.dumps(new_phases), profile_id),
        )


def rollback_step(conn):
    """Re-add review phase to all agent profiles."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, phases_config FROM agent_profiles WHERE phases_config IS NOT NULL")
    rows = cursor.fetchall()

    for row in rows:
        profile_id, phases_json = row
        try:
            phases = json.loads(phases_json)
        except (json.JSONDecodeError, TypeError):
            continue

        # Re-add markers
        for p in phases:
            if p.get("phase") == "planning":
                p["marker"] = "[PLANNING_COMPLETE]"
            elif p.get("phase") == "developing":
                p["marker"] = "[DEVELOPING_COMPLETE]"

        # Re-add review phase
        phases.append({
            "phase": "review",
            "prompts": ["/open-pr --draft parent:{PARENT_BRANCH}"],
            "marker": "[PR_COMPLETE]",
        })

        cursor.execute(
            "UPDATE agent_profiles SET phases_config = ? WHERE id = ?",
            (json.dumps(phases), profile_id),
        )


steps = [
    step(apply_step, rollback_step),
]
