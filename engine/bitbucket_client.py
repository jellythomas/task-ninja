"""Bitbucket REST API client for PR status tracking."""

from __future__ import annotations

import logging

import httpx

from engine.env_manager import get_env

logger = logging.getLogger(__name__)


class BitbucketClient:
    """Async Bitbucket REST API client."""

    async def _get_credentials(self) -> tuple[str, str, str]:
        workspace = get_env("BITBUCKET_WORKSPACE")
        username = get_env("BITBUCKET_USERNAME")
        app_password = get_env("BITBUCKET_APP_PASSWORD")
        if not all([workspace, username, app_password]):
            raise ValueError("Bitbucket credentials not configured")
        return workspace, username, app_password

    async def is_configured(self) -> bool:
        try:
            await self._get_credentials()
            return True
        except ValueError:
            return False

    async def get_pr_status(self, repo_slug: str, pr_number: int) -> dict | None:
        """Fetch PR state from Bitbucket. Returns {state, approvals, comment_count} or None on error."""
        try:
            workspace, username, app_password = await self._get_credentials()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pr_number}",
                    auth=(username, app_password),
                )
                resp.raise_for_status()
                data = resp.json()
                state = data.get("state", "OPEN").lower()  # OPEN, MERGED, DECLINED, SUPERSEDED
                participants = data.get("participants", [])
                approved_count = sum(1 for p in participants if p.get("approved"))
                comment_count = data.get("comment_count", 0)
                return {
                    "state": state,
                    "approvals": approved_count,
                    "comment_count": comment_count,
                }
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            logger.error("Error fetching PR status: %s", e)
            return None

    async def get_pr_comments_since(self, repo_slug: str, pr_number: int, since: str | None = None) -> int:
        """Get count of comments on a PR, optionally since a timestamp."""
        try:
            workspace, username, app_password = await self._get_credentials()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pr_number}/comments",
                    auth=(username, app_password),
                    params={"pagelen": 1},  # We just need the total
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("size", 0)
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            logger.error("Error fetching PR comments: %s", e)
            return 0
