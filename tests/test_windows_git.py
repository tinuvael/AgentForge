"""Fake Win32 sources plus real offline Git; scratch locking is mocked.

These are orchestration tests, NOT Windows integration. Native tests are separate.
"""

import os
import shutil
import subprocess
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentforge.db.database import create_session_factory
from agentforge.db.projects import ProjectRepository
from agentforge.projects.backends import GitLocation
from agentforge.projects.errors import UnsafeProjectPath
from agentforge.projects.service import ProjectRegistry
from agentforge.projects.windows import WindowsSafeFilesystemBackend
from agentforge.tools import git_backend, windows_git
from agentforge.tools.errors import GitFailure, GitUnavailable, NotGitRepository
from agentforge.tools.service import RepositoryTools
from tests.test_windows_filesystem import FakeWin32
from tests.test_windows_filesystem import mocked_windows as windows_fixture

mocked_windows = windows_fixture


@pytest.fixture
def git_snapshot(database, tmp_path, monkeypatch):
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Synthetic Git contract needs a local Git executable")
    monkeypatch.setattr(
        windows_git,
        "_git_executable",
        lambda backend, repository, resources: executable,
    )
    original_scandir = os.scandir
    api = FakeWin32()
    monkeypatch.setattr(
        os,
        "scandir",
        lambda path: (
            api.scandir(path)
            if str(path).startswith("\\\\?\\")
            else original_scandir(path)
        ),
    )
    monkeypatch.setattr(os, "supports_fd", os.supports_fd | {os.scandir})
    root = tmp_path / "source"
    (root / "allowed").mkdir(parents=True)
    (root / "secret").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def run(*args):
        return subprocess.run(
            [
                executable,
                "-c",
                "user.name=Snapshot Tests",
                "-c",
                "user.email=snapshot@example.invalid",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-C",
                str(root),
                *args,
            ],
            env=env,
            shell=False,
            capture_output=True,
            check=True,
            timeout=5,
        )

    run("init", "--initial-branch=trunk")
    (root / "allowed" / "file.txt").write_bytes(b"needle allowed original\n")
    (root / "secret" / "outside.txt").write_bytes(b"needle OUTSIDE SECRET\n")
    (root / "allowed" / ".env").write_bytes(b"PRIVATE ENV\n")
    run("add", ".")
    run("commit", "-m", "Initial")

    def load():
        # Preserve the registered root identity across synthetic source updates.
        old = api.nodes.get("c:\\repo\\allowed")
        old_identity = old.info.identity if old else None
        api.nodes = {"c:\\": api.nodes["c:\\"]}
        api.add(r"C:\Repo", directory=True)
        for path in sorted(root.rglob("*")):
            target = "C:\\Repo\\" + str(path.relative_to(root)).replace(os.sep, "\\")
            node = api.add(
                target,
                b"" if path.is_dir() else path.read_bytes(),
                directory=path.is_dir(),
            )
            if target == r"C:\Repo\allowed" and old_identity:
                node.info = replace(node.info, identity=old_identity)

    load()
    backend = WindowsSafeFilesystemBackend(api)
    registry = ProjectRegistry(
        ProjectRepository(create_session_factory(database[0])),
        base_directory="C:\\",
        filesystem=backend,
    )
    project = registry.register_project("Snapshot subproject", r"C:\Repo\allowed")
    snapshots = []

    class MockScratch:
        """Only snapshot orchestration is tested here; native sealing is separate."""

        def __init__(self, *args):
            pass

        def parent(self, path):
            path.mkdir(parents=True, exist_ok=True)

        def copy(self, source, target, budget, cap):
            self.parent(target.parent)
            with target.open("xb") as destination:
                size = 0
                while data := source.read(64 * 1024):
                    size += len(data)
                    if size > cap:
                        raise GitFailure("Mock scratch cap")
                    budget.data(len(data))
                    destination.write(data)

        write_config = windows_git._Scratch.write_config

    monkeypatch.setattr(windows_git, "_Scratch", MockScratch)

    def fake_pin(_backend, destination, _resources):
        files = {
            p.relative_to(destination).as_posix(): p.read_bytes()
            for p in destination.rglob("*")
            if p.is_file()
        }
        assert not any(
            name.startswith("secret/") or name == "allowed/.env" for name in files
        )
        assert "filter" not in files[".git/config"].decode()
        snapshots.append(files)

    monkeypatch.setattr(windows_git, "_pin_snapshot", fake_pin)
    return SimpleNamespace(
        root=root,
        run=run,
        api=api,
        load=load,
        backend=backend,
        registry=registry,
        project=project,
        tools=RepositoryTools(registry),
        snapshots=snapshots,
    )


def test_snapshot_status_grep_diff_subproject_contract(git_snapshot):
    s = git_snapshot
    assert s.tools.git_status(s.project.id).changes == ()
    assert [m.path for m in s.tools.git_grep(s.project.id, "needle").matches] == [
        "file.txt"
    ]
    assert s.tools.git_grep(s.project.id, "OUTSIDE").matches == ()
    for scope, name in (("allowed", "file.txt"), ("secret", "outside.txt")):
        (s.root / scope / name).write_bytes(b"needle staged " + scope.encode() + b"\n")
    s.run("add", ".")
    for scope, name in (("allowed", "file.txt"), ("secret", "outside.txt")):
        (s.root / scope / name).write_bytes(
            b"needle unstaged " + scope.encode() + b"\n"
        )
    s.load()
    assert [c.path for c in s.tools.git_status(s.project.id).changes] == ["file.txt"]
    for staged in (False, True):
        result = s.tools.git_diff(s.project.id, staged=staged)
        assert (
            "file.txt" in result.content
            and "secret" not in result.content
            and "PRIVATE" not in result.content
        )
    assert s.snapshots
    assert not any(p.startswith(r"C:\Repo\secret") for p in s.api.read_paths)


@pytest.mark.parametrize("outgoing", [False, True])
def test_snapshot_cross_boundary_rename(git_snapshot, outgoing):
    s = git_snapshot
    s.run(
        "mv",
        *(
            ["allowed/file.txt", "secret/moved.txt"]
            if outgoing
            else ["secret/outside.txt", "allowed/moved.txt"]
        ),
    )
    s.load()
    result = s.tools.git_diff(s.project.id, staged=True)
    assert "secret/" not in result.content and "rename from" not in result.content
    if outgoing:
        assert (
            "-needle allowed original" in result.content
            and "OUTSIDE SECRET" not in result.content
        )
    else:
        assert (
            "+needle OUTSIDE SECRET" in result.content
        )  # Destination is now authorized.


def test_snapshot_discards_executable_repository_config(git_snapshot):
    s = git_snapshot
    for key, value in (
        ("core.fsmonitor", "invalid-fsmonitor-command"),
        ("filter.hostile.clean", "invalid-clean-command"),
        ("filter.hostile.required", "true"),
        ("diff.external", "invalid-diff-command"),
        ("include.path", str(s.root.parent / "unreadable")),
    ):
        s.run("config", key, value)
    (s.root / "allowed" / ".gitattributes").write_bytes(
        b"file.txt filter=hostile diff=hostile\n"
    )
    s.load()
    s.tools.git_status(s.project.id)
    s.tools.git_grep(s.project.id, "needle")
    s.tools.git_diff(s.project.id, staged=True)
    assert not any(p.endswith("\\config") for p in s.api.read_paths)


@pytest.mark.parametrize(
    "path", ["objects/info/alternates", "objects/info/http-alternates", "commondir"]
)
def test_snapshot_metadata_redirection_denied(git_snapshot, path):
    s = git_snapshot
    target = s.root / ".git" / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"../../outside")
    s.load()
    with pytest.raises(GitUnavailable):
        s.tools.git_status(s.project.id)
    assert not s.snapshots


def test_snapshot_metadata_reparse_denied(git_snapshot):
    s = git_snapshot
    node = s.api.nodes["c:\\repo\\.git\\objects"]
    node.info = replace(node.info, attributes=0x410)
    with pytest.raises(UnsafeProjectPath):
        s.tools.git_status(s.project.id)
    assert not s.snapshots


def test_snapshot_directory_to_file_diff_cannot_expand_sensitive_head(git_snapshot):
    s = git_snapshot
    (s.root / "allowed" / "public").mkdir()
    (s.root / "allowed" / "public" / ".env").write_bytes(b"PRIVATE HEAD CONTENT\n")
    s.run("add", ".")
    s.run("commit", "-m", "Head directory")
    shutil.rmtree(s.root / "allowed" / "public")
    (s.root / "allowed" / "public").write_bytes(b"public replacement\n")
    s.run("add", ".")
    s.load()
    with pytest.raises(GitFailure, match="expanded"):
        s.tools.git_diff(s.project.id, staged=True)


def test_snapshot_bounds_fail_closed(git_snapshot, monkeypatch):
    s = git_snapshot
    monkeypatch.setattr(windows_git, "SNAPSHOT_BYTES", 10)
    with pytest.raises(GitFailure, match="limit"):
        s.tools.git_status(s.project.id)
    assert not s.snapshots


def test_snapshot_staged_symlink_content_is_unsupported(git_snapshot):
    s = git_snapshot
    object_id = s.run("hash-object", "-w", "--stdin").stdout.decode().strip()
    s.run("update-index", "--cacheinfo", "120000," + object_id + ",allowed/file.txt")
    s.load()
    with pytest.raises(GitFailure):
        s.tools.git_diff(s.project.id, staged=True)


def test_snapshot_returned_paths_case_and_sibling_filter(git_snapshot):
    s = git_snapshot
    with s.registry.open_root(s.project.id) as (project, handle):
        git = git_backend._Git(project.root_path, handle)
        assert git._scoped("ALLOWED/file.txt") == "file.txt"
        for path in (
            "allowed-secret/file.txt",
            "secret/outside.txt",
            "allowed/.ENV",
            "allowed/file.txt:stream",
            "allowed/NUL.txt",
            "allowed/file.txt.",
        ):
            assert git._scoped(path) is None


def test_isolated_process_options_and_termination():
    location = GitLocation("scratch", prefix="allowed")
    assert location.process_options() == {"close_fds": True}
    killed = []
    location.stop(SimpleNamespace(kill=lambda: killed.append(True)))
    assert killed == [True]


def test_snapshot_non_git_and_gitfile_fail_closed(mocked_windows, monkeypatch):
    s = mocked_windows
    monkeypatch.setattr(windows_git.shutil, "which", lambda _: "git.exe")
    with s.registry.open_root(s.project.id) as (_, handle):
        with pytest.raises(NotGitRepository):
            windows_git.snapshot_git(s.backend, handle)
    s.api.add(r"C:\Repo\.git", b"gitdir: C:\\outside")
    with s.registry.open_root(s.project.id) as (_, handle):
        with pytest.raises(GitUnavailable):
            windows_git.snapshot_git(s.backend, handle)


def test_executable_lookup_ignores_cwd_relative_and_worktree_paths(
    mocked_windows, monkeypatch
):
    s = mocked_windows
    s.api.add(r"C:\Tools", directory=True)
    s.api.add(r"C:\Tools\git.exe", b"trusted installation")
    monkeypatch.setenv("PATH", r".;relative;C:\Repo;C:\Repo\nested;C:\Tools")
    lookups = []

    def which(candidate):
        lookups.append(candidate)
        assert candidate == r"C:\Tools\git.exe"
        return candidate

    monkeypatch.setattr(windows_git.shutil, "which", which)
    with s.registry.open_root(s.project.id) as (_, root):
        with ExitStack() as resources:
            assert windows_git._git_executable(s.backend, root, resources) == (
                r"C:\Tools\git.exe"
            )
            assert r"C:\Tools\git.exe" in s.api.opened
    assert lookups == [r"C:\Tools\git.exe"]


def test_executable_lookup_uses_real_git_for_windows_binary(
    mocked_windows, monkeypatch
):
    s = mocked_windows
    for path in (r"C:\Git", r"C:\Git\mingw64", r"C:\Git\mingw64\bin"):
        s.api.add(path, directory=True)
    executable = r"C:\Git\mingw64\bin\git.exe"
    s.api.add(executable, b"real installed command")
    monkeypatch.setenv("PATH", r"C:\Git\cmd")
    lookups = []

    def which(candidate):
        lookups.append(candidate)
        return candidate if candidate == executable else None

    monkeypatch.setattr(windows_git.shutil, "which", which)
    with s.registry.open_root(s.project.id) as (_, root):
        with ExitStack() as resources:
            assert windows_git._git_executable(s.backend, root, resources) == executable
    assert lookups == [executable]


@pytest.mark.parametrize("ancestor", [False, True])
def test_executable_lookup_reparse_fails_closed(mocked_windows, monkeypatch, ancestor):
    s = mocked_windows
    s.api.add(r"C:\Tools", directory=True, reparse=ancestor)
    s.api.add(r"C:\Tools\git.exe", b"untrusted", reparse=not ancestor)
    monkeypatch.setenv("PATH", r"C:\Tools")
    monkeypatch.setattr(windows_git.shutil, "which", lambda candidate: candidate)
    with s.registry.open_root(s.project.id) as (_, root):
        with ExitStack() as resources:
            with pytest.raises(UnsafeProjectPath):
                windows_git._git_executable(s.backend, root, resources)


def test_executable_lookup_no_safe_installation(mocked_windows, monkeypatch):
    s = mocked_windows
    monkeypatch.setenv("PATH", r".;relative;C:\Repo;C:relative;\\host\share")
    monkeypatch.setattr(
        windows_git.shutil, "which", lambda _: pytest.fail("Unsafe PATH lookup")
    )
    with s.registry.open_root(s.project.id) as (_, root):
        with ExitStack() as resources:
            with pytest.raises(GitUnavailable):
                windows_git._git_executable(s.backend, root, resources)
