"""Offline, read-only Git observations without parsing repository internals."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agentforge.projects.models import GitMetadata


def git_environment() -> dict[str, str]:
    """Shared Git isolation: no inherited Git redirection/configuration."""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="",
        GIT_ATTR_NOSYSTEM="1",
        GIT_PAGER="cat",
        LC_ALL="C",
    )
    return env


def inspect_git(root: Path) -> GitMetadata:
    """Observe worktrees (including linked worktrees) and bare repositories.

    No inherited GIT_* setting may redirect discovery to another repository.
    Global/system configuration and optional locks are disabled. These separate
    reads are best effort, not an atomic snapshot of a concurrently changing repo.
    """
    observed_at = datetime.now(UTC)
    env = git_environment()

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )

    try:
        discovery = run("rev-parse", "--is-bare-repository")
        if discovery.returncode:
            status = (
                "not_repository"
                if "not a git repository" in discovery.stderr
                else "unavailable"
            )
            return GitMetadata(status=status, observed_at=observed_at)
        root_result = run(
            "rev-parse",
            "--absolute-git-dir"
            if discovery.stdout.strip() == "true"
            else "--show-toplevel",
        )
        if root_result.returncode:
            return GitMetadata(status="unavailable", observed_at=observed_at)
        repository_root = Path(root_result.stdout.removesuffix("\n")).resolve(
            strict=True
        )
        branch = run("symbolic-ref", "--quiet", "--short", "HEAD")
        head = run("rev-parse", "--verify", "HEAD^{commit}")
        return GitMetadata(
            status="repository",
            observed_at=observed_at,
            repository_root=repository_root,
            branch=branch.stdout.strip() if branch.returncode == 0 else None,
            head_commit=head.stdout.strip() if head.returncode == 0 else None,
        )
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return GitMetadata(status="unavailable", observed_at=observed_at)
