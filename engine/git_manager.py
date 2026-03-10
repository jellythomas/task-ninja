"""Git worktree management for parallel ticket execution."""

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# Hidden files/dirs to copy from main repo to new worktrees
DEFAULT_HIDDEN_COPIES = [
    ".env", ".claude", ".tool-versions", ".ruby-version",
    ".node-version", ".nvmrc", ".python-version",
]


@dataclass
class WorktreeResult:
    """Result of a worktree creation attempt."""
    path: str
    created: bool  # True if newly created, False if reused
    branch_existed: bool
    current_parent: Optional[str] = None  # Detected merge-base branch
    expected_parent: Optional[str] = None  # Requested parent_branch
    mismatch: bool = False  # True if current_parent != expected_parent

    def to_dict(self) -> dict:
        return asdict(self)


class GitManager:
    """Creates and cleans up git worktrees for isolated ticket work."""

    def __init__(self, project_path: str, worktree_dir: str = ".worktrees", branch_prefix: str = "feat",
                 hidden_copies: list[str] = None):
        self.project_path = Path(project_path).expanduser().resolve()
        self.worktree_base = self.project_path / worktree_dir
        self.hidden_copies = hidden_copies or DEFAULT_HIDDEN_COPIES
        self.branch_prefix = branch_prefix

    async def create_worktree(self, jira_key: str, parent_branch: str = None, clean: bool = False) -> WorktreeResult:
        """Create a git worktree for a ticket. Returns a WorktreeResult.

        Args:
            jira_key: The Jira ticket key (e.g., MC-9174)
            parent_branch: Branch to create the worktree from (default: current HEAD)
            clean: If True, destroy existing worktree and start fresh.
                   If False (default), reuse existing worktree when present.
        """
        branch_name = f"{self.branch_prefix}/{jira_key}"
        worktree_path = self.worktree_base / f"worktree-{jira_key.lower()}"

        self.worktree_base.mkdir(parents=True, exist_ok=True)

        # Always prune stale worktree refs first
        try:
            await self._run_git("worktree", "prune")
        except RuntimeError:
            pass

        # Handle existing worktree
        if worktree_path.exists():
            if not clean:
                # Reuse existing worktree — just return it
                return WorktreeResult(
                    path=str(worktree_path),
                    created=False,
                    branch_existed=True,
                )

            # Clean mode: destroy and recreate
            await self._remove_worktree(worktree_path)

        # Always fetch latest from origin before branching
        fetched_remote = False
        if parent_branch:
            try:
                await self._run_git("fetch", "origin", parent_branch)
                fetched_remote = True
            except RuntimeError:
                pass  # May not have remote, that's fine

        # Check if branch exists
        branch_existed = await self._branch_exists(branch_name)

        if branch_existed:
            # Branch exists — create worktree from it
            await self._run_git("worktree", "add", str(worktree_path), branch_name)
            # Copy hidden files to worktree
            self._copy_hidden_files(worktree_path)
            # Sync existing branch with latest origin
            if parent_branch and fetched_remote:
                try:
                    await self._run_git("-C", str(worktree_path), "merge", f"origin/{parent_branch}", "--ff-only")
                except RuntimeError:
                    pass  # May have diverged, skip auto-merge

            # Check if branch parent matches expected parent
            mismatch = False
            current_parent = None
            if parent_branch and fetched_remote:
                mismatch, current_parent = await self._check_parent_mismatch(
                    branch_name, parent_branch
                )

            return WorktreeResult(
                path=str(worktree_path),
                created=False,
                branch_existed=True,
                current_parent=current_parent,
                expected_parent=parent_branch,
                mismatch=mismatch,
            )
        else:
            # Create new branch from origin/<parent_branch> (latest remote) or local fallback
            if parent_branch and fetched_remote:
                start_point = f"origin/{parent_branch}"
            else:
                start_point = parent_branch or "HEAD"
            await self._run_git("worktree", "add", "-b", branch_name, str(worktree_path), start_point)
            # Copy hidden files to worktree
            self._copy_hidden_files(worktree_path)

            return WorktreeResult(
                path=str(worktree_path),
                created=True,
                branch_existed=False,
                expected_parent=parent_branch,
                mismatch=False,
            )

    def _copy_hidden_files(self, worktree_path: Path):
        """Copy hidden files/dirs from main repo to worktree."""
        for name in self.hidden_copies:
            src = self.project_path / name
            dst = worktree_path / name
            if not src.exists() or dst.exists():
                continue
            try:
                if src.is_dir():
                    shutil.copytree(str(src), str(dst), symlinks=True)
                else:
                    shutil.copy2(str(src), str(dst))
            except Exception:
                pass  # Best-effort, don't fail worktree creation
        # Write permissive settings.local.json so --dangerously-skip-permissions
        # works correctly for subagents in interactive PTY sessions.
        self._write_permissive_settings(worktree_path)

    def _write_permissive_settings(self, worktree_path: Path):
        """Write a permissive .claude/settings.local.json to bypass all permission prompts."""
        claude_dir = worktree_path / ".claude"
        settings_path = claude_dir / "settings.local.json"
        if settings_path.exists():
            return  # Don't overwrite existing settings
        try:
            claude_dir.mkdir(parents=True, exist_ok=True)
            settings = {
                "permissions": {
                    "allow": [
                        "Bash(*)",
                        "Edit(*)",
                        "Write(*)",
                        "Read(*)",
                        "Glob(*)",
                        "Grep(*)",
                        "Agent(*)",
                        "mcp__mcp-atlassian__*",
                        "mcp__sequentialthinking__*",
                    ]
                }
            }
            settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        except Exception:
            pass  # Best-effort

    async def rebase_onto(self, jira_key: str, new_parent: str) -> str:
        """Rebase a branch onto a new parent branch. Returns worktree path."""
        branch_name = f"{self.branch_prefix}/{jira_key}"
        worktree_path = self.worktree_base / f"worktree-{jira_key.lower()}"

        # Fetch latest
        try:
            await self._run_git("fetch", "origin", new_parent)
        except RuntimeError:
            pass

        # Find the merge-base between branch and its current parent
        # Then rebase only the branch's own commits onto the new parent
        try:
            merge_base = await self._run_git(
                "-C", str(worktree_path),
                "merge-base", branch_name, "HEAD"
            )
        except RuntimeError:
            merge_base = None

        if merge_base:
            await self._run_git(
                "-C", str(worktree_path),
                "rebase", "--onto", f"origin/{new_parent}", merge_base, branch_name
            )
        else:
            await self._run_git(
                "-C", str(worktree_path),
                "rebase", f"origin/{new_parent}"
            )

        return str(worktree_path)

    async def fresh_start(self, jira_key: str, parent_branch: str) -> WorktreeResult:
        """Delete existing branch and worktree, create fresh from parent_branch."""
        branch_name = f"{self.branch_prefix}/{jira_key}"
        worktree_path = self.worktree_base / f"worktree-{jira_key.lower()}"

        # Remove worktree
        await self._remove_worktree(worktree_path)

        # Delete the branch
        try:
            await self._run_git("branch", "-D", branch_name)
        except RuntimeError:
            pass  # Branch may not exist

        # Create fresh via create_worktree
        return await self.create_worktree(jira_key, parent_branch, clean=False)

    async def _check_parent_mismatch(self, branch_name: str, expected_parent: str) -> tuple[bool, Optional[str]]:
        """Check if a branch's actual parent matches the expected parent.

        Returns (mismatch: bool, detected_parent_desc: str or None).
        """
        try:
            # Get merge-base between branch and expected parent
            merge_base = await self._run_git(
                "merge-base", branch_name, f"origin/{expected_parent}"
            )
            # Get the tip of the expected parent
            parent_tip = await self._run_git("rev-parse", f"origin/{expected_parent}")

            if merge_base.strip() == parent_tip.strip():
                # Branch is correctly based on the expected parent
                return False, expected_parent

            # Mismatch — try to identify where it was actually branched from
            current_parent = await self._detect_branch_parent(branch_name)
            return True, current_parent

        except RuntimeError:
            # Can't determine — assume no mismatch
            return False, None

    async def _detect_branch_parent(self, branch_name: str) -> Optional[str]:
        """Best-effort detection of which branch this was forked from."""
        try:
            # Check common parent branches
            candidates = []
            for ref in ["master", "main", "develop"]:
                try:
                    await self._run_git("rev-parse", f"origin/{ref}")
                    candidates.append(ref)
                except RuntimeError:
                    continue

            # Also check all remote branches that look like epics or features
            try:
                refs_output = await self._run_git("branch", "-r", "--format=%(refname:short)")
                for ref in refs_output.splitlines():
                    ref = ref.strip().removeprefix("origin/")
                    if ref.startswith("EPIC-") or ref.startswith("feat/"):
                        candidates.append(ref)
            except RuntimeError:
                pass

            # Find which candidate has the closest merge-base to branch tip
            branch_tip = await self._run_git("rev-parse", branch_name)
            best_parent = None
            best_distance = float("inf")

            for candidate in candidates:
                try:
                    merge_base = await self._run_git(
                        "merge-base", branch_name, f"origin/{candidate}"
                    )
                    # Count commits between merge-base and branch tip
                    count_output = await self._run_git(
                        "rev-list", "--count", f"{merge_base.strip()}..{branch_tip.strip()}"
                    )
                    distance = int(count_output.strip())
                    if distance < best_distance:
                        best_distance = distance
                        best_parent = candidate
                except RuntimeError:
                    continue

            return best_parent
        except RuntimeError:
            return None

    async def _branch_exists(self, branch_name: str) -> bool:
        """Check if a local branch exists."""
        proc = await asyncio.create_subprocess_exec(
            "git", "branch", "--list", branch_name,
            cwd=str(self.project_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return bool(stdout.strip())

    async def _remove_worktree(self, worktree_path: Path) -> None:
        """Force-remove a worktree directory and clean up git refs."""
        try:
            await self._run_git("worktree", "remove", str(worktree_path), "--force")
        except RuntimeError:
            pass
        # Force-remove directory if git couldn't clean it
        if worktree_path.exists():
            import shutil
            shutil.rmtree(str(worktree_path), ignore_errors=True)
        try:
            await self._run_git("worktree", "prune")
        except RuntimeError:
            pass

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
