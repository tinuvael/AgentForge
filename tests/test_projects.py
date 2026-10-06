"""Registry lifecycle and filesystem boundaries using migrated temporary databases."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import URL

from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.models import ProjectRecord
from agentforge.db.projects import ProjectRepository
from agentforge.projects.errors import (
    InvalidProjectName,
    InvalidProjectPath,
    ProjectAlreadyRegistered,
    ProjectNotFound,
    ProjectStorageError,
    UnsafeProjectPath,
)
from agentforge.projects.models import Project
from agentforge.projects.paths import resolve_project_path
from agentforge.projects.service import ProjectRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]


def migration_config() -> Config:
    return Config(str(REPO_ROOT / "alembic.ini"))


def test_registry_lifecycle(registry, tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "unrelated" / "second"
    first_root.mkdir()
    second_root.mkdir(parents=True)
    first = registry.register_project("Same name", first_root)
    second = registry.register_project("Same name", second_root)
    assert isinstance(first.id, UUID)
    assert first.id != second.id
    assert first.created_at.tzinfo == UTC
    assert first.created_at <= datetime.now(UTC)
    assert first.root_path == first_root.resolve()
    assert registry.list_projects() == [first, second]
    assert registry.get_project(str(first.id)) == first
    assert registry.get_project(second.id) == second
    observation = registry.inspect_project(first.id)
    assert observation.project == first
    assert observation.git.status == "not_repository"
    assert observation.git.is_repository is False
    registry.remove_project(str(first.id))
    assert registry.list_projects() == [second]
    assert first_root.is_dir()  # Removal changes registry state only.
    with pytest.raises(ProjectNotFound):
        registry.get_project(first.id)
    with pytest.raises(ProjectNotFound):
        registry.remove_project(first.id)
    replacement = registry.register_project("New registration", first_root)
    assert replacement.id != first.id


def test_persistence_reopen(registry, database, tmp_path):
    engine, url = database
    root = tmp_path / "project"
    root.mkdir()
    project = registry.register_project("Persistent", root)
    engine.dispose()
    reopened_engine = create_database_engine(url)
    try:
        reopened = ProjectRegistry(
            ProjectRepository(create_session_factory(reopened_engine)),
            base_directory=tmp_path,
        )
        assert reopened.get_project(project.id) == project
        assert reopened.list_projects() == [project]
        reopened.remove_project(project.id)
    finally:
        reopened_engine.dispose()
    assert registry.list_projects() == []


@pytest.mark.posix
def test_canonical_relative_paths_and_duplicates(registry, tmp_path, monkeypatch):
    root = tmp_path / "project"
    child = root / "child"
    child.mkdir(parents=True)
    project = registry.register_project(" Project ", "project/child/..")
    assert project.name == "Project"
    assert project.root_path == root.resolve()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with pytest.raises(ProjectAlreadyRegistered):
        registry.register_project("Other", "./project")
    with pytest.raises(ProjectAlreadyRegistered):
        registry.register_project("Other", child / "..")


def test_default_relative_base_is_captured(database, tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(tmp_path)
    registry = ProjectRegistry(ProjectRepository(create_session_factory(database[0])))
    monkeypatch.chdir(root)
    assert registry.register_project("Relative", "project").root_path == root


@pytest.mark.parametrize("path", ["missing", "", "\x00"])
def test_invalid_root(registry, path):
    with pytest.raises(InvalidProjectPath):
        registry.register_project("Invalid", path)
    assert registry.list_projects() == []


def test_file_is_not_root(registry, tmp_path):
    file = tmp_path / "file"
    file.write_text("content")
    with pytest.raises(InvalidProjectPath):
        registry.register_project("File", file)


@pytest.mark.parametrize("name", ["", "   ", "x" * 256])
def test_invalid_name(registry, tmp_path, name):
    with pytest.raises(InvalidProjectName):
        registry.register_project(name, tmp_path)


@pytest.mark.parametrize("project_id", ["invalid", str(uuid4())])
def test_missing_id(registry, project_id):
    for operation in [
        registry.get_project,
        registry.remove_project,
        registry.inspect_project,
    ]:
        with pytest.raises(ProjectNotFound):
            operation(project_id)


@pytest.mark.posix
def test_symlink_registration(registry, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    project = registry.register_project("Alias", alias)
    assert project.root_path == root.resolve()
    with pytest.raises(ProjectAlreadyRegistered):
        registry.register_project("Real", root)
    broken = tmp_path / "broken"
    broken.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(InvalidProjectPath):
        registry.register_project("Broken", broken)


@pytest.mark.posix
def test_containment(registry, tmp_path):
    root = tmp_path / "project"
    child = root / "sub" / "file.py"
    child.parent.mkdir(parents=True)
    child.write_text("pass")
    sibling = tmp_path / "project-other"
    sibling.mkdir()
    (sibling / "secret").write_text("outside")
    project = registry.register_project("Scoped", root)
    assert registry.resolve_path(project.id, "sub/file.py") == child
    assert registry.resolve_path(project.id, child) == child
    assert registry.resolve_path(project.id, ".") == root
    for candidate in ["../project-other/secret", sibling / "secret", "missing"]:
        with pytest.raises(UnsafeProjectPath):
            registry.resolve_path(project.id, candidate)
    inside_alias = root / "inside-alias"
    inside_alias.symlink_to(child)
    assert resolve_project_path(root, inside_alias) == child
    outside_alias = root / "outside-alias"
    outside_alias.symlink_to(sibling, target_is_directory=True)
    with pytest.raises(UnsafeProjectPath):
        registry.resolve_path(project.id, "outside-alias/secret")
    (sibling / "nested").mkdir()
    (root / "sub" / "link").symlink_to(sibling / "nested", target_is_directory=True)
    with pytest.raises(UnsafeProjectPath):
        registry.resolve_path(project.id, "sub/link/../secret")
    loop = root / "loop"
    loop.symlink_to(loop)
    with pytest.raises(UnsafeProjectPath):
        registry.resolve_path(project.id, loop)


@pytest.mark.posix
def test_changed_root_fails_closed(registry, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    project = registry.register_project("Scoped", root)
    root.rmdir()
    with pytest.raises(InvalidProjectPath):
        registry.inspect_project(project.id)
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InvalidProjectPath):
        registry.inspect_project(project.id)
    with pytest.raises(UnsafeProjectPath):
        registry.resolve_path(project.id, ".")
    # Configuration stays retrievable/removable even if its root disappears.
    assert registry.get_project(project.id) == project
    registry.remove_project(project.id)


@pytest.mark.parametrize("operation", ["register", "get", "list", "remove"])
def test_database_failures_are_service_errors(registry, database, tmp_path, operation):
    ProjectRecord.__table__.drop(database[0])
    operations = {
        "register": lambda: registry.register_project("Project", tmp_path),
        "get": lambda: registry.get_project(uuid4()),
        "list": registry.list_projects,
        "remove": lambda: registry.remove_project(uuid4()),
    }
    with pytest.raises(ProjectStorageError) as error:
        operations[operation]()
    assert "SELECT" not in str(error.value)
    assert "projects" not in str(error.value) or operation == "list"
    assert error.value.__suppress_context__


def test_database_constraint_handles_duplicate_and_rolls_back(database, tmp_path):
    repository = ProjectRepository(create_session_factory(database[0]))
    project = Project(uuid4(), "Project", tmp_path, datetime.now(UTC))
    repository.add(project)
    with pytest.raises(ProjectAlreadyRegistered):
        repository.add(replace(project, id=uuid4()))
    assert repository.list() == [project]
    # A different integrity failure must not be reported as a root duplicate.
    with pytest.raises(ProjectStorageError):
        repository.add(replace(project, root_path=tmp_path / "different"))
    assert repository.list() == [project]


def test_migration_upgrade_downgrade_and_metadata_match(database):
    engine, _ = database
    config = migration_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.check(config)
        command.downgrade(config, "base")
        assert "projects" not in inspect(connection).get_table_names()
        command.upgrade(config, "head")
        command.check(config)


def test_migration_cli_url_and_offline_sql(tmp_path):
    import io

    config = migration_config()
    url = URL.create("sqlite", database=str(tmp_path / "cli.sqlite"))
    config.set_main_option("sqlalchemy.url", url.render_as_string().replace("%", "%%"))
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    try:
        assert "projects" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    config.output_buffer = io.StringIO()
    command.upgrade(config, "head", sql=True)
    assert "CREATE TABLE projects" in config.output_buffer.getvalue()
