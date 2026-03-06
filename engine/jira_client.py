"""Direct Jira REST API client — replaces claude_helper for orchestrator-level Jira calls."""

import sys
from typing import Optional

import httpx

from engine.state import StateManager


class JiraClient:
    """Async Jira REST API client using credentials from settings DB."""

    def __init__(self, state: StateManager):
        self.state = state
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_credentials(self) -> tuple[str, str, str]:
        """Fetch Jira credentials from settings. Raises ValueError if missing."""
        url = await self.state.get_setting("jira_url")
        email = await self.state.get_setting("jira_email")
        token = await self.state.get_setting("jira_token")
        if not all([url, email, token]):
            raise ValueError("Jira credentials not configured. Set jira_url, jira_email, jira_token in Settings.")
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

    async def fetch_epic_children(self, epic_key: str) -> list[dict]:
        """Fetch child tickets from a Jira epic via REST API."""
        jql = f'"Epic Link" = {epic_key} OR parent = {epic_key} ORDER BY rank ASC'
        try:
            resp = await self._request(
                "GET", "/rest/api/3/search",
                params={"jql": jql, "maxResults": 100, "fields": "summary,status,assignee,labels,components"},
            )
            data = resp.json()
            results = []
            for issue in data.get("issues", []):
                fields = issue.get("fields", {})
                assignee = fields.get("assignee")
                labels = fields.get("labels", [])
                components = [c.get("name", "") for c in fields.get("components", [])]
                results.append({
                    "key": issue.get("key", ""),
                    "summary": fields.get("summary", ""),
                    "status": fields.get("status", {}).get("name", "To Do"),
                    "assignee": assignee.get("displayName", "") if assignee else "",
                    "labels": labels,
                    "components": components,
                })
            return results
        except httpx.HTTPStatusError as e:
            print(f"[jira_client] Search failed ({e.response.status_code}): {e.response.text[:200]}", file=sys.stderr)
            return []
        except ValueError as e:
            print(f"[jira_client] {e}", file=sys.stderr)
            return []
        except Exception as e:
            print(f"[jira_client] Error fetching epic children: {e}", file=sys.stderr)
            return []

    async def get_issue(self, jira_key: str) -> Optional[dict]:
        """Fetch a single Jira issue."""
        try:
            resp = await self._request(
                "GET", f"/rest/api/3/issue/{jira_key}",
                params={"fields": "summary,status,assignee,labels,components"},
            )
            data = resp.json()
            fields = data.get("fields", {})
            assignee = fields.get("assignee")
            return {
                "key": data.get("key", ""),
                "summary": fields.get("summary", ""),
                "status": fields.get("status", {}).get("name", ""),
                "assignee": assignee.get("displayName", "") if assignee else "",
                "labels": fields.get("labels", []),
                "components": [c.get("name", "") for c in fields.get("components", [])],
            }
        except Exception as e:
            print(f"[jira_client] Error fetching issue {jira_key}: {e}", file=sys.stderr)
            return None

    async def get_transitions(self, jira_key: str) -> list[dict]:
        """Get available transitions for a Jira issue."""
        try:
            resp = await self._request("GET", f"/rest/api/3/issue/{jira_key}/transitions")
            data = resp.json()
            return [
                {"id": t["id"], "name": t["name"]}
                for t in data.get("transitions", [])
            ]
        except Exception as e:
            print(f"[jira_client] Error fetching transitions for {jira_key}: {e}", file=sys.stderr)
            return []

    async def transition_issue(self, jira_key: str, target_status: str) -> bool:
        """Transition a Jira issue to a target status by name."""
        try:
            transitions = await self.get_transitions(jira_key)
            match = next((t for t in transitions if t["name"].lower() == target_status.lower()), None)
            if not match:
                print(f"[jira_client] No transition '{target_status}' found for {jira_key}. "
                      f"Available: {[t['name'] for t in transitions]}", file=sys.stderr)
                return False

            await self._request(
                "POST", f"/rest/api/3/issue/{jira_key}/transitions",
                json={"transition": {"id": match["id"]}},
            )
            print(f"[jira_client] Transitioned {jira_key} -> {target_status}", file=sys.stderr)
            return True
        except Exception as e:
            print(f"[jira_client] Transition failed for {jira_key}: {e}", file=sys.stderr)
            return False

    async def is_configured(self) -> bool:
        """Check if Jira credentials are configured."""
        try:
            await self._get_credentials()
            return True
        except ValueError:
            return False
