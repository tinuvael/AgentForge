"""Synthetic projects only: source budgets, races, offline Git and subprojects."""

import io
import os
import shutil
import stat
import subprocess
from dataclasses import replace
from uuid import uuid4

import pytest

from agentforge.db.database import create_session_factory
from agentforge.db.projects import ProjectRepository
from agentforge.projects.errors import (
    InvalidProjectPath,
    ProjectNotFound,
    UnsafeProjectPath,
)
from agentforge.projects.service import ProjectRegistry
from agentforge.tools import filesystem, git_backend
from agentforge.tools.errors import (
    GitTimeout,
    GitUnavailable,
    InvalidToolArgument,
    NotGitRepository,
    PathNotFound,
    RepositoryIOError,
    SensitivePath,
    UnsupportedTextFile,
)
from agentforge.tools.policy import FILE_SCAN_BYTES, HARD_OUTPUT_BYTES
from agentforge.tools.service import RepositoryTools


def write(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


@pytest.fixture
def tools(registry, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    project = registry.register_project("Synthetic", root)
    return RepositoryTools(registry), project.id, root


@pytest.fixture
def local_git():
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Synthetic Git tests require the local Git executable")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def run(root, *args):
        return subprocess.run(
            [
                executable,
                "-c",
                "user.name=Tool Tests",
                "-c",
                "user.email=tests@example.invalid",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-C",
                str(root),
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()

    return run


@pytest.fixture
def git_tools(tools, local_git):
    service, project, root = tools
    local_git(root, "init", "--initial-branch=trunk")
    write(root, "source.py", "first needle\nsecond Needle\n")
    local_git(root, "add", ".")
    local_git(root, "commit", "-m", "Initial")
    return service, project, root


def test_list_order_scope_and_limits(tools):
    service, project, root = tools
    for name in ["z.py", "src/b.py", "src/a.py", "a.txt", "a/z.py", "a.py"]:
        write(root, name, "source\n")
    expected = ("a.py", "a.txt", "a/z.py", "src/a.py", "src/b.py", "z.py")
    assert service.list_files(project).paths == expected
    assert not service.list_files(project).truncated
    assert service.list_files(project, "src").paths == ("src/a.py", "src/b.py")
    assert service.list_files(project, max_results=2).paths == expected[:2]
    assert service.list_files(project, max_results=2).truncated
    assert not service.list_files(project, max_results=6).truncated
    bounded = service.list_files(project, max_bytes=5)
    assert bounded.paths == ("a.py",)
    assert bounded.truncated


def test_complete_read_line_range_empty_and_utf8_budget(tools):
    service, project, root = tools
    text = "one\nдва\nthree\n"
    write(root, "source.py", text)
    result = service.read_file(project, "source.py")
    assert result.content == text
    assert (result.path, result.start_line, result.end_line) == ("source.py", 1, 3)
    assert result.requested_start_line == 1 and result.requested_end_line is None
    assert not result.truncated
    result = service.read_file(project, "source.py", start_line=2, end_line=2)
    assert result.content == "два\n" and result.start_line == result.end_line == 2
    assert not result.truncated
    assert service.read_file(project, "source.py", max_lines=1).truncated
    bounded = service.read_file(project, "source.py", start_line=2, max_bytes=3)
    assert bounded.content == "д" and bounded.truncated
    empty = service.read_file(project, "source.py", start_line=99)
    assert empty.content == "" and empty.start_line is empty.end_line is None
    assert not empty.truncated
    write(root, "empty.txt", "")
    assert service.read_file(project, "empty.txt").content == ""


def test_huge_file_scanning_is_bounded(tools):
    service, project, root = tools
    (root / "huge.txt").write_bytes(b"a\n" * FILE_SCAN_BYTES)
    result = service.read_file(project, "huge.txt", start_line=1, end_line=2)
    assert result.content == "a\na\n" and not result.truncated
    assert service.read_file(project, "huge.txt").truncated
    assert service.search_code(project, "missing").truncated


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_line": 0},
        {"start_line": -1},
        {"start_line": True},
        {"start_line": 4, "end_line": 3},
        {"end_line": 0},
        {"end_line": "3"},
        {"max_lines": 0},
        {"max_bytes": HARD_OUTPUT_BYTES + 1},
    ],
)
def test_invalid_read_arguments(tools, kwargs):
    service, project, root = tools
    write(root, "file.txt", "text")
    with pytest.raises(InvalidToolArgument):
        service.read_file(project, "file.txt", **kwargs)


@pytest.mark.parametrize("data", [b"hello\0world", b"\xff\xfe\x00a", b"\x1b[31mtext"])
def test_non_text_rejected_and_search_skips(tools, data):
    service, project, root = tools
    (root / "binary.dat").write_bytes(data)
    with pytest.raises(UnsupportedTextFile):
        service.read_file(project, "binary.dat")
    result = service.search_code(project, "hello")
    assert result.matches == () and result.skipped_binary_files == 1


def test_missing_directory_and_special_file(tools):
    service, project, root = tools
    with pytest.raises(PathNotFound):
        service.read_file(project, "absent.txt")
    with pytest.raises(PathNotFound):
        service.list_files(project, "absent")
    (root / "dir").mkdir()
    with pytest.raises(InvalidToolArgument):
        service.read_file(project, "dir")
    os.mkfifo(root / "fifo")
    with pytest.raises(UnsafeProjectPath):
        service.read_file(project, "fifo")
    assert service.list_files(project).paths == ()


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "sub/../../outside",
        "/etc/passwd",
        "C:\\outside",
        "sub/../file.txt",
        ":(top)secret",
    ],
)
def test_untrusted_paths_rejected_by_all_tools(tools, path):
    service, project, _ = tools
    calls = [
        lambda: service.read_file(project, path),
        lambda: service.list_files(project, path),
        lambda: service.search_code(project, "needle", path=path),
        lambda: service.git_grep(project, "needle", path=path),
        lambda: service.git_status(project, path=path),
        lambda: service.git_diff(project, path=path),
    ]
    for call in calls:
        with pytest.raises(UnsafeProjectPath):
            call()


def test_common_prefix_sibling_and_symlinks(tools, tmp_path):
    service, project, root = tools
    sibling = tmp_path / "project-private"
    outside = write(sibling, "secret.txt", "OUTSIDE SECRET")
    write(root, "real.txt", "safe")
    (root / "escape.txt").symlink_to(outside)
    (root / "escape-dir").symlink_to(sibling, target_is_directory=True)
    (root / "inside.txt").symlink_to(root / "real.txt")
    for name in [
        str(outside),
        "../project-private/secret.txt",
        "escape.txt",
        "escape-dir/secret.txt",
        "inside.txt",
    ]:
        with pytest.raises(UnsafeProjectPath):
            service.read_file(project, name)
    assert service.list_files(project).paths == ("real.txt",)
    assert service.search_code(project, "SECRET").matches == ()


def test_file_symlink_swap_between_check_and_open(tools, tmp_path, monkeypatch):
    service, project, root = tools
    file = write(root, "race.txt", "safe")
    outside = write(tmp_path, "secret.txt", "OUTSIDE SECRET")
    original = os.open

    def racing_open(path, *args, **kwargs):
        if path == "race.txt":
            file.unlink()
            file.symlink_to(outside)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {racing_open})
    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises((UnsafeProjectPath, RepositoryIOError)):
        service.read_file(project, "race.txt")


def test_directory_replacement_on_early_search_exit(tools, tmp_path, monkeypatch):
    service, project, root = tools
    write(root, "sub/a.txt", "needle\nneedle\n")
    original = filesystem.read_bytes

    def racing_read(*args, **kwargs):
        result = original(*args, **kwargs)
        (root / "sub").rename(tmp_path / "moved")
        (root / "sub").mkdir()
        return result

    monkeypatch.setattr(filesystem, "read_bytes", racing_read)
    with pytest.raises(UnsafeProjectPath):
        service.search_code(project, "needle", max_results=1)


@pytest.mark.parametrize("symlink", [False, True])
def test_root_replacement_after_registration(tools, tmp_path, symlink):
    service, project, root = tools
    write(root, "safe.txt", "safe")
    root.rename(tmp_path / "old-root")
    if symlink:
        root.symlink_to(tmp_path / "old-root", target_is_directory=True)
    else:
        root.mkdir()
        write(root, "safe.txt", "replacement")
    for call in [
        lambda: service.list_files(project),
        lambda: service.read_file(project, "safe.txt"),
        lambda: service.search_code(project, "safe"),
        lambda: service.git_status(project),
        lambda: service.git_grep(project, "safe"),
        lambda: service.git_diff(project),
    ]:
        with pytest.raises((InvalidProjectPath, UnsafeProjectPath)):
            call()


def test_root_identity_persisted_and_legacy_fails_closed(registry, database, tmp_path):
    root = tmp_path / "persisted"
    root.mkdir()
    project = registry.register_project("Persisted", root)
    repository = ProjectRepository(create_session_factory(database[0]))
    reopened = ProjectRegistry(repository, base_directory=tmp_path)
    assert reopened.get_project(project.id).root_inode == root.stat().st_ino
    root.rename(tmp_path / "moved")
    root.mkdir()
    with pytest.raises(UnsafeProjectPath):
        RepositoryTools(reopened).list_files(project.id)
    legacy = replace(
        project,
        id=uuid4(),
        root_path=tmp_path / "legacy",
        root_device=None,
        root_inode=None,
    )
    legacy.root_path.mkdir()
    repository.add(legacy)
    with pytest.raises(UnsafeProjectPath):
        RepositoryTools(reopened).list_files(legacy.id)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.example",
        ".env.local",
        ".secrets/a.txt",
        ".ssh/id_rsa",
        ".aws/credentials",
        "server.pem",
        "private.key",
        "keys.p12",
        "cert.pfx",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        ".git/config",
    ],
)
def test_sensitive_files_denied_everywhere(tools, path):
    service, project, root = tools
    write(root, path, "PRIVATE CONTENT")
    assert service.list_files(project).paths == ()
    assert service.search_code(project, "PRIVATE").matches == ()
    for call in [
        lambda: service.read_file(project, path),
        lambda: service.git_diff(project, path=path),
        lambda: service.git_grep(project, "PRIVATE", path=path),
        lambda: service.git_status(project, path=path),
    ]:
        with pytest.raises(SensitivePath) as error:
            call()
        assert "PRIVATE CONTENT" not in str(error.value)


def test_generated_excluded_but_explicit_safe_read_allowed(tools):
    service, project, root = tools
    for directory in ["node_modules", "build", ".venv", "vendor", "thing.egg-info"]:
        write(root, f"{directory}/source.py", "needle")
    assert service.list_files(project).paths == ()
    assert service.list_files(project, "build").paths == ()
    assert service.search_code(project, "needle").matches == ()
    assert service.read_file(project, "build/source.py").content == "needle"


def test_literal_search_case_scope_order_and_budgets(tools):
    service, project, root = tools
    write(root, "b.py", "NEEDLE\nneedle\n")
    write(root, "src/a.py", "needle .*\nneedle\n")
    write(root, "a.txt", "needle\n")
    result = service.search_code(project, "needle")
    assert [(m.path, m.line_number) for m in result.matches] == [
        ("a.txt", 1),
        ("b.py", 1),
        ("b.py", 2),
        ("src/a.py", 1),
        ("src/a.py", 2),
    ]
    assert not result.truncated
    assert len(service.search_code(project, "needle", case_sensitive=True).matches) == 4
    assert len(service.search_code(project, ".*").matches) == 1
    assert (
        len(service.search_code(project, "needle", path="src", glob="*.py").matches)
        == 2
    )
    bounded = service.search_code(project, "needle", max_results=1)
    assert len(bounded.matches) == 1 and bounded.truncated
    assert service.search_code(project, "needle", max_bytes=1).truncated
    assert service.search_code(project, "needle", max_line_bytes=2).matches[0].truncated


@pytest.mark.parametrize(
    "method",
    ["list_files", "read_file", "search_code", "git_grep", "git_status", "git_diff"],
)
def test_every_tool_requires_registered_project(tools, method):
    service, _, _ = tools
    args = (
        ["file.txt"]
        if method == "read_file"
        else (["needle"] if method in {"search_code", "git_grep"} else [])
    )
    with pytest.raises(ProjectNotFound):
        getattr(service, method)("not-a-project", *args)


@pytest.mark.parametrize("method", ["git_grep", "git_status", "git_diff"])
def test_non_git_behavior(tools, method):
    service, project, _ = tools
    with pytest.raises(NotGitRepository):
        getattr(service, method)(project, *(["needle"] if method == "git_grep" else []))


def test_git_grep_is_cached_literal_and_bounded(git_tools, local_git):
    service, project, root = git_tools
    result = service.git_grep(project, "needle")
    assert [(m.path, m.line_number) for m in result.matches] == [
        ("source.py", 1),
        ("source.py", 2),
    ]
    assert len(service.git_grep(project, "needle", case_sensitive=True).matches) == 1
    write(root, "source.py", "worktree only")
    assert len(service.git_grep(project, "needle").matches) == 2
    assert service.git_grep(project, "only").matches == ()
    assert service.git_grep(project, "needle", max_results=1).truncated
    assert service.git_grep(project, "needle", max_bytes=5).truncated
    assert service.git_grep(project, "needle", max_line_bytes=2).matches[0].truncated
    write(root, "literal[1].txt", "needle\n")
    write(root, ".env", "needle SECRET")
    write(root, "vendor/a.txt", "needle VENDOR")
    (root / "binary").write_bytes(b"needle\x00secret")
    (root / "alias").symlink_to(root / "source.py")
    local_git(root, "add", ".")
    assert (
        service.git_grep(project, "needle", path="literal[1].txt").matches[0].path
        == "literal[1].txt"
    )
    matches = service.git_grep(project, "needle").matches
    assert {m.path for m in matches} == {"literal[1].txt"}


def test_git_status_clean_changes_detached_and_unborn(
    git_tools, local_git, registry, tmp_path
):
    service, project, root = git_tools
    clean = service.git_status(project)
    assert clean.branch == "trunk" and not clean.detached and clean.changes == ()
    write(root, "source.py", "staged\n")
    local_git(root, "add", "source.py")
    write(root, "source.py", "unstaged\n")
    write(root, "new.py", "untracked")
    changes = {c.path: c for c in service.git_status(project).changes}
    assert changes["source.py"].staged and changes["source.py"].unstaged
    assert changes["new.py"].untracked and not changes["new.py"].staged
    assert not changes["source.py"].conflicted
    assert service.git_status(project, max_results=1).truncated
    assert service.git_status(project, max_bytes=1).truncated
    local_git(root, "reset", "--hard", "HEAD")
    local_git(root, "checkout", "--detach", "HEAD")
    assert service.git_status(project).detached
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    local_git(unborn, "init", "--initial-branch=empty")
    registered = registry.register_project("Unborn", unborn)
    assert service.git_status(registered.id).branch == "empty"


def test_git_diffs_staged_unstaged_deleted_binary_and_limits(git_tools, local_git):
    service, project, root = git_tools
    write(root, "source.py", "new staged\n")
    local_git(root, "add", "source.py")
    write(root, "source.py", "new unstaged\n")
    unstaged = service.git_diff(project)
    assert "+new unstaged" in unstaged.content and "-new staged" in unstaged.content
    staged = service.git_diff(project, staged=True)
    assert "+new staged" in staged.content and "new unstaged" not in staged.content
    assert service.git_diff(project, max_bytes=10).truncated
    assert service.git_diff(project, staged=True, max_bytes=10).truncated
    assert len(service.git_diff(project, max_bytes=10).content.encode()) <= 10
    assert service.git_diff(project, path="absent").content == ""
    (root / "source.py").unlink()
    assert "-new staged" in service.git_diff(project).content
    local_git(root, "add", "source.py")
    assert "deleted file mode" in service.git_diff(project, staged=True).content
    (root / "binary").write_bytes(b"one\x00two")
    local_git(root, "add", "binary")
    local_git(root, "commit", "-m", "Binary fixture")
    (root / "binary").write_bytes(b"one\x00changed")
    assert "Binary/non-text file differs: binary" in service.git_diff(project).content


def test_git_subproject_never_exposes_siblings(registry, tmp_path, local_git):
    root = tmp_path / "repo"
    root.mkdir()
    local_git(root, "init", "--initial-branch=trunk")
    write(root, "allowed/file.txt", "needle allowed original\n")
    write(root, "secret/private.txt", "needle OUTSIDE SECRET original\n")
    local_git(root, "add", ".")
    local_git(root, "commit", "-m", "Initial")
    project = registry.register_project("Subproject", root / "allowed")
    service = RepositoryTools(registry)
    assert service.git_grep(project.id, "needle").matches[0].path == "file.txt"
    assert service.git_grep(project.id, "OUTSIDE").matches == ()
    for name in ["allowed/file.txt", "secret/private.txt"]:
        write(root, name, "needle staged " + name + "\n")
    local_git(root, "add", ".")
    for name in ["allowed/file.txt", "secret/private.txt"]:
        write(root, name, "needle unstaged " + name + "\n")
    write(root, "secret/untracked.txt", "OUTSIDE SECRET")
    write(root, "allowed/untracked.txt", "allowed")
    assert {c.path for c in service.git_status(project.id).changes} == {
        "file.txt",
        "untracked.txt",
    }
    for staged in [False, True]:
        diff = service.git_diff(project.id, staged=staged)
        assert "file.txt" in diff.content
        assert "secret/" not in diff.content and "OUTSIDE SECRET" not in diff.content
        assert "allowed/file.txt" not in diff.content.splitlines()[0]
    assert service.search_code(project.id, "OUTSIDE").matches == ()
    for path in [
        "../secret/private.txt",
        ":(top)secret",
        str(root / "secret/private.txt"),
    ]:
        with pytest.raises(UnsafeProjectPath):
            service.git_grep(project.id, "needle", path=path)


def test_git_failures_are_safe_and_timeout_translated(git_tools, monkeypatch):
    service, project, _ = git_tools

    def missing(*args, **kwargs):
        raise FileNotFoundError("DO NOT EXPOSE backend paths")

    monkeypatch.setattr(git_backend.subprocess, "Popen", missing)
    with pytest.raises(GitUnavailable) as error:
        service.git_status(project)
    assert "DO NOT EXPOSE" not in str(error.value)

    class TimedOut:
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = -9
        waited = False
        pid = 12345

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self, timeout=None):
            if not self.waited:
                self.waited = True
                raise subprocess.TimeoutExpired("git", timeout)
            return -9

        def kill(self):
            pass

    monkeypatch.setattr(git_backend.subprocess, "Popen", lambda *a, **k: TimedOut())
    monkeypatch.setattr(git_backend.os, "killpg", lambda *a: None)
    with pytest.raises(GitTimeout):
        service.git_status(project)


def test_git_environment_no_shell_no_mutation_and_no_drivers(
    git_tools, local_git, monkeypatch
):
    service, project, root = git_tools
    write(root, "source.py", "needle changed\n")
    # These configured commands would fail the test if Git executed them.
    local_git(root, "config", "core.fsmonitor", "invalid-fsmonitor-command")
    local_git(root, "config", "diff.external", "invalid-diff-command")
    local_git(root, "config", "filter.hostile.clean", "invalid-clean-command")
    local_git(root, "config", "filter.hostile.smudge", "invalid-smudge-command")
    local_git(root, "config", "filter.hostile.process", "invalid-process-command")
    local_git(root, "config", "filter.hostile.required", "true")
    write(root, ".gitattributes", "source.py filter=hostile diff=hostile\n")
    local_git(root, "config", "diff.hostile.textconv", "invalid-textconv-command")
    monkeypatch.setenv("GIT_DIR", "/outside")
    monkeypatch.setenv("GIT_WORK_TREE", "/outside")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "100")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "invalid-command")
    original = subprocess.Popen
    observed = []
    before = {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }

    def checked(args, **kwargs):
        assert kwargs["shell"] is False and isinstance(args, list)
        assert kwargs["pass_fds"]
        env = kwargs["env"]
        assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
        assert "GIT_CONFIG_COUNT" not in env and "GIT_EXTERNAL_DIFF" not in env
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_NO_LAZY_FETCH"] == "1"
        assert env["GIT_ALLOW_PROTOCOL"] == ""
        assert "core.fsmonitor=false" in args
        assert "--literal-pathspecs" in args
        observed.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", checked)
    service.git_status(project)
    service.git_grep(project, "needle")
    service.git_diff(project)
    service.git_diff(project, staged=True)
    after = {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    assert after == before
    assert observed


def test_git_backend_failure_does_not_leak_stderr(git_tools, monkeypatch):
    from agentforge.tools.errors import GitFailure

    service, project, _ = git_tools

    class Failed:
        stdout = io.BytesIO()
        stderr = io.BytesIO(b"fatal: PRIVATE backend diagnostic")
        returncode = 128

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Failed())
    with pytest.raises(GitFailure) as error:
        service.git_status(project)
    assert "PRIVATE" not in str(error.value)


def test_git_symlink_read_and_root_race_fail_closed(git_tools, tmp_path, monkeypatch):
    service, project, root = git_tools
    file = root / "source.py"
    file.unlink()
    file.symlink_to(write(tmp_path, "outside.txt", "OUTSIDE SECRET"))
    with pytest.raises(UnsafeProjectPath):
        service.git_diff(project)
    file.unlink()
    write(root, "source.py", "safe\n")
    original = git_backend._Git.status

    def racing_status(*args, **kwargs):
        result = original(*args, **kwargs)
        root.rename(tmp_path / "moved")
        root.mkdir()
        return result

    monkeypatch.setattr(git_backend._Git, "status", racing_status)
    with pytest.raises(UnsafeProjectPath):
        service.git_status(project)


def test_no_newline_diff_marker(git_tools):
    service, project, root = git_tools
    write(root, "source.py", "new without newline")
    diff = service.git_diff(project).content
    assert "+new without newline\n\\ No newline at end of file\n" in diff


def test_conflicts_are_structured_and_diff_reports_omission(git_tools, local_git):
    service, project, root = git_tools
    local_git(root, "checkout", "-b", "other")
    write(root, "source.py", "other branch\n")
    local_git(root, "add", "source.py")
    local_git(root, "commit", "-m", "Other")
    local_git(root, "checkout", "trunk")
    write(root, "source.py", "trunk branch\n")
    local_git(root, "add", "source.py")
    local_git(root, "commit", "-m", "Trunk")
    with pytest.raises(subprocess.CalledProcessError):
        local_git(root, "merge", "other")
    assert service.git_status(project).changes[0].conflicted
    assert service.git_diff(project).truncated


def test_metadata_only_diff_is_visible(git_tools):
    service, project, root = git_tools
    (root / "source.py").chmod(0o755)
    assert "Metadata-only change: source.py" in service.git_diff(project).content


def test_git_query_is_data_and_file_paths_are_literal(git_tools, local_git):
    service, project, root = git_tools
    write(root, "-option.txt", "--help $(not-a-command)\n")
    local_git(root, "add", "--", "-option.txt")
    for query in ["--help", "$(not-a-command)"]:
        assert (
            service.git_grep(project, query, path="-option.txt").matches[0].path
            == "-option.txt"
        )


def test_filesystem_read_detects_in_place_edits(tools, monkeypatch):
    service, project, root = tools
    target = write(root, "source.py", "old\n")
    original = os.fstat
    calls = 0

    def racing_fstat(fd):
        nonlocal calls
        result = original(fd)
        if stat.S_ISREG(result.st_mode):
            calls += 1
            if calls == 2:
                target.write_text("new content\n")
                return original(fd)
        return result

    monkeypatch.setattr(os, "fstat", racing_fstat)
    with pytest.raises(UnsafeProjectPath):
        service.read_file(project, "source.py")


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("list_files", {"max_results": 0}),
        ("list_files", {"max_results": 10_001}),
        ("search_code", {"case_sensitive": "yes"}),
        ("search_code", {"max_results": 1001}),
        ("search_code", {"glob": 123}),
        ("git_grep", {"max_line_bytes": 2001}),
        ("git_status", {"max_bytes": HARD_OUTPUT_BYTES + 1}),
        ("git_diff", {"staged": 1}),
    ],
)
def test_other_argument_limits(tools, method, kwargs):
    service, project, _ = tools
    args = ["needle"] if method in {"search_code", "git_grep"} else []
    with pytest.raises(InvalidToolArgument):
        getattr(service, method)(project, *args, **kwargs)


def test_git_sensitivity_and_rename_across_boundary(registry, tmp_path, local_git):
    root = tmp_path / "repo"
    root.mkdir()
    local_git(root, "init", "--initial-branch=trunk")
    write(root, "secret/outside.txt", "OUTSIDE SECRET\n")
    write(root, "allowed/file.txt", "allowed\n")
    write(root, "allowed/.env", "PRIVATE ENV\n")
    local_git(root, "add", ".")
    local_git(root, "commit", "-m", "Initial")
    local_git(root, "mv", "allowed/file.txt", "secret/moved.txt")
    write(root, "allowed/.env", "PRIVATE CHANGED\n")
    local_git(root, "add", ".")
    project = registry.register_project("Subproject", root / "allowed")
    service = RepositoryTools(registry)
    assert [c.path for c in service.git_status(project.id).changes] == ["file.txt"]
    diff = service.git_diff(project.id, staged=True).content
    assert "-allowed" in diff and "secret/" not in diff and "PRIVATE" not in diff


def test_unsupported_descriptor_platform_fails_closed(tools, monkeypatch):
    service, project, _ = tools
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(UnsafeProjectPath):
        service.list_files(project)


@pytest.mark.parametrize("query", ["", "\n", "\x00", "a" * 1025, "\udcff", 123])
def test_invalid_search_query(tools, query):
    service, project, _ = tools
    with pytest.raises(InvalidToolArgument):
        service.search_code(project, query)
    with pytest.raises(InvalidToolArgument):
        service.git_grep(project, query)
