"""
Combine planning+developing pipeline into a single /execute-jira-task command.

Planning phase now runs /execute-jira-task which handles everything (planning,
implementation, quality gates, commit). Developing phase is left with empty
prompts — the Worker auto-completes it for proper state transitions.
"""
import json
from yoyo import step

__depends__ = {"0009_remove_review_phase_from_profiles"}


def apply_step(conn):
    """Replace split pipeline with combined /execute-jira-task."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, phases_config FROM agent_profiles WHERE phases_config IS NOT NULL")
    rows = cursor.fetchall()

    for row in rows:
        profile_id, phases_json = row
        try:
            phases = json.loads(phases_json)
        except (json.JSONDecodeError, TypeError):
            continue

        # Collect any pre-prompts from the planning phase (e.g. /caveman ultra)
        planning_phase = next((p for p in phases if p.get("phase") == "planning"), None)
        pre_prompts = []
        if planning_phase:
            for prompt in planning_phase.get("prompts", []):
                if (not prompt.startswith("/planning-task")
                        and not prompt.startswith("/developing-task")
                        and not prompt.startswith("/execute-jira-task")):
                    pre_prompts.append(prompt)

        new_phases = [
            {
                "phase": "planning",
                "prompts": pre_prompts + ["/execute-jira-task {JIRA_KEY} parent:{PARENT_BRANCH}"],
            },
            {
                "phase": "developing",
                "prompts": [],  # Auto-completed by Worker
            },
        ]

        cursor.execute(
            "UPDATE agent_profiles SET phases_config = ? WHERE id = ?",
            (json.dumps(new_phases), profile_id),
        )


def rollback_step(conn):
    """Restore split planning+developing pipeline."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, phases_config FROM agent_profiles WHERE phases_config IS NOT NULL")
    rows = cursor.fetchall()

    for row in rows:
        profile_id, phases_json = row
        try:
            phases = json.loads(phases_json)
        except (json.JSONDecodeError, TypeError):
            continue

        # Collect any pre-prompts (e.g. /caveman ultra)
        planning_phase = next((p for p in phases if p.get("phase") == "planning"), None)
        pre_prompts = []
        if planning_phase:
            for prompt in planning_phase.get("prompts", []):
                if not prompt.startswith("/execute-jira-task"):
                    pre_prompts.append(prompt)

        new_phases = [
            {
                "phase": "planning",
                "prompts": pre_prompts + ["/planning-task {JIRA_KEY} parent:{PARENT_BRANCH}"],
            },
            {
                "phase": "developing",
                "prompts": pre_prompts + ["/developing-task {JIRA_KEY} parent:{PARENT_BRANCH}"],
            },
        ]

        cursor.execute(
            "UPDATE agent_profiles SET phases_config = ? WHERE id = ?",
            (json.dumps(new_phases), profile_id),
        )


steps = [
    step(apply_step, rollback_step),
]
