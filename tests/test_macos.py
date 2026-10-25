"""Native APFS acceptance, never emulated or substituted with mock locking."""

import asyncio
import os
import sqlite3
import subprocess
import sys
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import text

from agentforge.db.ownership import DatabaseOwnership, ExecutorOwnershipError
from agentforge.projects import filesystem as fs
from agentforge.projects.darwin import descriptor_path, local_apfs
from agentforge.projects.errors import ProjectError, UnsafeProjectPath
from agentforge.tools.service import RepositoryTools
from tests.test_coding_validation import run_validation
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_coding_workspaces import git, primary_state
from tests.test_executor_ownership import peer, reject, start

pytestmark = pytest.mark.macos
coding = coding_fixture
HELPER = Path(__file__).with_name("helpers") / "filesystem_process.py"


@contextmanager
def replaced(target, outside, *, directory=False):
    process = subprocess.Popen(
        [
            sys.executable,
            str(HELPER),
            "directory" if directory else "file",
            str(target),
            str(outside),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Use the existing bounded pipe reader rather than an unbounded readline.
    import queue
    import threading

    messages = queue.Queue()
    reader = threading.Thread(target=lambda: messages.put(process.stdout.readline()))
    reader.start()
    try:
        assert '"replaced"' in messages.get(timeout=10)
        yield
    finally:
        if process.poll() is None:
            process.stdin.write("\n")
            process.stdin.flush()
        try:
            assert process.wait(timeout=10) == 0, process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def test_native_apfs_descriptor_identity(tmp_path):
    with fs.anchored_root(tmp_path) as fd:
        assert local_apfs(fd)
        assert descriptor_path(fd) == str(tmp_path)
        assert os.fstat(fd).st_ino == tmp_path.stat().st_ino


def test_dirty_swift_git_and_python_index(coding):
    from agentforge.db.index import IndexRepository
    from agentforge.index.service import ProjectIndex

    root = coding.root
    (root / "App.swift").write_text("struct Original {}\n")
    git(root, "add", "App.swift")
    git(root, "commit", "-m", "Swift baseline")
    (root / "App.swift").write_text("struct DirtyMacSource {}\n")
    tools = RepositoryTools(coding.manager.projects)
    project_id = coding.project.id
    before = primary_state(root)
    assert "DirtyMacSource" in tools.read_file(project_id, "App.swift").content
    assert tools.search_code(project_id, "DirtyMacSource").matches
    assert "App.swift" in str(tools.git_status(project_id))
    assert "+struct DirtyMacSource" in tools.git_diff(project_id).content
    index = ProjectIndex(
        coding.manager.projects, IndexRepository(coding.manager.repository.sessions)
    )
    assert index.refresh_index(project_id).file_count >= 1
    assert primary_state(root) == before


def test_hardlinks_fail_closed_for_mac_repository(registry, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.py").write_text("private alias\n")
    os.link(root / "source.py", tmp_path / "outside.py")
    project = registry.register_project("Mac", root)
    with pytest.raises(UnsafeProjectPath):
        RepositoryTools(registry).read_file(project.id, "source.py")


def test_native_index_scan_obeys_sensitive_directory_policy(registry, tmp_path):
    from agentforge.index.scanner import scan_python_files

    (tmp_path / ".SSH").mkdir()
    (tmp_path / ".SSH" / "private.py").write_text("PRIVATE = 1\n")
    (tmp_path / "public.py").write_text("PUBLIC = 1\n")
    project = registry.register_project("Mac", tmp_path)
    with registry.open_root(project.id) as (_, fd):
        assert list(scan_python_files(fd)) == [("public.py", b"PUBLIC = 1\n")]


def test_actual_case_and_unicode_spelling(registry, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "Source.swift").write_text("original\n")
    (root / "caf\u00e9.swift").write_text("unicode\n")
    project = registry.register_project("Mac", root)
    tools = RepositoryTools(registry)
    actual = next(p.name for p in root.iterdir() if p.name.startswith("caf"))
    assert tools.read_file(project.id, actual).content == "unicode\n"
    alias = unicodedata.normalize(
        "NFD" if unicodedata.is_normalized("NFC", actual) else "NFC", actual
    )
    assert alias != actual
    with pytest.raises(UnsafeProjectPath):
        tools.read_file(project.id, alias)
    if (root / "source.swift").exists():
        with pytest.raises(UnsafeProjectPath):
            tools.read_file(project.id, "source.swift")
    else:
        (root / "source.swift").write_text("distinct\n")
        assert tools.read_file(project.id, "source.swift").content == "distinct\n"
    assert tools.read_file(project.id, "Source.swift").content == "original\n"


def test_independent_root_swap_never_reopens_outside(registry, tmp_path):
    root, outside = tmp_path / "repo", tmp_path / "outside"
    for directory, content in ((root, "authorized"), (outside, "OUTSIDE_SECRET")):
        directory.mkdir()
        (directory / "source.py").write_text(content)
    project = registry.register_project("Mac", root)
    with pytest.raises(ProjectError):
        with registry.open_root(project.id) as (_, fd):
            with replaced(root, outside, directory=True):
                with fs.regular_file(fd, "source.py") as file:
                    assert file.read() == b"authorized"
                # Check root identity while the independent replacement is live.
                with registry.open_root(project.id):
                    pytest.fail("Replacement must not be authorized")


def test_independent_file_retarget_is_detected(registry, tmp_path):
    (tmp_path / "source.py").write_text("authorized")
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE_SECRET")
    project = registry.register_project("Mac", tmp_path)
    with registry.open_root(project.id) as (_, fd):
        with replaced(tmp_path / "source.py", outside):
            with pytest.raises(UnsafeProjectPath):
                RepositoryTools(registry).read_file(project.id, "source.py")
        opened = fs.regular_file(fd, "source.py")
        file = opened.__enter__()
        try:
            with replaced(tmp_path / "source.py", outside):
                assert file.read() == b"authorized"
                with pytest.raises(UnsafeProjectPath):
                    opened.__exit__(None, None, None)
        finally:
            file.close()


def test_git_child_uses_pinned_directory_during_independent_root_swap(coding, tmp_path):
    from agentforge.projects.backends import POSIX
    from agentforge.projects.git import git_environment
    from agentforge.tools.git_backend import _run_git

    outside = tmp_path / "outside"
    outside.mkdir()
    expected = git(coding.root, "rev-parse", "HEAD")
    with coding.manager.projects.open_root(coding.project.id) as (_, fd):
        location = POSIX.git_location(coding.root, fd)
        with replaced(coding.root, outside, directory=True):
            result = _run_git(
                ["git", "-C", ".", "rev-parse", "HEAD"],
                location,
                128,
                environment=git_environment(),
            )
            assert result.data == expected


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_ofd_owner_preserves_sqlite_transaction_exclusion(database, journal):
    engine, url = database
    owner = DatabaseOwnership(engine)
    owner.acquire()
    try:
        connection = sqlite3.connect(url.database, timeout=0)
        try:
            assert (
                connection.execute("PRAGMA journal_mode=" + journal).fetchone()[0]
                == journal
            )
            connection.execute("CREATE TABLE darwin_probe (value INTEGER)")
            connection.commit()
            connection.execute("INSERT INTO darwin_probe VALUES (1)")
            code = (
                "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); "
                "c.execute('INSERT INTO darwin_probe VALUES (2)'); c.commit()"
            )
            blocked = subprocess.run(
                [sys.executable, "-c", code, url.database],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert blocked.returncode != 0 and "database is locked" in blocked.stderr
            reject(url)
            connection.commit()
            accepted = subprocess.run(
                [sys.executable, "-c", code, url.database],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert accepted.returncode == 0, accepted.stderr
        finally:
            connection.close()
        with engine.connect() as current:
            assert (
                current.execute(text("SELECT count(*) FROM darwin_probe")).scalar() == 2
            )
    finally:
        owner.release()


def test_native_case_alias_database_has_one_owner(database):
    path = Path(database[1].database)
    alias = path.with_name(path.name.upper())
    if not alias.exists():
        pytest.skip("Case-sensitive APFS: distinct filenames are distinct databases")
    with peer(database[1]) as owner:
        start(owner)
        reject("sqlite:///" + str(alias))
        owner.send()
        owner.receive("closed")
        owner.finish()


def test_system_toolchain_hardlinks_are_trusted_only_as_executables(coding):
    from agentforge.coding.config import ValidationCommand
    from agentforge.coding.inspection import check_coding_host

    coding.manager.config = coding.manager.config.model_copy(
        update={
            "validations": {
                "git_version": ValidationCommand(argv=("/usr/bin/git", "--version"))
            }
        }
    )
    check_coding_host(coding.manager)
    before = primary_state(coding.root)
    setup = coding.create()
    result = asyncio.run(run_validation(setup, "git_version"))
    assert result["exit_code"] == 0 and "git version" in result["stdout"]
    assert primary_state(coding.root) == before


def test_same_process_renamed_alias_does_not_drop_sqlite_locks(database):
    from agentforge.db.database import create_database_engine

    engine, url = database
    path = Path(url.database)
    moved = path.with_name("renamed.sqlite")
    owner = DatabaseOwnership(engine)
    owner.acquire()
    connection = sqlite3.connect(path, timeout=0)
    try:
        connection.execute("CREATE TABLE alias_probe (value INTEGER)")
        connection.commit()
        connection.execute("INSERT INTO alias_probe VALUES (1)")
        path.rename(moved)
        contender = create_database_engine("sqlite:///" + str(moved))
        try:
            with pytest.raises(ExecutorOwnershipError, match="already owns"):
                DatabaseOwnership(contender).acquire()
            code = (
                "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); "
                "c.execute('INSERT INTO alias_probe VALUES (2)'); c.commit()"
            )
            blocked = subprocess.run(
                [sys.executable, "-c", code, str(moved)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert blocked.returncode != 0 and "database is locked" in blocked.stderr
        finally:
            contender.dispose()
            moved.rename(path)
        connection.commit()
    finally:
        connection.close()
        owner.release()
