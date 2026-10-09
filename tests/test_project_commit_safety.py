"""Rejected Project writes cannot survive a later pooled SQLite transaction."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from uuid import uuid4

import pytest

from agentforge.db.database import create_session_factory
from agentforge.db.index import IndexRepository
from agentforge.db.projects import ProjectRepository
from agentforge.index.service import ProjectIndex
from agentforge.projects.errors import ProjectStorageError
from tests.sqlite_busy import blocked_commit


def durable_registration(engine):
    # Inspect registration and all cascaded index data outside the failed pool.
    with closing(sqlite3.connect(engine.url.database)) as reader:
        return {
            table: reader.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "projects",
                "project_indexes",
                "indexed_files",
                "index_symbols",
                "index_relationships",
            )
        }


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_failed_project_commit_cannot_leak_into_unrelated_write(
    database, registry, tmp_path, operation
):
    engine, _ = database
    root = tmp_path / "registered"
    root.mkdir()
    (root / "example.py").write_text(
        "def value():\n    return 1\n\ndef caller():\n    return value()\n"
    )
    project = registry.register_project("Indexed project", root)
    sessions = create_session_factory(engine)
    ProjectIndex(registry, IndexRepository(sessions)).refresh_index(project.id)
    repository = ProjectRepository(sessions)
    before = durable_registration(engine)
    assert all(before.values())  # The removal must preserve actual index data too.
    rejected_root = tmp_path / "rejected"
    rejected_root.mkdir()
    rejected = replace(
        project,
        id=uuid4(),
        root_path=rejected_root,
        root_identity=registry.filesystem.observe_root(rejected_root),
    )

    message = (
        "Could not store project" if operation == "add" else "Could not remove project"
    )
    with blocked_commit(engine):
        with pytest.raises(ProjectStorageError, match=message):
            if operation == "add":
                repository.add(rejected)
            else:
                repository.remove(project.id)
    assert engine.pool.checkedout() == 0

    other_root = tmp_path / "unrelated"
    other_root.mkdir()
    other = replace(
        project,
        id=uuid4(),
        root_path=other_root,
        root_identity=registry.filesystem.observe_root(other_root),
    )
    # Call the repository directly: registration's read-only collision check
    # could roll back leaked work and mask the failed-COMMIT defect.
    assert repository.add(other) == other
    after = durable_registration(engine)
    assert len(after["projects"]) == len(before["projects"]) + 1
    after["projects"] = [row for row in after["projects"] if row[0] != other.id.hex]
    assert after == before
    assert engine.pool.checkedout() == 0
