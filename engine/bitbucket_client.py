"""Bitbucket REST API client for PR status tracking and creation."""

from __future__ import annotations

import json
import logging

import httpx

from engine.env_manager import get_env

logger = logging.getLogger(__name__)

_API_BASE = "https://api.bitbucket.org/2.0"


class BitbucketClient:
    """Async Bitbucket REST API client."""

    async def _get_credentials(self) -> tuple[str, str, str]:
        workspace = get_env("BITBUCKET_WORKSPACE")
        username = get_env("BITBUCKET_USERNAME")
        # Support both BITBUCKET_API_TOKEN (mcp-atlassian style) and BITBUCKET_APP_PASSWORD
        app_password = get_env("BITBUCKET_API_TOKEN") or get_env("BITBUCKET_APP_PASSWORD")
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

    async def get_default_reviewers(self, repo_slug: str) -> list[dict]:
        """Fetch default reviewers for a repository.

        Returns list of dicts with 'uuid', 'display_name', 'nickname'.
        """
        try:
            workspace, username, app_password = await self._get_credentials()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{_API_BASE}/repositories/{workspace}/{repo_slug}/default-reviewers",
                    auth=(username, app_password),
                )
                resp.raise_for_status()
                data = resp.json()
                return [
                    {
                        "uuid": r.get("uuid", ""),
                        "display_name": r.get("display_name", ""),
                        "nickname": r.get("nickname", ""),
                    }
                    for r in data.get("values", [])
                ]
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            logger.error("Error fetching default reviewers: %s", e)
            return []

    async def resolve_reviewer_uuids(self, repo_slug: str, emails: list[str]) -> list[dict]:
        """Resolve email addresses to Bitbucket UUIDs via workspace members search.

        Falls back to default reviewers if email lookup fails.
        Returns list of dicts with 'uuid'.
        """
        try:
            workspace, username, app_password = await self._get_credentials()
            uuids = []
            async with httpx.AsyncClient(timeout=30) as client:
                for email in emails:
                    # Search workspace members by email
                    resp = await client.get(
                        f"{_API_BASE}/workspaces/{workspace}/members",
                        auth=(username, app_password),
                        params={"q": f'user.nickname="{email.split("@")[0]}"'},
                    )
                    if resp.status_code == 200:
                        members = resp.json().get("values", [])
                        if members:
                            user = members[0].get("user", {})
                            uuids.append({"uuid": user.get("uuid", "")})
                            continue
                    # Fallback: try as UUID directly (already resolved)
                    if email.startswith("{"):
                        uuids.append({"uuid": email})
            return uuids
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            logger.error("Error resolving reviewer UUIDs: %s", e)
            return []

    async def create_pr(
        self,
        repo_slug: str,
        title: str,
        source_branch: str,
        destination_branch: str,
        description: str = "",
        reviewers: list[dict] | None = None,
        close_source_branch: bool = True,
    ) -> dict | None:
        """Create a pull request on Bitbucket.

        Args:
            repo_slug: Repository slug
            title: PR title
            source_branch: Source branch name
            destination_branch: Destination/base branch name
            description: PR description (Markdown)
            reviewers: List of dicts with 'uuid' key
            close_source_branch: Whether to close source branch on merge

        Returns:
            Dict with 'id', 'url', 'title' or None on error.
        """
        try:
            workspace, username, app_password = await self._get_credentials()
            payload = {
                "title": title,
                "source": {"branch": {"name": source_branch}},
                "destination": {"branch": {"name": destination_branch}},
                "description": description,
                "close_source_branch": close_source_branch,
            }
            if reviewers:
                payload["reviewers"] = reviewers

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests",
                    auth=(username, app_password),
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                pr_id = data.get("id")
                pr_url = data.get("links", {}).get("html", {}).get("href", "")
                logger.info("Created PR #%s: %s", pr_id, pr_url)
                return {
                    "id": pr_id,
                    "url": pr_url,
                    "title": data.get("title", ""),
                }
        except httpx.HTTPStatusError as e:
            # Handle "PR already exists" gracefully
            if e.response.status_code == 409:
                logger.warning("PR already exists for branch %s", source_branch)
                return await self._find_existing_pr(repo_slug, source_branch)
            logger.error("Error creating PR: %s — %s", e, e.response.text)
            return None
        except (httpx.RequestError, ValueError) as e:
            logger.error("Error creating PR: %s", e)
            return None

    async def _find_existing_pr(self, repo_slug: str, source_branch: str) -> dict | None:
        """Find an existing open PR for the given source branch."""
        try:
            workspace, username, app_password = await self._get_credentials()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests",
                    auth=(username, app_password),
                    params={
                        "q": f'source.branch.name="{source_branch}" AND state="OPEN"',
                    },
                )
                resp.raise_for_status()
                prs = resp.json().get("values", [])
                if prs:
                    pr = prs[0]
                    return {
                        "id": pr.get("id"),
                        "url": pr.get("links", {}).get("html", {}).get("href", ""),
                        "title": pr.get("title", ""),
                    }
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            logger.error("Error finding existing PR: %s", e)
        return None

    async def get_pr_comments(
        self, repo_slug: str, pr_number: int, bot_filter: list[str] | None = None
    ) -> list[dict]:
        """Fetch full PR comments with inline context, filtering out bot users.

        Returns list of dicts with keys:
            - author: display name
            - content: comment text
            - file: file path (for inline comments) or None
            - line: line number (for inline comments) or None
            - created_on: ISO timestamp
        """
        if bot_filter is None:
            bot_filter = ["jenkins", "ci-bot", "bitbucket-pipelines"]

        try:
            workspace, username, app_password = await self._get_credentials()
            comments = []
            url = f"{_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_number}/comments"
            async with httpx.AsyncClient(timeout=30) as client:
                while url:
                    resp = await client.get(
                        url,
                        auth=(username, app_password),
                        params={"pagelen": 50},
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    for c in data.get("values", []):
                        author_name = c.get("user", {}).get("display_name", "")
                        author_nick = c.get("user", {}).get("nickname", "").lower()

                        # Filter out bots
                        if any(bot.lower() in author_nick for bot in bot_filter):
                            continue

                        # Skip deleted/system comments
                        if c.get("deleted"):
                            continue

                        inline = c.get("inline", {})
                        comments.append({
                            "author": author_name,
                            "content": c.get("content", {}).get("raw", ""),
                            "file": inline.get("path"),
                            "line": inline.get("to") or inline.get("from"),
                            "created_on": c.get("created_on", ""),
                        })

                    url = data.get("next")

            return comments
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            logger.error("Error fetching PR comments: %s", e)
            return []
