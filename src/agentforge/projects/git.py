"""Offline, read-only Git observations without parsing repository internals."""

import os
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


def inspect_git(root_fd) -> GitMetadata:
    from agentforge.projects.backends import backend_for

    return backend_for(root_fd).inspect_git(root_fd)


def _inspect_posix_git(root_fd: int) -> GitMetadata:
    """Observe Git through an already-authorized registered directory descriptor.

    No inherited GIT_* setting may redirect discovery to another repository.
    Global/system configuration and optional locks are disabled. These separate
    reads are best effort, not an atomic snapshot of a concurrently changing repo.
    """
    from agentforge.projects.backends import GitLocation
    from agentforge.tools.errors import GitFailure, GitTimeout, GitUnavailable
    from agentforge.tools.git_backend import _run_git

    observed_at = datetime.now(UTC)
    env = git_environment()
    root = Path(f"/proc/self/fd/{root_fd}")
    if not root.is_dir():
        # No pathname fallback: Git is observationally unavailable without a
        # descriptor-backed cwd, rather than inspecting an unverified replacement.
        return GitMetadata(status="unavailable", observed_at=observed_at)

    def run(*args: str):
        result = _run_git(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            GitLocation(str(root), (root_fd,)),
            4096,
            environment=env,
            allow_failure=True,
        )
        if result.truncated:
            raise GitFailure("Git inspection output exceeded its limit")
        return result

    try:
        discovery = run("rev-parse", "--is-bare-repository")
        if discovery.returncode:
            status = "not_repository" if discovery.not_repository else "unavailable"
            return GitMetadata(status=status, observed_at=observed_at)
        root_result = run(
            "rev-parse",
            "--absolute-git-dir"
            if discovery.data.strip() == b"true"
            else "--show-toplevel",
        )
        if root_result.returncode:
            return GitMetadata(status="unavailable", observed_at=observed_at)
        repository_root = Path(
            root_result.data.decode("utf-8").removesuffix("\n")
        ).resolve(strict=True)
        branch = run("symbolic-ref", "--quiet", "--short", "HEAD")
        head = run("rev-parse", "--verify", "HEAD^{commit}")
        return GitMetadata(
            status="repository",
            observed_at=observed_at,
            repository_root=repository_root,
            branch=branch.data.decode("utf-8").strip()
            if branch.returncode == 0
            else None,
            head_commit=head.data.decode("ascii").strip()
            if head.returncode == 0
            else None,
        )
    except (OSError, RuntimeError, ValueError, GitFailure, GitTimeout, GitUnavailable):
        return GitMetadata(status="unavailable", observed_at=observed_at)
