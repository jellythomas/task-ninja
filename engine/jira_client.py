"""Direct Jira REST API client — replaces claude_helper for orchestrator-level Jira calls."""

from __future__ import annotations

import logging
import re

import httpx

from engine.env_manager import get_env

logger = logging.getLogger(__name__)


def _extract_text_from_adf(node: dict | list) -> str:
    """Recursively extract plain text from Jira ADF (Atlassian Document Format)."""
    if isinstance(node, list):
        return " ".join(_extract_text_from_adf(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        content = node.get("content", [])
        return " ".join(_extract_text_from_adf(c) for c in content)
    return str(node) if node else ""


class JiraClient:
    """Async Jira REST API client using credentials from .env."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_credentials(self) -> tuple[str, str, str]:
        """Fetch Jira credentials from .env. Raises ValueError if missing."""
        url = get_env("JIRA_BASE_URL")
        email = get_env("JIRA_EMAIL")
        token = get_env("JIRA_API_TOKEN")
        if not all([url, email, token]):
            raise ValueError("Jira credentials not configured. Set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN in .env")
        return url.rstrip("/"), email, token

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make an authenticated Jira API request."""
        url, email, token = await self._get_credentials()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method,
                f"{url}{path}",
                auth=(email, token),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                **kwargs,
            )
            resp.raise_for_status()
            return resp

    @staticmethod
    def _parse_blocked_by(issuelinks: list[dict]) -> list[str]:
        """Extract blocker keys from Jira issuelinks.

        Returns a list of Jira keys that block this ticket (inward 'is blocked by').
        """
        blocked_by = []
        for link in issuelinks:
            link_type = link.get("type", {})
            # inward relationship with type "Blocks" means this ticket is blocked by inwardIssue
            if link_type.get("name") == "Blocks" and "inwardIssue" in link:
                key = link["inwardIssue"].get("key", "")
                if key:
                    blocked_by.append(key)
        return blocked_by

    @staticmethod
    def _extract_file_paths(description: str | dict | None) -> list[str]:
        """Extract file/directory paths mentioned in ticket description."""
        if not description:
            return []
        # Handle Jira's Atlassian Document Format (ADF) - description may be a dict
        if isinstance(description, dict):
            description = _extract_text_from_adf(description)

        # Match common source paths
        pattern = r'(?:src|app|lib|api|engine|models|services|controllers|workers|spec|test|tests|config|db|migrations)/[\w./\-]+'
        matches = re.findall(pattern, description)
        # Deduplicate and normalize (preserve order, remove dupes)
        paths = list(dict.fromkeys(matches))
        return paths[:20]  # Cap at 20 to avoid noise

    async def fetch_epic_children(self, epic_key: str) -> list[dict]:
        """Fetch child tickets from a Jira epic via REST API."""
        jql = f'"Epic Link" = {epic_key} OR parent = {epic_key} ORDER BY rank ASC'
        try:
            resp = await self._request(
                "GET",
                "/rest/api/3/search/jql",
                params={"jql": jql, "maxResults": 100, "fields": "summary,status,assignee,labels,components,issuelinks,description"},
            )
            data = resp.json()
            results = []
            for issue in data.get("issues", []):
                fields = issue.get("fields", {})
                assignee = fields.get("assignee")
                labels = fields.get("labels", [])
                components = [c.get("name", "") for c in fields.get("components", [])]
                issuelinks = fields.get("issuelinks", [])
                description = fields.get("description")
                results.append(
                    {
                        "key": issue.get("key", ""),
                        "summary": fields.get("summary", ""),
                        "status": fields.get("status", {}).get("name", "To Do"),
                        "assignee": assignee.get("displayName", "") if assignee else "",
                        "labels": labels,
                        "components": components,
                        "blocked_by": self._parse_blocked_by(issuelinks),
                        "predicted_files": self._extract_file_paths(description),
                    }
                )
            return results
        except httpx.HTTPStatusError as e:
            logger.error("Search failed (%d): %s", e.response.status_code, e.response.text[:200])
            return []
        except ValueError as e:
            logger.error("%s", e)
            return []
        except httpx.RequestError as e:
            logger.error("Error fetching epic children: %s", e)
            return []

    async def get_issue(self, jira_key: str) -> dict | None:
        """Fetch a single Jira issue."""
        try:
            resp = await self._request(
                "GET",
                f"/rest/api/3/issue/{jira_key}",
                params={"fields": "summary,status,assignee,labels,components,issuelinks,description"},
            )
            data = resp.json()
            fields = data.get("fields", {})
            assignee = fields.get("assignee")
            issuelinks = fields.get("issuelinks", [])
            description = fields.get("description")
            return {
                "key": data.get("key", ""),
                "summary": fields.get("summary", ""),
                "status": fields.get("status", {}).get("name", ""),
                "assignee": assignee.get("displayName", "") if assignee else "",
                "labels": fields.get("labels", []),
                "components": [c.get("name", "") for c in fields.get("components", [])],
                "blocked_by": self._parse_blocked_by(issuelinks),
                "predicted_files": self._extract_file_paths(description),
            }
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            logger.error("Error fetching issue %s: %s", jira_key, e)
            return None

    async def get_transitions(self, jira_key: str) -> list[dict]:
        """Get available transitions for a Jira issue."""
        try:
            resp = await self._request("GET", f"/rest/api/3/issue/{jira_key}/transitions")
            data = resp.json()
            return [{"id": t["id"], "name": t["name"]} for t in data.get("transitions", [])]
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            logger.error("Error fetching transitions for %s: %s", jira_key, e)
            return []

    async def transition_issue(self, jira_key: str, target_status: str) -> bool:
        """Transition a Jira issue to a target status by name."""
        try:
            transitions = await self.get_transitions(jira_key)
            match = next((t for t in transitions if t["name"].lower() == target_status.lower()), None)
            if not match:
                logger.warning(
                    "No transition '%s' found for %s. Available: %s",
                    target_status,
                    jira_key,
                    [t["name"] for t in transitions],
                )
                return False

            await self._request(
                "POST",
                f"/rest/api/3/issue/{jira_key}/transitions",
                json={"transition": {"id": match["id"]}},
            )
            logger.info("Transitioned %s -> %s", jira_key, target_status)
            return True
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            logger.error("Transition failed for %s: %s", jira_key, e)
            return False

    async def is_configured(self) -> bool:
        """Check if Jira credentials are configured."""
        try:
            await self._get_credentials()
            return True
        except ValueError:
            return False
