"""
Restrict gchat_events default to only pr_created notifications.

Previously all events were enabled by default, causing unwanted notifications
for PR merges, ticket failures, run completions, and review comments.
"""
from yoyo import step

__depends__ = {"0010_combine_pipeline_execute_jira_task"}

OLD_DEFAULT = '["pr_created","pr_merged","ticket_failed","run_completed","review_comments"]'
NEW_DEFAULT = '["pr_created"]'

steps = [
    step(
        f"UPDATE repositories SET gchat_events = '{NEW_DEFAULT}' WHERE gchat_events = '{OLD_DEFAULT}'",
        f"UPDATE repositories SET gchat_events = '{OLD_DEFAULT}' WHERE gchat_events = '{NEW_DEFAULT}'",
    ),
]
