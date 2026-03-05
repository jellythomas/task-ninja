"""Git worktree management for parallel ticket execution."""

import asyncio
import os
from pathlib import Path


class GitManager:
    """Creates and cleans up git worktrees for isolated ticket work."""

    def __init__(self, project_path: str, worktree_dir: str = ".worktrees", branch_prefix: str = "feat"):
        self.project_path = Path(project_path)
        self.worktree_base = self.project_path / worktree_dir
        self.branch_prefix = branch_prefix

    async def create_worktree(self, jira_key: str) -> str:
        """Create a git worktree for a ticket. Returns the worktree path."""
        branch_name = f"{self.branch_prefix}/{jira_key}"
        worktree_path = self.worktree_base / f"worktree-{jira_key.lower()}"

        self.worktree_base.mkdir(parents=True, exist_ok=True)

        # Check if branch exists
        proc = await asyncio.create_subprocess_exec(
            "git", "branch", "--list", branch_name,
            cwd=str(self.project_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if stdout.strip():
            # Branch exists — create worktree from it
            await self._run_git("worktree", "add", str(worktree_path), branch_name)
        else:
            # Create new branch from current HEAD
            await self._run_git("worktree", "add", "-b", branch_name, str(worktree_path))

        return str(worktree_path)

    async def cleanup_worktree(self, worktree_path: str) -> None:
        """Remove a worktree (keeps the branch)."""
        if Path(worktree_path).exists():
            await self._run_git("worktree", "remove", worktree_path, "--force")
        # Prune stale worktree references
        await self._run_git("worktree", "prune")

    async def get_branch_name(self, jira_key: str) -> str:
        return f"{self.branch_prefix}/{jira_key}"

    async def _run_git(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(self.project_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode().strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {error}")
        return stdout.decode().strip()
