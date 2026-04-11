"""Google Chat notifier — sends cards via webhook, per-repository config.

Direct HTTP POST to GChat webhooks. No AI context, no MCP tools.
Card format matches the /open-pr skill's GChat card structure.
"""

from __future__ import annotations

import json
import logging

import httpx

from engine.state import StateManager

logger = logging.getLogger(__name__)


class GChatNotifier:
    """Sends Google Chat card notifications via repository-configured webhooks."""

    def __init__(self, state: StateManager):
        self.state = state

    async def _get_webhook_url(self, repository_id: int | None, event: str) -> str | None:
        """Get the webhook URL for a repository if the event is enabled."""
        if not repository_id:
            return None
        repo = await self.state.get_repository(repository_id)
        if not repo or not repo.gchat_webhook_url:
            return None

        # Check if event is enabled
        try:
            events = json.loads(repo.gchat_events) if repo.gchat_events else []
        except (json.JSONDecodeError, TypeError):
            events = []

        if event not in events:
            return None

        return repo.gchat_webhook_url

    async def _send_card(self, webhook_url: str, card: dict) -> bool:
        """POST a card message to a GChat webhook."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(webhook_url, json=card)
                resp.raise_for_status()
                logger.info("GChat notification sent successfully")
                return True
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.error("Failed to send GChat notification: %s", e)
            return False

    async def notify_pr_created(
        self,
        repository_id: int | None,
        jira_key: str,
        pr_url: str,
        pr_number: int,
        pr_title: str,
        repo_name: str,
        branch_name: str,
        base_branch: str,
        additions: int = 0,
        deletions: int = 0,
        file_count: int = 0,
        reviewer_names: list[str] | None = None,
    ) -> bool:
        """Send PR created notification card matching /open-pr format."""
        webhook_url = await self._get_webhook_url(repository_id, "pr_created")
        if not webhook_url:
            return False

        reviewer_text = ""
        if reviewer_names:
            bold_names = ", ".join(f"<b>{name}</b>" for name in reviewer_names)
            reviewer_text = f"{bold_names} — Please review this PR"
        else:
            reviewer_text = "No reviewers assigned"

        jira_url = f"https://jurnal.atlassian.net/browse/{jira_key}"

        card = {
            "cardsV2": [
                {
                    "cardId": f"pr-{pr_number}",
                    "card": {
                        "header": {
                            "title": "PR Review Request",
                            "subtitle": pr_title,
                            "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/rate_review/v6/24px.svg",
                            "imageType": "CIRCLE",
                        },
                        "sections": [
                            {
                                "header": '<font color="#1a73e8">Pull Request Details</font>',
                                "widgets": [
                                    {
                                        "decoratedText": {
                                            "startIcon": {"knownIcon": "DESCRIPTION"},
                                            "topLabel": "Repository",
                                            "text": f"<b>{repo_name}</b>",
                                            "bottomLabel": f"{branch_name} -> {base_branch}",
                                        }
                                    },
                                    {
                                        "decoratedText": {
                                            "startIcon": {"materialIcon": {"name": "difference"}},
                                            "topLabel": "Changes",
                                            "text": f'<font color="#34A853">+{additions}</font> / <font color="#EA4335">-{deletions}</font> across <b>{file_count} files</b>',
                                        }
                                    },
                                    {
                                        "decoratedText": {
                                            "startIcon": {"materialIcon": {"name": "confirmation_number"}},
                                            "topLabel": "Ticket",
                                            "text": f"<b>{jira_key}</b>",
                                            "button": {
                                                "text": "Open Jira",
                                                "onClick": {
                                                    "openLink": {"url": jira_url}
                                                },
                                            },
                                        }
                                    },
                                    {"divider": {}},
                                    {
                                        "decoratedText": {
                                            "startIcon": {"knownIcon": "PERSON"},
                                            "topLabel": "Reviewers",
                                            "text": reviewer_text,
                                        }
                                    },
                                ],
                            },
                            {
                                "header": '<font color="#1a73e8">Quick Links</font>',
                                "widgets": [
                                    {
                                        "buttonList": {
                                            "buttons": [
                                                {
                                                    "text": "View Pull Request",
                                                    "icon": {"knownIcon": "BOOKMARK"},
                                                    "onClick": {
                                                        "openLink": {"url": pr_url}
                                                    },
                                                },
                                                {
                                                    "text": "View Diff",
                                                    "icon": {"knownIcon": "DESCRIPTION"},
                                                    "onClick": {
                                                        "openLink": {"url": f"{pr_url}/diff"}
                                                    },
                                                },
                                            ]
                                        }
                                    }
                                ],
                            },
                        ],
                    },
                }
            ]
        }

        return await self._send_card(webhook_url, card)

    async def notify_pr_merged(
        self,
        repository_id: int | None,
        jira_key: str,
        pr_number: int,
        pr_url: str,
    ) -> bool:
        """Send PR merged notification."""
        webhook_url = await self._get_webhook_url(repository_id, "pr_merged")
        if not webhook_url:
            return False

        card = {
            "cardsV2": [
                {
                    "cardId": f"pr-merged-{pr_number}",
                    "card": {
                        "header": {
                            "title": "PR Merged",
                            "subtitle": f"{jira_key} — PR #{pr_number}",
                            "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/check_circle/v6/24px.svg",
                            "imageType": "CIRCLE",
                        },
                        "sections": [
                            {
                                "widgets": [
                                    {
                                        "buttonList": {
                                            "buttons": [
                                                {
                                                    "text": "View PR",
                                                    "onClick": {
                                                        "openLink": {"url": pr_url}
                                                    },
                                                }
                                            ]
                                        }
                                    }
                                ]
                            }
                        ],
                    },
                }
            ]
        }
        return await self._send_card(webhook_url, card)

    async def notify_ticket_failed(
        self,
        repository_id: int | None,
        jira_key: str,
        ticket_id: str,
        error: str = "",
    ) -> bool:
        """Send ticket failure notification."""
        webhook_url = await self._get_webhook_url(repository_id, "ticket_failed")
        if not webhook_url:
            return False

        short_error = (error[:200] + "...") if len(error) > 200 else error

        card = {
            "cardsV2": [
                {
                    "cardId": f"failed-{ticket_id}",
                    "card": {
                        "header": {
                            "title": "Ticket Failed",
                            "subtitle": jira_key,
                            "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/error/v6/24px.svg",
                            "imageType": "CIRCLE",
                        },
                        "sections": [
                            {
                                "widgets": [
                                    {
                                        "decoratedText": {
                                            "startIcon": {"knownIcon": "DESCRIPTION"},
                                            "topLabel": "Error",
                                            "text": short_error or "Unknown error",
                                        }
                                    }
                                ]
                            }
                        ],
                    },
                }
            ]
        }
        return await self._send_card(webhook_url, card)

    async def notify_run_completed(
        self,
        repository_id: int | None,
        run_name: str,
        done_count: int = 0,
        failed_count: int = 0,
    ) -> bool:
        """Send run completed notification."""
        webhook_url = await self._get_webhook_url(repository_id, "run_completed")
        if not webhook_url:
            return False

        card = {
            "cardsV2": [
                {
                    "cardId": f"run-{run_name}",
                    "card": {
                        "header": {
                            "title": "Run Completed",
                            "subtitle": run_name,
                            "imageUrl": "https://fonts.gstatic.com/s/i/googlematerialicons/flag/v6/24px.svg",
                            "imageType": "CIRCLE",
                        },
                        "sections": [
                            {
                                "widgets": [
                                    {
                                        "decoratedText": {
                                            "topLabel": "Results",
                                            "text": f'<font color="#34A853"><b>{done_count}</b> done</font>, <font color="#EA4335"><b>{failed_count}</b> failed</font>',
                                        }
                                    }
                                ]
                            }
                        ],
                    },
                }
            ]
        }
        return await self._send_card(webhook_url, card)
