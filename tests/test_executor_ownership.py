"""Kernel-lock tests run in independent interpreters on native Linux and Windows.

No Windows lock simulation, live Provider, network, GPU or workstation required.
"""

import json
import os
import queue
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select, text

from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.models import (
    CodingWorkspaceRecord,
    CouncilParticipantRecord,
    CouncilRecord,
    TaskRecord,
)
from agentforge.db.ownership import (
    BUSY_MESSAGE,
    UNSAFE_MESSAGE,
    DatabaseOwnership,
    ExecutorOwnershipError,
)

HELPER = Path(__file__).with_name("helpers") / "executor_process.py"


class Peer:
    def __init__(self, url, mode="hold", *, cwd=None):
        self.process = subprocess.Popen(
            [sys.executable, str(HELPER), str(url), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
        self.messages = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.messages.put(json.loads(line))
        self.messages.put({"event": "eof"})

    def receive(self, event):
        result = self.messages.get(timeout=15)
        assert result["event"] == event, (
            result,
            self.process.poll(),
            self.process.stderr.read() if result["event"] == "eof" else "",
        )
        return result

    def send(self, **message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def finish(self):
        assert self.process.wait(timeout=15) == 0, self.process.stderr.read()
        assert self.process.stderr.read() == ""

    def cleanup(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


@contextmanager
def peer(url, mode="hold", **kwargs):
    process = Peer(url, mode, **kwargs)
    try:
        process.receive("ready")
        yield process
    finally:
        process.cleanup()


def start(process):
    process.send()
    return process.receive("owned")


def reject(url, **kwargs):
    with peer(url, **kwargs) as process:
        process.send()
        assert process.receive("rejected")["message"] == BUSY_MESSAGE
        process.finish()


def durable(database):
    with create_session_factory(database)() as session:
        return [
            [dict(row._mapping) for row in session.execute(select(model.__table__))]
            for model in (
                TaskRecord,
                CouncilRecord,
                CouncilParticipantRecord,
                CodingWorkspaceRecord,
            )
        ]


def test_inflight_process_rejects_second_without_task_council_coding_mutation(
    database, registry, tmp_path
):
    root = tmp_path / "project"
    root.mkdir()
    project = registry.register_project("Offline", root)
    with peer(database[1], "active") as owner:
        start(owner)
        owner.send(project_id=str(project.id))
        owner.receive("running")
        before = durable(database[0])
        reject(database[1])
        assert durable(database[0]) == before
        owner.send()
        completed = owner.receive("completed")
        assert completed["states"] == ["completed", "completed"]
        assert completed["answer"] == "Scripted final answer"
        owner.receive("closed")
        owner.finish()
    with peer(database[1]) as restarted:
        start(restarted)
        restarted.send()
        restarted.receive("closed")
        restarted.finish()


def test_simultaneous_cold_starts_have_exactly_one_owner(tmp_path):
    url = "sqlite:///" + (tmp_path / "cold.sqlite").as_posix()
    # No schema or DB file yet. Only the winner may reach schema validation.
    # Use migrated storage for successful TaskEngine startup, sidecar remains cold.
    from agentforge.db.migrate import upgrade_database

    upgrade_database(url)
    processes = [Peer(url) for _ in range(4)]
    try:
        for process in processes:
            process.receive("ready")
        for process in processes:
            process.send()
        outcomes = [process.messages.get(timeout=15) for process in processes]
        assert [result["event"] for result in outcomes].count("owned") == 1
        for process, result in zip(processes, outcomes, strict=True):
            if result["event"] == "owned":
                process.send()
                process.receive("closed")
            else:
                assert result == {"event": "rejected", "message": BUSY_MESSAGE}
            process.finish()
    finally:
        for process in processes:
            process.cleanup()


def test_force_kill_releases_lock_and_recovers_only_orphaned_work(
    database, registry, tmp_path
):
    root = tmp_path / "project"
    root.mkdir()
    project = registry.register_project("Offline", root)
    with peer(database[1], "active") as owner:
        start(owner)
        owner.send(project_id=str(project.id))
        running = owner.receive("running")["task_id"]
        owner.process.kill()
        owner.process.wait(timeout=10)
    with peer(database[1], "recover") as restarted:
        start(restarted)
        recovered = restarted.receive("recovered")
        assert [running, "failed", "execution_interrupted"] in recovered["tasks"]
        assert sum(row[1] == "queued" for row in recovered["tasks"]) == 1
        assert recovered["coding"] == ["interrupted"]
        restarted.receive("closed")
        restarted.finish()
    # Another restart can execute the preserved queued Council participant.
    with peer(database[1], "active") as next_owner:
        start(next_owner)
        next_owner.send(project_id=str(project.id))
        next_owner.receive("running")
        next_owner.send()
        next_owner.receive("completed")
        next_owner.receive("closed")
        next_owner.finish()
    with create_session_factory(database[0])() as session:
        states = list(session.scalars(select(TaskRecord.state)))
    assert states.count("failed") == 1 and states.count("completed") == 3


@pytest.mark.parametrize("mode", ["failure", "cancel"])
def test_startup_failure_and_cancellation_release_native_handles(database, mode):
    with peer(database[1], mode) as failing:
        failing.send()
        failing.receive("startup_failed")
        failing.finish()
    with peer(database[1]) as restarted:
        start(restarted)
        restarted.send()
        restarted.receive("closed")
        restarted.finish()


def test_relative_absolute_and_dot_aliases_contend(database):
    path = Path(database[1].database)
    with peer(database[1]) as owner:
        start(owner)
        reject("sqlite:///./sub/../" + path.name, cwd=path.parent)
        reject("sqlite:///" + path.name, cwd=path.parent)
        owner.send()
        owner.receive("closed")
        owner.finish()


@pytest.mark.posix
def test_symlink_alias_and_renamed_database_cannot_bypass_owner(database, tmp_path):
    path = Path(database[1].database)
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(path)
    with peer(database[1]) as owner:
        start(owner)
        reject("sqlite:///" + alias.as_posix())
        moved = path.with_name("moved.sqlite")
        path.rename(moved)
        # Inode flock also protects a renamed DB with a different sidecar path.
        reject("sqlite:///" + moved.as_posix())
        moved.rename(path)
        owner.send()
        owner.receive("closed")
        owner.finish()


@pytest.mark.windows
def test_native_windows_case_alias_and_delete_sharing(database):
    with peer(database[1]) as owner:
        start(owner)
        reject("sqlite:///" + str(database[1].database).swapcase())
        with pytest.raises(OSError):
            Path(database[1].database).rename(str(database[1].database) + ".moved")
        owner.send()
        owner.receive("closed")
        owner.finish()


def test_hardlink_database_alias_fails_closed(database, tmp_path):
    alias = tmp_path / "hard.sqlite"
    os.link(database[1].database, alias)
    engine = create_database_engine("sqlite:///" + alias.as_posix())
    try:
        with pytest.raises(ExecutorOwnershipError, match="ownership is unavailable"):
            DatabaseOwnership(engine).acquire()
    finally:
        engine.dispose()
        alias.unlink()


def test_sidecar_survives_repeated_release_and_is_not_inherited(database):
    owner = DatabaseOwnership(database[0])
    owner.acquire()
    sidecar = Path(database[1].database + ".agentforge.lock")
    identity = sidecar.stat().st_ino
    reject(database[1])
    owner.release()
    owner.release()
    assert sidecar.stat().st_ino == identity
    with peer(database[1]) as restarted:
        start(restarted)
        restarted.send()
        restarted.receive("closed")
        restarted.finish()
    assert sidecar.stat().st_ino == identity


@pytest.mark.posix
def test_database_and_lock_replacement_fail_before_sql(database, tmp_path):
    for target in (
        Path(database[1].database),
        Path(database[1].database + ".agentforge.lock"),
    ):
        owner = DatabaseOwnership(database[0])
        owner.acquire()
        moved = tmp_path / "moved"
        target.rename(moved)
        target.touch(mode=0o600)
        try:
            with pytest.raises(ExecutorOwnershipError):
                with database[0].connect() as connection:
                    connection.execute(text("SELECT 1"))
        finally:
            owner.release()
            target.unlink()
            moved.rename(target)


@pytest.mark.parametrize("kind", ["directory", "hardlink", "permissions"])
def test_unsafe_sidecar_fails_without_database_mutation(database, tmp_path, kind):
    sidecar = Path(database[1].database + ".agentforge.lock")
    if kind == "directory":
        sidecar.mkdir()
    elif kind == "hardlink":
        os.link(database[1].database, sidecar)
    else:
        # Native POSIX permission protection; Windows uses inherited operator ACLs.
        if os.name == "nt":
            sidecar.mkdir()  # Native failure to create a regular lock file.
        else:
            sidecar.touch(mode=0o666)
            sidecar.chmod(0o666)
    before = Path(database[1].database).read_bytes()
    with pytest.raises(ExecutorOwnershipError) as error:
        DatabaseOwnership(database[0]).acquire()
    assert str(error.value) == UNSAFE_MESSAGE
    assert Path(database[1].database).read_bytes() == before


def test_memory_database_preserves_per_engine_process_guard():
    database = create_database_engine("sqlite://")
    first, second = DatabaseOwnership(database), DatabaseOwnership(database)
    first.acquire()
    try:
        with pytest.raises(ExecutorOwnershipError, match="already owns"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
    database.dispose()


@pytest.mark.parametrize(
    "url", ["sqlite:///file:ambiguous?uri=true", "sqlite:///:memory:?cache=shared"]
)
def test_ambiguous_sqlite_configurations_fail_closed(url):
    engine = create_database_engine(url)
    try:
        with pytest.raises(ExecutorOwnershipError):
            DatabaseOwnership(engine).acquire()
    finally:
        engine.dispose()


@pytest.mark.posix
def test_lock_creation_permission_failure_releases_process_reservation(database):
    parent = Path(database[1].database).parent
    parent.chmod(0o500)
    try:
        with pytest.raises(ExecutorOwnershipError) as error:
            DatabaseOwnership(database[0]).acquire()
        assert str(error.value) == UNSAFE_MESSAGE
    finally:
        parent.chmod(0o700)
    owner = DatabaseOwnership(database[0])
    owner.acquire()
    owner.release()


def test_unsupported_filesystem_rejected_before_schema_or_storage_change(
    database, monkeypatch
):
    import agentforge.db.ownership as ownership

    def unsupported(*args):
        raise ExecutorOwnershipError(UNSAFE_MESSAGE)

    owner = DatabaseOwnership(database[0])
    monkeypatch.setattr(owner, "_windows" if os.name == "nt" else "_posix", unsupported)
    before = Path(database[1].database).read_bytes()
    with pytest.raises(ExecutorOwnershipError):
        owner.acquire()
    assert Path(database[1].database).read_bytes() == before
    # This simulates unsupported volume detection, never native Windows locking.
    assert owner._resources is None
    assert owner.key not in ownership._OWNERS


@pytest.mark.posix
def test_offline_replacement_discards_connections_to_previous_database(
    database, tmp_path
):
    path = Path(database[1].database)
    owner = DatabaseOwnership(database[0])
    owner.acquire()
    with database[0].begin() as connection:
        connection.execute(text("CREATE TABLE old_inode (value INTEGER)"))
    owner.release()
    moved = tmp_path / "previous.sqlite"
    path.rename(moved)
    from agentforge.db.migrate import upgrade_database

    upgrade_database(str(database[1]))
    owner.acquire()
    try:
        with database[0].connect() as connection:
            assert not connection.execute(
                text("SELECT name FROM sqlite_master WHERE name='old_inode'")
            ).all()
    finally:
        owner.release()
