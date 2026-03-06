"""Git worktree management for parallel ticket execution."""

import asyncio
import os
from pathlib import Path


class GitManager:
    """Creates and cleans up git worktrees for isolated ticket work."""

    def __init__(self, project_path: str, worktree_dir: str = ".worktrees", branch_prefix: str = "feat"):
        self.project_path = Path(project_path).expanduser().resolve()
        self.worktree_base = self.project_path / worktree_dir
        self.branch_prefix = branch_prefix

    async def create_worktree(self, jira_key: str, parent_branch: str = None) -> str:
        """Create a git worktree for a ticket. Returns the worktree path.

        Args:
            jira_key: The Jira ticket key (e.g., MC-9174)
            parent_branch: Branch to create the worktree from (default: current HEAD)
        """
        branch_name = f"{self.branch_prefix}/{jira_key}"
        worktree_path = self.worktree_base / f"worktree-{jira_key.lower()}"

        self.worktree_base.mkdir(parents=True, exist_ok=True)

        # Clean up existing worktree if it exists (from a previous failed run)
        if worktree_path.exists():
            try:
                await self._run_git("worktree", "remove", str(worktree_path), "--force")
            except RuntimeError:
                # If git worktree remove fails, force-remove the directory
                import shutil
                shutil.rmtree(str(worktree_path), ignore_errors=True)
            await self._run_git("worktree", "prune")

        # Always fetch latest from origin before branching
        fetched_remote = False
        if parent_branch:
            try:
                await self._run_git("fetch", "origin", parent_branch)
                fetched_remote = True
            except RuntimeError:
                pass  # May not have remote, that's fine

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
            # Sync existing branch with latest origin
            if parent_branch and fetched_remote:
                try:
                    await self._run_git("-C", str(worktree_path), "merge", f"origin/{parent_branch}", "--ff-only")
                except RuntimeError:
                    pass  # May have diverged, skip auto-merge
        else:
            # Create new branch from origin/<parent_branch> (latest remote) or local fallback
            if parent_branch and fetched_remote:
                start_point = f"origin/{parent_branch}"
            else:
                start_point = parent_branch or "HEAD"
            await self._run_git("worktree", "add", "-b", branch_name, str(worktree_path), start_point)

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
