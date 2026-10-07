"""First-release schema from empty storage and its destructive base roundtrip."""

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from shutil import copytree, ignore_patterns
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, func, inspect, select, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from agentforge.db import migrate
from agentforge.db.coding import WorkspaceRepository
from agentforge.db.database import Base, create_database_engine, create_session_factory
from agentforge.db.index import IndexRepository
from agentforge.db.migrate import upgrade_database
from agentforge.db.models import CodingWorkspaceRecord, ProjectRecord, TaskRecord
from agentforge.db.projects import ProjectRepository
from agentforge.db.tasks import TaskRepository
from agentforge.index.service import ProjectIndex
from agentforge.projects.service import ProjectRegistry


def configuration():
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(migrate.__file__).with_name("migrations")).replace("%", "%%"),
    )
    return config


def assert_schema(connection):
    assert (
        MigrationContext.configure(connection).get_current_revision() == "0001_initial"
    )
    config = configuration()
    config.attributes["connection"] = connection
    command.check(
        config
    )  # Columns/types, nullability, FKs, uniqueness, indexes, defaults.
    inspector = inspect(connection)
    assert set(inspector.get_table_names()) == {
        *Base.metadata.tables,
        "alembic_version",
    }
    # Alembic doesn't compare CHECK constraints automatically.
    for table in Base.metadata.sorted_tables:
        expected = {
            c.name: str(c.sqltext)
            for c in table.constraints
            if isinstance(c, CheckConstraint)
        }
        actual = {
            c["name"]: c["sqltext"] for c in inspector.get_check_constraints(table.name)
        }
        assert actual == expected, table.name
    assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def populate(engine, tmp_path):
    """Real registration/index and lifecycle writes, plus private ownership metadata."""
    sessions = create_session_factory(engine)
    root = tmp_path / "project"
    root.mkdir()
    (root / "source.py").write_text(
        "def child(): return 1\ndef parent(): return child()\n"
    )
    projects = ProjectRegistry(ProjectRepository(sessions))
    project = projects.register_project("Synthetic", root)
    ProjectIndex(projects, IndexRepository(sessions)).refresh_index(project.id)
    tasks = TaskRepository(sessions)
    _, members = tasks.add_council(
        project_id=project.id,
        agent_id="repo_explorer",
        request="Synthetic request",
        targets=(
            ("local", "ollama", "model"),
            ("remote", "openai_compatible", "model"),
        ),
    )
    tasks.cancel(members[0].task_id)
    WorkspaceRepository(sessions).add(
        task_id=members[1].task_id,
        workspace_id=uuid4(),
        project_id=project.id,
        worker_id="remote",
        branch_name="agentforge/synthetic",
        base_commit="0" * 40,
        created_at=datetime.now(UTC),
        state="interrupted",
        repository_path=str(root),
        worktree_path=str(tmp_path / "private"),
        prefix="",
        identities={},
        observations={},
    )
    return project


def test_single_initial_revision():
    scripts = ScriptDirectory.from_config(configuration())
    revisions = list(scripts.walk_revisions())
    assert scripts.get_heads() == scripts.get_bases() == ["0001_initial"]
    assert len(revisions) == 1 and revisions[0].down_revision is None


def test_source_alembic_online_and_offline_entrypoints(tmp_path):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    url = URL.create("sqlite", database=str(tmp_path / "cli.sqlite"))
    config.set_main_option("sqlalchemy.url", url.render_as_string().replace("%", "%%"))
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    try:
        with engine.connect() as connection:
            assert_schema(connection)
    finally:
        engine.dispose()
    config.output_buffer = StringIO()
    command.upgrade(config, "head", sql=True)
    sql = config.output_buffer.getvalue()
    for table in Base.metadata.tables:
        assert f"CREATE TABLE {table}" in sql
    assert "0001_initial" in sql
    assert "ALTER TABLE" not in sql


def counts(connection):
    return {
        name: connection.scalar(select(func.count()).select_from(table))
        for name, table in Base.metadata.tables.items()
    }


def test_empty_upgrade_and_entrypoint_idempotency(tmp_path):
    url = "sqlite:///" + (tmp_path / "fresh.db").as_posix()
    upgrade_database(url)
    engine = create_database_engine(url)
    try:
        with engine.begin() as connection:
            assert_schema(connection)
        project = populate(engine, tmp_path)
        with engine.begin() as connection:
            before = counts(connection)
        upgrade_database(url)
        with engine.begin() as connection:
            assert_schema(connection)
            assert counts(connection) == before
        # SQLite's UUID and UTC timestamp roundtrip is the service contract.
        restored = ProjectRepository(create_session_factory(engine)).get(project.id)
        assert restored == project and restored.created_at.tzinfo == UTC
    finally:
        engine.dispose()


def test_packaged_migration_path_with_literal_percent(tmp_path, monkeypatch):
    source = Path(migrate.__file__).with_name("migrations")
    installed = tmp_path / "package%location"
    copytree(source, installed / "migrations", ignore=ignore_patterns("__pycache__"))
    monkeypatch.setattr(migrate, "__file__", str(installed / "migrate.py"))
    url = "sqlite:///" + (tmp_path / "percent.db").as_posix()
    upgrade_database(url)
    engine = create_database_engine(url)
    try:
        with engine.connect() as connection:
            assert_schema(connection)
            assert migrate.schema_is_current(connection)
    finally:
        engine.dispose()


def test_populated_head_downgrades_to_empty_and_reupgrades(database, tmp_path):
    engine, _ = database
    populate(engine, tmp_path)
    config = configuration()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        assert all(count > 0 for count in counts(connection).values())
        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]
        assert MigrationContext.configure(connection).get_current_revision() is None
        command.upgrade(config, "head")
        assert_schema(connection)
        assert all(count == 0 for count in counts(connection).values())


@pytest.mark.parametrize(
    "table,field,value",
    [
        (ProjectRecord, "root_identity", None),
        (TaskRecord, "state", "invented"),
        (TaskRecord, "telemetry_status", "invented"),
        (CodingWorkspaceRecord, "state", "invented"),
    ],
)
def test_required_identity_and_lifecycle_checks_reject_invalid_writes(
    database, tmp_path, table, field, value
):
    engine, _ = database
    populate(engine, tmp_path)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(update(table).values({field: value}))
    with engine.begin() as connection:
        assert_schema(connection)
