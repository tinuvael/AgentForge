"""Local-only Git observations: no user config, user repositories or network."""

import os
import shutil
import subprocess
from datetime import UTC, datetime

import pytest

from agentforge.projects import git as git_module


@pytest.fixture
def git(tmp_path):
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Local Git executable is required for repository fixtures")
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def run(root, *args):
        return subprocess.run(
            [
                executable,
                "-c",
                "user.name=Registry Tests",
                "-c",
                "user.email=registry-tests@example.invalid",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(root),
                *args,
            ],
            env=env,
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        ).stdout.strip()

    return run


@pytest.fixture
def git_root(tmp_path, git):
    root = tmp_path / "git-project"
    root.mkdir()
    git(root, "init", "--initial-branch=trunk")
    git(root, "commit", "--allow-empty", "-m", "Initial local commit")
    return root


def test_register_git_and_live_refresh(registry, git_root, git):
    project = registry.register_project("Git", git_root)
    first = registry.inspect_project(project.id)
    assert first.project == project
    assert first.git.is_repository is True
    assert first.git.repository_root == git_root
    assert first.git.branch == "trunk"
    assert first.git.head_commit == git(git_root, "rev-parse", "HEAD")
    assert first.git.observed_at.tzinfo == UTC
    assert first.git.observed_at <= datetime.now(UTC)
    git(git_root, "checkout", "-b", "another-branch")
    git(git_root, "commit", "--allow-empty", "-m", "Another local commit")
    current = registry.inspect_project(project.id)
    assert current.git.branch == "another-branch"
    assert current.git.head_commit != first.git.head_commit
    assert current.git.head_commit == git(git_root, "rev-parse", "HEAD")
    assert registry.get_project(project.id) == project


def test_multiple_unrelated_repositories(registry, git_root, tmp_path, git):
    other_root = tmp_path / "other-repository"
    other_root.mkdir()
    git(other_root, "init", "--initial-branch=other")
    first = registry.register_project("First", git_root)
    second = registry.register_project("Second", other_root)
    assert registry.list_projects() == [first, second]
    assert registry.inspect_project(first.id).git.repository_root == git_root
    assert registry.inspect_project(second.id).git.repository_root == other_root


def test_detached_head(registry, git_root, git):
    head = git(git_root, "rev-parse", "HEAD")
    git(git_root, "checkout", "--detach", head)
    project = registry.register_project("Detached", git_root)
    metadata = registry.inspect_project(project.id).git
    assert metadata.status == "repository"
    assert metadata.branch is None
    assert metadata.head_commit == head


def test_unborn_repository(registry, tmp_path, git):
    root = tmp_path / "unborn"
    root.mkdir()
    git(root, "init", "--initial-branch=unborn-branch")
    project = registry.register_project("Unborn", root)
    metadata = registry.inspect_project(project.id).git
    assert metadata.status == "repository"
    assert metadata.branch == "unborn-branch"
    assert metadata.head_commit is None


def test_linked_worktree(registry, tmp_path, git_root, git):
    worktree = tmp_path / "linked"
    git(git_root, "worktree", "add", "-b", "linked-branch", str(worktree))
    project = registry.register_project("Worktree", worktree)
    metadata = registry.inspect_project(project.id).git
    assert metadata.repository_root == worktree
    assert metadata.branch == "linked-branch"
    assert metadata.head_commit == git(git_root, "rev-parse", "HEAD")


def test_nested_git_root_does_not_expand_boundary(registry, git_root):
    subdirectory = git_root / "nested"
    subdirectory.mkdir()
    project = registry.register_project("Nested", subdirectory)
    inspection = registry.inspect_project(project.id)
    assert inspection.project.root_path == subdirectory
    assert inspection.git.repository_root == git_root


def test_bare_repository(registry, tmp_path, git):
    root = tmp_path / "bare"
    root.mkdir()
    git(root, "init", "--bare", "--initial-branch=trunk")
    project = registry.register_project("Bare", root)
    metadata = registry.inspect_project(project.id).git
    assert metadata.repository_root == root
    assert metadata.branch == "trunk"
    assert metadata.head_commit is None


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError, "timeout"])
def test_git_unavailable_is_observational(registry, tmp_path, monkeypatch, failure):
    def unavailable(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], 2)
        raise failure("Git unavailable")

    monkeypatch.setattr(git_module.subprocess, "run", unavailable)
    project = registry.register_project("Normal", tmp_path)
    metadata = registry.inspect_project(project.id).git
    assert metadata.status == "unavailable"
    assert metadata.is_repository is None
    assert metadata.branch is None
    assert metadata.head_commit is None
    assert registry.get_project(project.id) == project


def test_inherited_git_settings_do_not_redirect_inspection(
    registry, git_root, tmp_path, monkeypatch
):
    normal = tmp_path / "plain"
    normal.mkdir()
    monkeypatch.setenv("GIT_DIR", str(git_root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(git_root))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    project = registry.register_project("Plain", normal)
    assert registry.inspect_project(project.id).git.status == "not_repository"
    git_project = registry.register_project("Git", git_root)
    assert registry.inspect_project(git_project.id).git.branch == "trunk"


def test_git_inspection_is_read_only_and_bounded(registry, git_root, monkeypatch):
    original_run = subprocess.run
    invocations = []
    before = {p: p.read_bytes() for p in (git_root / ".git").rglob("*") if p.is_file()}

    def checked_run(args, **kwargs):
        assert args[:4] == ["git", "--no-optional-locks", "-C", str(git_root)]
        assert args[4] in {"rev-parse", "symbolic-ref"}
        assert kwargs["timeout"] == 2
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        invocations.append(args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(git_module.subprocess, "run", checked_run)
    project = registry.register_project("Git", git_root)
    assert registry.inspect_project(project.id).git.status == "repository"
    after = {p: p.read_bytes() for p in (git_root / ".git").rglob("*") if p.is_file()}
    assert after == before
    assert len(invocations) == 4


def test_git_refusal_is_unavailable(registry, tmp_path, monkeypatch):
    def refused(args, **kwargs):
        return subprocess.CompletedProcess(args, 128, "", "fatal: dubious ownership")

    monkeypatch.setattr(git_module.subprocess, "run", refused)
    project = registry.register_project("Refused", tmp_path)
    assert registry.inspect_project(project.id).git.status == "unavailable"
