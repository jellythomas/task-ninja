"""Claude CLI helper — runs quick Claude commands for MCP tool access."""

import asyncio
import json
import os
import re
import sys
from typing import Optional


class ClaudeHelper:
    """Runs Claude CLI commands to leverage MCP tools (Jira, Bitbucket)."""

    def __init__(self, command: str = "claude", skip_permissions: bool = True):
        self.command = command
        self.skip_permissions = skip_permissions

    async def run_prompt(self, prompt: str, cwd: str = None, timeout: int = 120) -> str:
        """Run a Claude CLI prompt and return the output."""
        cmd = [self.command, "--print"]
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        cmd.append(prompt)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd or os.getcwd(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
            output = stdout.decode("utf-8", errors="replace").strip()
            if process.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                print(f"[claude_helper] Command failed (rc={process.returncode}): {err}", file=sys.stderr)
            return output
        except asyncio.TimeoutError:
            print(f"[claude_helper] Command timed out after {timeout}s", file=sys.stderr)
            if process.returncode is None:
                process.kill()
            return ""
        except Exception as e:
            print(f"[claude_helper] Error: {e}", file=sys.stderr)
            return ""

    async def fetch_epic_children(self, epic_key: str) -> list[dict]:
        """Fetch child tickets from a Jira epic using MCP tools."""
        prompt = (
            f'Use the jira_search tool to find all issues linked to epic {epic_key}. '
            f'Search with JQL: "Epic Link" = {epic_key} OR parent = {epic_key} ORDER BY rank ASC. '
            f'Return ONLY a JSON array of objects with keys: "key", "summary", "status", "assignee". '
            f'No explanation, just the JSON array.'
        )
        output = await self.run_prompt(prompt, timeout=60)
        return self._parse_json_array(output)

    async def transition_jira_issue(self, jira_key: str, target_status: str) -> bool:
        """Transition a Jira issue to a target status."""
        prompt = (
            f'Transition Jira issue {jira_key} to "{target_status}". '
            f'First use jira_get_transitions to find the transition ID for "{target_status}", '
            f'then use jira_transition_issue to apply it. '
            f'Reply with just "OK" if successful or "FAILED: reason" if not.'
        )
        output = await self.run_prompt(prompt, timeout=30)
        success = "ok" in output.lower() and "failed" not in output.lower()
        if not success:
            print(f"[claude_helper] Jira transition failed for {jira_key}: {output[:200]}", file=sys.stderr)
        return success

    async def create_draft_pr(
        self, jira_key: str, branch_name: str, cwd: str, base_branch: str = "master"
    ) -> Optional[dict]:
        """Create a draft PR via Bitbucket MCP tools."""
        prompt = (
            f'Create a draft pull request on Bitbucket for branch "{branch_name}" targeting "{base_branch}". '
            f'Title: "{jira_key}: Implementation". '
            f'Use the bitbucket_create_pull_request tool. '
            f'Return ONLY a JSON object with keys: "id", "url", "title". No explanation.'
        )
        output = await self.run_prompt(prompt, cwd=cwd, timeout=60)
        result = self._parse_json_object(output)
        return result

    async def get_issue_summary(self, jira_key: str) -> Optional[str]:
        """Fetch just the summary/title of a Jira issue."""
        prompt = (
            f'Use jira_get_issue to fetch {jira_key}. '
            f'Return ONLY the issue summary text, nothing else.'
        )
        output = await self.run_prompt(prompt, timeout=30)
        # Strip any markdown or extra formatting
        summary = output.strip().strip('"').strip("'")
        return summary if summary and len(summary) < 500 else None

    def _parse_json_array(self, text: str) -> list[dict]:
        """Extract a JSON array from Claude's output."""
        # Try to find JSON array in the output
        match = re.search(r'\[[\s\S]*\]', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        print(f"[claude_helper] Could not parse JSON array from output: {text[:200]}", file=sys.stderr)
        return []

    def _parse_json_object(self, text: str) -> Optional[dict]:
        """Extract a JSON object from Claude's output."""
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        print(f"[claude_helper] Could not parse JSON object from output: {text[:200]}", file=sys.stderr)
        return None
