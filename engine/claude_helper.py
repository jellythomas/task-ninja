"""Claude CLI helper — runs quick Claude commands for MCP tool access."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

logger = logging.getLogger(__name__)


class ClaudeHelper:
    """Runs Claude CLI commands to leverage MCP tools (Jira, Bitbucket)."""

    def __init__(self, command: str = "claude"):
        self.command = command

    async def run_prompt(self, prompt: str, cwd: str | None = None, timeout: int = 120) -> str:
        """Run a Claude CLI prompt and return the output."""
        cmd = [self.command, "--print", "--dangerously-skip-permissions"]
        cmd.append(prompt)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd or os.getcwd(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            output = stdout.decode("utf-8", errors="replace").strip()
            if process.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                logger.error("Command failed (rc=%d): %s", process.returncode, err)
            return output
        except asyncio.TimeoutError:
            logger.warning("Command timed out after %ds", timeout)
            if process.returncode is None:
                process.kill()
            return ""
        except OSError as e:
            logger.error("Error running claude command: %s", e)
            return ""

    async def fetch_epic_children(self, epic_key: str) -> list[dict]:
        """Fetch child tickets from a Jira epic using MCP tools."""
        prompt = (
            f"Use the jira_search tool to find all issues linked to epic {epic_key}. "
            f'Search with JQL: "Epic Link" = {epic_key} OR parent = {epic_key} ORDER BY rank ASC. '
            f'Return ONLY a JSON array of objects with keys: "key", "summary", "status", "assignee". '
            f"No explanation, just the JSON array."
        )
        output = await self.run_prompt(prompt, timeout=60)
        return self._parse_json_array(output)

    async def transition_jira_issue(self, jira_key: str, target_status: str) -> bool:
        """Transition a Jira issue to a target status."""
        prompt = (
            f'Transition Jira issue {jira_key} to "{target_status}". '
            f'First use jira_get_transitions to find the transition ID for "{target_status}", '
            f"then use jira_transition_issue to apply it. "
            f'Reply with just "OK" if successful or "FAILED: reason" if not.'
        )
        output = await self.run_prompt(prompt, timeout=30)
        success = "ok" in output.lower() and "failed" not in output.lower()
        if not success:
            logger.warning("Jira transition failed for %s: %s", jira_key, output[:200])
        return success

    def _parse_json_array(self, text: str) -> list[dict]:
        """Extract a JSON array from Claude's output."""
        # Try to find JSON array in the output
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.warning("Could not parse JSON array from output: %s", text[:200])
        return []
