"""Real temporary Git repositories; no Provider, host repository or networking."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentforge.agents.models import CancellationToken
from agentforge.coding.config import CodingConfig, ValidationCommand
from agentforge.coding.models import CodingError, CodingLimit, EditConflict
from agentforge.coding.service import CodingWorkspaceManager
from agentforge.coding.tools import (
    DeleteArguments,
    InspectionArguments,
    PatchArguments,
    WriteArguments,
    coding_toolset,
)
from agentforge.db.coding import WorkspaceRepository
from agentforge.db.database import create_session_factory
from agentforge.projects.errors import UnsafeProjectPath
from agentforge.tools.errors import (
    InvalidToolArgument,
    SensitivePath,
    UnsupportedTextFile,
)


def git(root, *args):
    return subprocess.check_output(
        [shutil.which("git"), "-C", str(root), *args], stderr=subprocess.PIPE
    )


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def coding(registry, database, tmp_path):
    root = tmp_path / "primary"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Offline Test")
    (root / "source.py").write_text("original\n")
    (root / "allowed").mkdir()
    (root / "allowed" / "a.py").write_text("allowed\n")
    (root / "secret").mkdir()
    (root / "secret" / "private.py").write_text("SIBLING_SECRET\n")
    (root / ".env").write_text("PRIVATE_TOKEN\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    parent = tmp_path / "workspaces"
    parent.mkdir(mode=0o700)
    executable = Path(shutil.which("git")).resolve()
    if os.name == "nt" and executable.parent.name.lower() in {"cmd", "bin"}:
        for architecture in ("mingw64", "mingw32"):
            real = executable.parent.parent / architecture / "bin/git.exe"
            if real.is_file():
                executable = real
                break
    config = CodingConfig(
        workspace_parent=parent,
        git_executable=executable,
        validations={
            "check": ValidationCommand(
                argv=(str(Path(sys.executable).resolve()), "-c", "print('checked')")
            )
        },
    )
    repo = WorkspaceRepository(create_session_factory(database[0]))
    manager = CodingWorkspaceManager(registry, repo, config)
    project = registry.register_project("Primary", root)
    task = SimpleNamespace(
        task_id=uuid4(),
        project_id=project.id,
        worker_id="fake-worker",
        agent_id="coder",
        state="running",
    )

    def create(subproject=False):
        if subproject:
            p = registry.register_project("Subtree", root / "allowed")
            task.project_id = p.id
        report = manager.create(task)
        row = manager._record(task.task_id)
        workspace, tool_root = manager._paths(row)
        token = CancellationToken()
        session = manager.bind(task, token)
        return SimpleNamespace(
            report=report,
            row=row,
            workspace=workspace,
            tool_root=tool_root,
            session=session,
            token=token,
        )

    return SimpleNamespace(
        root=root,
        parent=parent,
        config=config,
        repository=repo,
        manager=manager,
        project=project,
        registry=registry,
        task=task,
        create=create,
    )


def write(setup, path, content, expected=None):
    return setup.session.edit(
        setup.session.task.project_id,
        WriteArguments(path=path, content=content, expected_sha256=expected),
        12000,
        operation="write",
    )


def patch(setup, path, old, new, expected):
    return setup.session.edit(
        setup.session.task.project_id,
        PatchArguments(path=path, old_text=old, new_text=new, expected_sha256=expected),
        12000,
        operation="patch",
    )


def delete(setup, path, expected):
    return setup.session.edit(
        setup.session.task.project_id,
        DeleteArguments(path=path, expected_sha256=expected),
        12000,
        operation="delete",
    )


def primary_state(root):
    return (
        (root / "source.py").read_bytes(),
        (root / ".git" / "index").read_bytes(),
        (root / ".git" / "HEAD").read_bytes(),
        git(root, "rev-parse", "HEAD"),
        git(root, "symbolic-ref", "HEAD"),
        git(root, "status", "--porcelain"),
    )


@pytest.mark.parametrize("dirty", [False, True])
def test_create_base_and_primary_invariant(coding, dirty):
    if dirty:
        (coding.root / "source.py").write_text("USER_UNCOMMITTED\n")
        (coding.root / "staged.txt").write_text("USER_STAGED\n")
        git(coding.root, "add", "staged.txt")
    before = primary_state(coding.root)
    setup = coding.create()
    assert setup.report.inspection_available
    assert setup.report.state == "ready"
    assert (
        setup.report.base_commit
        == git(coding.root, "rev-parse", "HEAD").decode().strip()
    )
    assert setup.report.branch_name == "agentforge/task-" + str(coding.task.task_id)
    assert not setup.workspace.is_relative_to(coding.root)
    assert (setup.workspace / "source.py").read_text() == "original\n"
    assert (
        setup.session.reads.read_file(coding.project.id, "source.py").content
        == "original\n"
    )
    write(setup, "new/sub.py", "created\n")
    patch(setup, "source.py", "original", "edited", sha("original\n"))
    assert (
        setup.session.reads.read_file(coding.project.id, "source.py").content
        == "edited\n"
    )
    assert setup.session.reads.search_code(coding.project.id, "edited").matches
    delete(setup, "allowed/a.py", sha("allowed\n"))
    diff = coding.manager.diff(coding.task.task_id)
    assert set(diff.changed_files) == {"new/sub.py", "source.py", "allowed/a.py"}
    assert (
        "+edited" in diff.content
        and "+created" in diff.content
        and "-allowed" in diff.content
    )
    assert "PRIVATE_TOKEN" not in diff.content
    assert primary_state(coding.root) == before
    assert coding.manager.get(coding.task.task_id).bytes_written == len(
        "created\nedited\n"
    )


def test_subproject_never_exposes_siblings(coding):
    before = primary_state(coding.root)
    setup = coding.create(subproject=True)
    assert setup.tool_root == setup.workspace / "allowed"
    assert not (setup.workspace / "secret").exists()
    assert git(setup.workspace, "status", "--porcelain") == b""
    assert setup.session.reads.list_files(coding.task.project_id).paths == ("a.py",)
    write(setup, "a.py", "changed\n", sha("allowed\n"))
    assert coding.manager.diff(coding.task.task_id).changed_files == ("a.py",)
    assert git(setup.workspace, "diff", "--name-only") == b"allowed/a.py\n"
    for path in ("../secret/private.py", "../../primary/secret/private.py"):
        with pytest.raises(UnsafeProjectPath):
            setup.session.reads.read_file(coding.task.project_id, path)
        with pytest.raises(UnsafeProjectPath):
            write(setup, path, "escape")
    assert primary_state(coding.root) == before


def test_collision_and_duplicate_workspace_refused(coding):
    git(coding.root, "branch", "agentforge/task-" + str(coding.task.task_id))
    with pytest.raises(CodingError, match="collision"):
        coding.create()
    assert not list(coding.parent.glob("*/*"))
    git(coding.root, "branch", "-D", "agentforge/task-" + str(coding.task.task_id))
    coding.create()
    with pytest.raises(CodingError, match="already"):
        coding.create()


def test_durable_reopen_recovery_and_primary_move(coding):
    setup = coding.create()
    write(setup, "source.py", "partial\n", sha("original\n"))
    git(coding.root, "commit", "--allow-empty", "-m", "primary moved")
    reopened = CodingWorkspaceManager(coding.registry, coding.repository, coding.config)
    reopened.recover()
    report = reopened.get(coding.task.task_id)
    assert report.state == "interrupted"
    assert report.base_commit == setup.report.base_commit
    assert report.branch_name == setup.report.branch_name
    assert "partial" in reopened.diff(coding.task.task_id).content
    assert report.base_commit != git(coding.root, "rev-parse", "HEAD").decode().strip()
    with pytest.raises(CodingError):
        reopened.create(coding.task)


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
def test_cleanup_explicit_identity_and_branch_retained(coding, state):
    before = primary_state(coding.root)
    setup = coding.create()
    write(setup, "source.py", "partial\n", sha("original\n"))
    with pytest.raises(CodingError):
        coding.manager.cleanup(coding.task.task_id, setup.report.workspace_id)
    coding.task.state = state
    coding.manager.finalize(coding.task)
    with pytest.raises(CodingError):
        coding.manager.cleanup(coding.task.task_id, uuid4())
    assert setup.workspace.exists()
    report = coding.manager.cleanup(coding.task.task_id, setup.report.workspace_id)
    assert report.state == "removed"
    assert not setup.workspace.exists()
    assert report.changed_files == ("source.py",)
    assert not report.inspection_available
    assert (
        git(coding.root, "rev-parse", setup.report.branch_name).decode().strip()
        == setup.report.base_commit
    )
    assert (
        str(setup.workspace)
        not in git(coding.root, "worktree", "list", "--porcelain").decode()
    )
    assert primary_state(coding.root) == before
    assert (
        coding.manager.cleanup(coding.task.task_id, setup.report.workspace_id).state
        == "removed"
    )


@pytest.mark.posix
@pytest.mark.parametrize(
    "kind", ["root", "parent", "gitfile", "registration", "symlink", "hardlink"]
)
def test_cleanup_replacement_refused(coding, kind, tmp_path):
    setup = coding.create()
    coding.task.state = "failed"
    coding.manager.finalize(coding.task)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("KEEP")
    if kind == "root":
        setup.workspace.rename(setup.workspace.with_name("moved"))
        setup.workspace.symlink_to(outside, target_is_directory=True)
    elif kind == "parent":
        setup.workspace.parent.rename(setup.workspace.parent.with_name("moved"))
        setup.workspace.parent.symlink_to(outside, target_is_directory=True)
    elif kind == "gitfile":
        (setup.workspace / ".git").write_text("gitdir: " + str(outside))
    elif kind == "registration":
        admin = (
            Path(setup.row["repository_path"])
            / ".git/worktrees"
            / setup.row["identities"]["admin_name"]
        )
        admin.rename(admin.with_name("moved"))
        admin.mkdir()
    elif kind == "symlink":
        (setup.workspace / "escape").symlink_to(outside, target_is_directory=True)
    else:
        os.link(outside / "keep.txt", setup.workspace / "escape")
    with pytest.raises((CodingError, UnsafeProjectPath)):
        coding.manager.cleanup(coding.task.task_id, setup.report.workspace_id)
    assert (outside / "keep.txt").read_text() == "KEEP"


def test_orphans_are_only_reported(coding):
    orphan = coding.parent / str(uuid4()) / str(uuid4())
    orphan.mkdir(parents=True)
    (orphan / "keep").write_text("partial")
    coding.manager.recover()
    assert coding.manager.orphans() == ((orphan.parent.name, orphan.name),)
    assert (orphan / "keep").read_text() == "partial"


@pytest.mark.parametrize(
    "path",
    [
        "../../secret",
        "/tmp/secret",
        "C:/secret",
        "C:relative",
        r"\\server\share\secret",
        r"\\?\C:\secret",
        r"\??\C:\secret",
        "file:ADS",
        "file.",
        "file ",
        "NUL",
        "CON.txt",
        "COM1.txt",
        "LPT¹",
        "a/.. /b",
        ".git/config",
        ".env",
        "secret.key",
        ".ssh/id_rsa",
    ],
)
def test_cross_platform_write_path_policy(coding, path):
    setup = coding.create()
    for operation in (
        lambda: write(setup, path, "attack"),
        lambda: patch(setup, path, "x", "y", sha("x")),
        lambda: delete(setup, path, sha("x")),
    ):
        with pytest.raises((UnsafeProjectPath, SensitivePath)):
            operation()


@pytest.mark.posix
@pytest.mark.parametrize(
    "kind", ["file_link", "directory_link", "hardlink", "root", "directory"]
)
def test_write_escape_denied(coding, kind, tmp_path):
    setup = coding.create()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("outside")
    path = "escape"
    if kind == "file_link":
        (setup.workspace / path).symlink_to(outside / "file")
    elif kind == "directory_link":
        (setup.workspace / path).symlink_to(outside, target_is_directory=True)
        path += "/file"
    elif kind == "hardlink":
        os.link(outside / "file", setup.workspace / path)
    elif kind == "root":
        setup.workspace.rename(setup.workspace.with_name("moved"))
        setup.workspace.symlink_to(outside, target_is_directory=True)
        path = "file"
    else:
        (setup.workspace / path).mkdir()
    with pytest.raises((UnsafeProjectPath, CodingError, InvalidToolArgument)):
        write(setup, path, "attack", sha("outside"))
    with pytest.raises((UnsafeProjectPath, CodingError, InvalidToolArgument)):
        delete(setup, path, sha("outside"))
    assert (outside / "file").read_text() == "outside"


def test_optimistic_preconditions_and_binary(coding):
    setup = coding.create()
    with pytest.raises(EditConflict):
        write(setup, "source.py", "blind", sha("stale"))
    with pytest.raises(EditConflict):
        write(setup, "source.py", "blind")
    with pytest.raises(EditConflict):
        patch(setup, "source.py", "original", "blind", sha("stale"))
    with pytest.raises(EditConflict):
        delete(setup, "source.py", sha("stale"))
    with pytest.raises(InvalidToolArgument):
        patch(setup, "source.py", "not present", "x", sha("original\n"))
    (setup.workspace / "binary").write_bytes(b"\xff\0")
    for operation in (
        lambda: write(setup, "binary", "new", hashlib.sha256(b"\xff\0").hexdigest()),
        lambda: delete(setup, "binary", hashlib.sha256(b"\xff\0").hexdigest()),
        lambda: write(setup, "new", "\0"),
    ):
        with pytest.raises(UnsupportedTextFile):
            operation()
    assert (setup.workspace / "source.py").read_text() == "original\n"


@pytest.mark.parametrize("limit", ["file", "patch", "total", "files", "calls"])
def test_edit_budgets(coding, limit):
    from agentforge.coding.models import CodingLimits

    values = {
        "max_file_bytes": 16,
        "max_patch_bytes": 4,
        "max_total_write_bytes": 17,
        "max_changed_files": 1,
        "max_write_calls": 1,
    }
    key = {
        "file": "max_file_bytes",
        "patch": "max_patch_bytes",
        "total": "max_total_write_bytes",
        "files": "max_changed_files",
        "calls": "max_write_calls",
    }[limit]
    coding.manager.config = coding.config.model_copy(
        update={"limits": CodingLimits(**{key: values[key]})}
    )
    setup = coding.create()
    if limit == "patch":

        def operation():
            return patch(setup, "source.py", "original", "edited", sha("original\n"))
    elif limit == "file":

        def operation():
            return write(setup, "new", "x" * 17)
    elif limit == "total":
        write(setup, "new", "x" * 10)

        def operation():
            return write(setup, "new", "x" * 10, sha("x" * 10))
    else:
        write(setup, "new", "x")

        def operation():
            return write(
                setup,
                "other" if limit == "files" else "new",
                "y",
                None if limit == "files" else sha("x"),
            )

    with pytest.raises(CodingLimit):
        operation()


def test_diff_truncation_is_explicit(coding):
    setup = coding.create()
    write(setup, "new", "line\n" * 100)
    diff = coding.manager.diff(coding.task.task_id, max_bytes=100)
    assert diff.truncated
    assert len(diff.content.encode()) <= 100
    status = coding_toolset(setup.session)["git_status"].execute(
        coding.task.project_id, InspectionArguments(), 800
    )
    assert status["truncated"]
    assert status["changed_files"] == diff.changed_files


def test_cancel_refuses_further_edits_preserves_partial(coding):
    setup = coding.create()
    write(setup, "new", "partial")
    setup.token.cancel()
    with pytest.raises(CodingError):
        write(setup, "new", "changed", sha("partial"))
    assert "partial" in coding.manager.diff(coding.task.task_id).content


@pytest.mark.posix
def test_hostile_git_hooks_filters_and_drivers_never_execute(coding, tmp_path):
    marker = tmp_path / "executed"
    payload = f"touch {marker}"
    for key in (
        "core.fsmonitor",
        "filter.evil.clean",
        "filter.evil.smudge",
        "filter.evil.process",
        "diff.evil.command",
        "diff.evil.textconv",
        "core.pager",
    ):
        git(coding.root, "config", key, payload)
    (coding.root / ".gitattributes").write_text("*.py filter=evil diff=evil\n")
    # Commit attributes without invoking the hostile source filter.
    git(
        coding.root,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "filter.evil.process=",
        "-c",
        "filter.evil.clean=",
        "add",
        ".gitattributes",
    )
    git(
        coding.root,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "filter.evil.process=",
        "-c",
        "filter.evil.clean=",
        "commit",
        "-m",
        "attributes",
    )
    marker.unlink(missing_ok=True)
    hook = coding.root / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\n" + payload + "\n")
    hook.chmod(0o700)
    before = (coding.root / ".git/index").read_bytes()
    setup = coding.create()
    write(setup, "source.py", "edited\n", sha("original\n"))
    assert "+edited" in coding.manager.diff(coding.task.task_id).content
    assert not marker.exists()
    assert (coding.root / ".git/index").read_bytes() == before


@pytest.mark.posix
@pytest.mark.parametrize("kind", ["root", "parent"])
def test_replacement_during_write_is_detected_before_publication(
    coding, tmp_path, monkeypatch, kind
):
    from agentforge.coding import filesystem

    setup = coding.create()
    write(setup, "nested/file", "old")
    outside = tmp_path / "outside-race"
    outside.mkdir()
    (outside / "file").write_text("outside")
    original = filesystem.os.fsync
    replaced = False

    def race(fd):
        nonlocal replaced
        original(fd)
        if not replaced:
            replaced = True
            target = setup.workspace if kind == "root" else setup.workspace / "nested"
            target.rename(target.with_name(target.name + "-moved"))
            target.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(filesystem.os, "fsync", race)
    with pytest.raises(UnsafeProjectPath):
        write(setup, "nested/file", "new", sha("old"))
    assert (outside / "file").read_text() == "outside"


@pytest.mark.posix
def test_atomic_create_does_not_replace_a_concurrent_file(coding, monkeypatch):
    from agentforge.coding import filesystem

    setup = coding.create()
    publish = filesystem._publish_new

    def race(parent, temporary, name):
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as file:
            file.write(b"concurrent")
        publish(parent, temporary, name)

    monkeypatch.setattr(filesystem, "_publish_new", race)
    with pytest.raises(EditConflict):
        write(setup, "new", "model")
    assert (setup.workspace / "new").read_text() == "concurrent"
    assert coding.manager.get(coding.task.task_id).bytes_written == 0


@pytest.mark.posix
def test_special_git_objects_remain_noneditable(coding):
    (coding.root / "linked").symlink_to("../secret")
    git(coding.root, "add", "linked")
    git(coding.root, "commit", "-m", "link")
    setup = coding.create()
    assert not (setup.workspace / "linked").exists()
    assert git(setup.workspace, "status", "--porcelain") == b""
    with pytest.raises(CodingError):
        write(setup, "linked", "attack")
    with pytest.raises(CodingError):
        write(setup, "linked/child", "attack")


def test_patch_format_rejects_headers_options_rename_and_modes():
    from pydantic import ValidationError

    for extra in (
        {"patch": "--- ../../secret\n+++ ../../secret\n"},
        {"rename_to": "../../secret"},
        {"mode": "120000"},
        {"binary": True},
        {"options": ["--unsafe-paths"]},
    ):
        with pytest.raises(ValidationError):
            PatchArguments.model_validate(
                {
                    "path": "source.py",
                    "expected_sha256": sha("old"),
                    "old_text": "old",
                    "new_text": "new",
                    **extra,
                }
            )


@pytest.mark.posix
def test_committed_executable_mode_is_preserved(coding):
    (coding.root / "script").write_text("#!/bin/sh\nexit 0\n")
    (coding.root / "script").chmod(0o700)
    git(coding.root, "add", "script")
    git(coding.root, "commit", "-m", "script")
    setup = coding.create()
    assert (setup.workspace / "script").stat().st_mode & 0o100
    assert coding.manager.diff(coding.task.task_id).changed_files == ()
    (setup.workspace / "script").chmod(0o600)
    diff = coding.manager.diff(coding.task.task_id)
    assert diff.changed_files == ("script",) and "Metadata-only" in diff.content


def test_workspace_parent_must_be_outside_repository(coding):
    inside = coding.root / "workspaces"
    inside.mkdir(mode=0o700)
    config = coding.config.model_copy(update={"workspace_parent": inside})
    manager = CodingWorkspaceManager(coding.registry, coding.repository, config)
    before = primary_state(coding.root)
    with pytest.raises(CodingError):
        manager.create(coding.task)
    assert primary_state(coding.root) == before


def test_failed_materialization_retains_owned_metadata(coding, monkeypatch):
    from agentforge.coding import service

    monkeypatch.setattr(
        service,
        "mutate",
        lambda *args, **kwargs: (_ for _ in ()).throw(CodingError("failed")),
    )
    with pytest.raises(CodingError):
        coding.create()
    report = coding.manager.get(coding.task.task_id)
    assert report.state == "failed" and not report.inspection_available
    row = coding.manager._record(coding.task.task_id)
    assert "admin" in row["identities"] and "worktree" in row["identities"]
    assert Path(row["worktree_path"]).exists()
    with pytest.raises(CodingError):
        coding.create()
    assert (
        coding.manager.cleanup(coding.task.task_id, coding.task.task_id).state
        == "removed"
    )


@pytest.mark.posix
def test_cleanup_registration_pointer_mismatch(coding, tmp_path):
    setup = coding.create()
    coding.task.state = "failed"
    coding.manager.finalize(coding.task)
    admin = (
        Path(setup.row["repository_path"])
        / ".git/worktrees"
        / setup.row["identities"]["admin_name"]
    )
    (admin / "gitdir").write_text(str(tmp_path / "another-task/.git"))
    with pytest.raises(CodingError):
        coding.manager.cleanup(coding.task.task_id, coding.task.task_id)
    assert setup.workspace.exists()
