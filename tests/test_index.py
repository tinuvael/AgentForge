"""Offline Project Index behavior against synthetic, temporary repositories."""

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select

from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.index import IndexRepository
from agentforge.db.models import (
    IndexedFileRecord,
    IndexStateRecord,
    ProjectRecord,
    RelationshipRecord,
    SymbolRecord,
)
from agentforge.db.projects import ProjectRepository
from agentforge.index import service
from agentforge.index.models import IndexRefreshError, IndexStorageError, SymbolNotFound
from agentforge.index.render import approximate_tokens
from agentforge.index.service import ProjectIndex
from agentforge.projects import filesystem
from agentforge.projects import service as registry_service
from agentforge.projects.errors import (
    InvalidProjectPath,
    ProjectNotFound,
    UnsafeProjectPath,
)
from agentforge.projects.exclusions import EXCLUDED_DIRECTORIES
from agentforge.projects.service import ProjectRegistry
from agentforge.tools.service import RepositoryTools


@pytest.fixture
def indexed(registry, database, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    project = registry.register_project("Synthetic", root)
    index = ProjectIndex(registry, IndexRepository(create_session_factory(database[0])))
    return index, project, root


def write(root, path, text):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)
    return file


def one(index, project, query):
    matches = index.find_symbol(project.id, query)
    assert len(matches) == 1, matches
    return matches[0]


def test_symbols_locations_containment_and_nested_identity(indexed):
    index, project, root = indexed
    write(
        root,
        "module_a.py",
        """class Runner:
    def run(self):
        def inner():
            pass
        return inner()

def run():
    class Runner:
        def run(self):
            pass
    return Runner()
""",
    )
    write(root, "module_b.py", "def run():\n    pass\n")
    status = index.refresh_index(str(project.id))
    assert status.file_count == 2
    assert status.symbol_count == 7
    assert status.observed_head is None  # Non-Git is fully supported.
    assert not status.needs_refresh
    assert len(index.find_symbol(project.id, "run")) == 4
    method = one(index, project, "module_a.Runner.run")
    assert (method.kind, method.relative_path, method.start_line, method.end_line) == (
        "method",
        "module_a.py",
        2,
        5,
    )
    nested = one(index, project, "module_a.run.Runner.run")
    inner = one(index, project, "module_a.Runner.run.inner")
    assert nested.id != method.id
    assert inner.kind == "function"
    assert inner.parent_id == method.id
    assert index.get_symbol(project.id, inner.id) == inner
    contains = [
        edge for edge in index.get_relationships(project.id) if edge.kind == "contains"
    ]
    assert len(contains) == 7
    assert any(e.source_id == method.id and e.target_id == inner.id for e in contains)
    assert index.find_symbol(project.id, "runner")
    assert index.find_symbol(project.id, "") == []
    assert index.find_symbol(project.id, "missing") == []


def test_absolute_relative_and_unresolved_imports(indexed):
    index, project, root = indexed
    write(root, "pkg/__init__.py", "from .module import Thing\nfrom . import module\n")
    write(root, "pkg/module.py", "class Thing:\n    pass\n")
    write(root, "pkg/sub/client.py", "from ..module import Thing as Alias\n")
    write(
        root,
        "client.py",
        """import pkg
import pkg.module as module
from pkg.module import Thing
import external_library
from external_library import Thing
from .pkg.module import Thing
from pkg.module import *
""",
    )
    index.refresh_index(project.id)
    edges = [e for e in index.get_relationships(project.id) if e.kind == "imports"]
    resolved = [e for e in edges if e.target_id]
    assert len(resolved) == 6
    assert {e.target_text for e in resolved} == {
        "from .module import Thing",
        "from . import module",
        "import pkg",
        "import pkg.module as module",
        "from pkg.module import Thing",
        "from ..module import Thing as Alias",
    }
    assert {e.target_text for e in edges if not e.target_id} == {
        "import external_library",
        "from external_library import Thing",
        "from .pkg.module import Thing",
        "from pkg.module import *",
    }
    thing = one(index, project, "pkg.module.Thing")
    assert (
        len(
            [
                e
                for e in index.get_dependents(project.id, thing.id)
                if e.kind == "imports"
            ]
        )
        == 3
    )


def test_local_calls_self_calls_and_directional_queries(indexed):
    index, project, root = indexed
    write(
        root,
        "service.py",
        """def helper():
    pass

def run(obj):
    helper()
    obj.foo()

class Worker:
    def execute(self):
        self.finish()
    def finish(self):
        pass
helper()
""",
    )
    index.refresh_index(project.id)
    helper = one(index, project, "helper")
    run = one(index, project, "run")
    execute = one(index, project, "execute")
    finish = one(index, project, "finish")
    assert any(
        e.kind == "calls" and e.target_id == helper.id
        for e in index.get_dependencies(project.id, run.id)
    )
    assert any(
        e.kind == "calls" and e.source_id == run.id
        for e in index.get_dependents(project.id, helper.id)
    )
    assert any(
        e.kind == "calls" and e.target_id == finish.id
        for e in index.get_dependencies(project.id, execute.id)
    )
    assert {e.kind for e in index.get_related_symbols(project.id, helper.id)} == {
        "calls",
        "contains",
    }
    assert all("obj.foo" != e.target_text for e in index.get_relationships(project.id))
    assert (
        len(
            [
                e
                for e in index.get_dependents(project.id, helper.id)
                if e.kind == "calls"
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    "source",
    [
        "def helper(): pass\ndef run(helper): helper()\n",
        "def helper(): pass\ndef run():\n    helper = lambda: 0\n    helper()\n",
        "def helper(): pass\nhelper = lambda: 0\nhelper()\n",
        "def helper(): pass\ndef helper(): pass\nhelper()\n",
        "if True:\n    def helper(): pass\nhelper()\n",
        "@decorator\ndef helper(): pass\nhelper()\n",
        "def helper(): pass\nfrom elsewhere import helper\nhelper()\n",
        "def helper(): pass\nfrom elsewhere import *\nhelper()\n",
        "def helper(): pass\ndef run():\n    global helper\n    helper()\n",
        "def helper(): pass\ndef run():\n    try: pass\n"
        "    except Exception as helper: helper()\n",
        "def helper(): pass\ndef run():\n    for helper in []: helper()\n",
        "def helper(): pass\n[helper() for helper in []]\n",
        "def helper(): pass\nx = lambda helper: helper()\n",
        "class C:\n    def helper(self): pass\n    def run(self): helper()\n",
        "class C:\n    def helper(self): pass\n    def run(self):\n"
        "        self = other\n        self.helper()\n",
        "class C:\n    @staticmethod\n    def helper(): pass\n"
        "    def run(self): self.helper()\n",
        "class C:\n    def helper(self): pass\n    def run(self):\n"
        "        self.helper = other\n        self.helper()\n",
    ],
)
def test_uncertain_calls_are_not_linked(indexed, source):
    index, project, root = indexed
    write(root, "module.py", source)
    index.refresh_index(project.id)
    assert not [
        e
        for e in index.get_relationships(project.id)
        if e.kind == "calls" and e.target_id
    ]


def test_nested_lexical_calls_and_async_definitions(indexed):
    index, project, root = indexed
    write(
        root,
        "nested.py",
        """async def outer():
    def helper():
        pass
    async def inner():
        helper()
    return inner()
""",
    )
    index.refresh_index(project.id)
    helper = one(index, project, "nested.outer.helper")
    inner = one(index, project, "nested.outer.inner")
    assert any(
        e.target_id == helper.id for e in index.get_dependencies(project.id, inner.id)
    )


@pytest.mark.parametrize(
    "directory", sorted(EXCLUDED_DIRECTORIES | {"example.egg-info"})
)
def test_excluded_directories_are_not_traversed(indexed, directory):
    index, project, root = indexed
    write(root, "included.py", "def included(): pass\n")
    write(root, f"{directory}/deep/hidden.py", "def hidden(): pass\n")
    write(root, "binary.bin", "not Python")
    assert index.refresh_index(project.id).file_count == 1
    assert not index.find_symbol(project.id, "hidden")


@pytest.mark.posix
def test_symlinks_inside_and_outside_are_skipped(indexed, tmp_path):
    index, project, root = indexed
    target = write(root, "real/source.py", "def inside(): pass\n")
    outside = tmp_path / "outside"
    secret = write(outside, "secret.py", "def outside_secret(): pass\n")
    (root / "file_alias.py").symlink_to(target)
    (root / "directory_alias").symlink_to(target.parent, target_is_directory=True)
    (root / "escape.py").symlink_to(secret)
    (root / "escape_dir").symlink_to(outside, target_is_directory=True)
    (root / "broken.py").symlink_to(tmp_path / "missing")
    (root / "loop").symlink_to(root, target_is_directory=True)
    assert index.refresh_index(project.id).file_count == 1
    assert one(index, project, "inside").relative_path == "real/source.py"
    assert not index.find_symbol(project.id, "outside_secret")


def test_refresh_changes_additions_deletions_and_parse_reuse(indexed, monkeypatch):
    index, project, root = indexed
    first = write(root, "first.py", "def first(): pass\n")
    deleted = write(root, "deleted.py", "def gone(): pass\n")
    index.refresh_index(project.id)
    before = one(index, project, "first.first")
    original = service.parse_python
    parsed = []

    def counted(path, content):
        parsed.append(path)
        return original(path, content)

    monkeypatch.setattr(service, "parse_python", counted)
    index.refresh_index(project.id)
    assert parsed == []
    assert one(index, project, "first.first") == before
    first.write_text("def changed(): pass\n")
    write(root, "new.py", "def new(): pass\n")
    deleted.unlink()
    assert index.get_index_status(project.id).changed_paths == (
        "deleted.py",
        "first.py",
        "new.py",
    )
    index.refresh_index(project.id)
    assert parsed == ["first.py", "new.py"]
    assert not index.find_symbol(project.id, "gone")
    assert not index.find_symbol(project.id, "first.first")
    assert one(index, project, "changed")
    assert one(index, project, "new.new")
    assert not index.get_index_status(project.id).needs_refresh


def test_import_resolution_updates_when_unchanged_importer_target_changes(indexed):
    index, project, root = indexed
    write(root, "client.py", "from target import Thing\n")
    index.refresh_index(project.id)
    assert index.get_relationships(project.id)[0].target_id is None
    write(root, "target.py", "class Thing: pass\n")
    index.refresh_index(project.id)
    assert any(
        e.target_id for e in index.get_relationships(project.id) if e.kind == "imports"
    )
    (root / "target.py").unlink()
    index.refresh_index(project.id)
    assert all(e.target_id is None for e in index.get_relationships(project.id))


def test_dirty_git_change_detected_without_head_change(indexed):
    index, project, root = indexed
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    file = write(root, "module.py", "def before(): pass\n")
    git("init")
    git("add", "module.py")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Initial",
    )
    head = git("rev-parse", "HEAD")
    assert index.refresh_index(project.id).observed_head == head
    file.write_text("def after(): pass\n")
    assert index.get_index_status(project.id).changed_paths == ("module.py",)
    assert index.refresh_index(project.id).observed_head == head
    assert git("rev-parse", "HEAD") == head
    assert one(index, project, "after")
    assert not index.find_symbol(project.id, "module.before")
    assert git("status", "--porcelain") == "M module.py"


def test_syntax_failure_retains_old_structure_and_recovers(indexed, monkeypatch):
    index, project, root = indexed
    valid = write(root, "valid.py", "def valid(): pass\n")
    broken = write(root, "broken.py", "def previous(): pass\n")
    index.refresh_index(project.id)
    previous = one(index, project, "previous")
    broken.write_text("def broken(:\n    secret_source_text\n")
    valid.write_text("def updated(): pass\n")
    write(root, "new_broken.py", "def bad(:\n")
    status = index.refresh_index(project.id)
    assert status.needs_refresh
    assert len(status.failures) == 2
    assert status.failures[0].has_previous_structure
    assert not status.failures[1].has_previous_structure
    assert all("secret_source_text" not in f.message for f in status.failures)
    retained = one(index, project, "previous")
    assert retained.id == previous.id and retained.stale
    assert one(index, project, "updated")
    assert "[stale: parse error]" in index.render_project_map(project.id)
    monkeypatch.setattr(
        service,
        "parse_python",
        lambda *args: pytest.fail("Unchanged sources must not be reparsed"),
    )
    assert index.refresh_index(project.id).failures == status.failures
    monkeypatch.undo()
    broken.write_text("def repaired(): pass\n")
    (root / "new_broken.py").unlink()
    assert not index.refresh_index(project.id).needs_refresh
    assert not index.find_symbol(project.id, "previous")
    assert not one(index, project, "repaired").stale


def test_failed_scan_rolls_back_complete_refresh(indexed, database, monkeypatch):
    index, project, root = indexed
    file = write(root, "module.py", "def original(): pass\n")
    initial = index.refresh_index(project.id)
    before = index.render_project_map(project.id)
    file.write_text("def changed(): pass\n")

    def failed_scan(_root):
        yield "module.py", file.read_bytes()
        raise IndexRefreshError("Synthetic traversal failure")

    monkeypatch.setattr(service, "scan_python_files", failed_scan)
    with pytest.raises(IndexRefreshError):
        index.refresh_index(project.id)
    assert index.render_project_map(project.id) == before
    snapshot = IndexRepository(create_session_factory(database[0])).read(project.id)
    assert snapshot.indexed_at == initial.indexed_at


@pytest.mark.posix
def test_replaced_root_is_rejected_and_previous_index_survives(indexed, tmp_path):
    index, project, root = indexed
    write(root, "module.py", "def original(): pass\n")
    index.refresh_index(project.id)
    root.rename(tmp_path / "moved")
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "secret.py", "def secret(): pass\n")
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InvalidProjectPath):
        index.refresh_index(project.id)
    with pytest.raises(InvalidProjectPath):
        index.get_index_status(project.id)
    assert one(index, project, "original")
    assert not index.find_symbol(project.id, "secret")


def test_ordinary_root_replacement_rejected_after_reopen(
    indexed, registry, database, tmp_path, monkeypatch
):
    index, project, root = indexed
    write(root, "module.py", "def original(): pass\n")
    index.refresh_index(project.id)
    sessions = create_session_factory(database[0])
    before = IndexRepository(sessions).read(project.id)
    before_map = index.render_project_map(project.id)
    root.rename(tmp_path / "registered-directory")
    root.mkdir()
    write(root, "replacement.py", "def unauthorized_replacement(): pass\n")
    assert root.stat().st_ino != project.root_inode

    def unexpected_git(*args):
        pytest.fail("Replaced roots must be rejected before Git inspection")

    monkeypatch.setattr(registry_service, "inspect_git", unexpected_git)

    def assert_rejected(active_registry, active_index, active_sessions):
        with pytest.raises(UnsafeProjectPath):
            active_registry.inspect_project(project.id)
        with pytest.raises(UnsafeProjectPath):
            active_index.refresh_index(project.id)
        with pytest.raises(UnsafeProjectPath):
            active_index.get_index_status(project.id)
        tools = RepositoryTools(active_registry)
        with pytest.raises(UnsafeProjectPath):
            tools.list_files(project.id)
        with pytest.raises(UnsafeProjectPath):
            tools.read_file(project.id, "replacement.py")
        assert active_index.render_project_map(project.id) == before_map
        assert one(active_index, project, "original")
        assert not active_index.find_symbol(project.id, "unauthorized_replacement")
        assert IndexRepository(active_sessions).read(project.id) == before

    assert_rejected(registry, index, sessions)
    engine, url = database
    engine.dispose()
    reopened = create_database_engine(url)
    try:
        sessions = create_session_factory(reopened)
        new_registry = ProjectRegistry(
            ProjectRepository(sessions), base_directory=tmp_path
        )
        new_index = ProjectIndex(new_registry, IndexRepository(sessions))
        assert_rejected(new_registry, new_index, sessions)
    finally:
        reopened.dispose()


@pytest.mark.posix
def test_legacy_identity_rejects_live_index_access_but_keeps_snapshot(
    indexed, registry, database, tmp_path, monkeypatch
):
    index, project, root = indexed
    write(root, "module.py", "def cached(): pass\n")
    index.refresh_index(project.id)
    before = index.render_project_map(project.id)
    sessions = create_session_factory(database[0])
    with sessions.begin() as session:
        record = session.get(ProjectRecord, project.id)
        record.root_device = record.root_inode = None
    write(root, "new.py", "def must_not_be_authorized(): pass\n")

    def unexpected_git(*args):
        pytest.fail("Legacy registration must not authorize Git inspection")

    monkeypatch.setattr(registry_service, "inspect_git", unexpected_git)
    # Reopen the database so no in-memory registration identity can hide NULLs.
    engine, url = database
    engine.dispose()
    reopened = create_database_engine(url)
    try:
        sessions = create_session_factory(reopened)
        registry = ProjectRegistry(ProjectRepository(sessions), base_directory=tmp_path)
        index = ProjectIndex(registry, IndexRepository(sessions))
        for operation in (
            registry.inspect_project,
            index.refresh_index,
            index.get_index_status,
            RepositoryTools(registry).list_files,
        ):
            with pytest.raises(UnsafeProjectPath, match="re-registered"):
                operation(project.id)
        assert index.render_project_map(project.id) == before
        assert one(index, project, "cached")
        assert not index.find_symbol(project.id, "must_not_be_authorized")
    finally:
        reopened.dispose()


@pytest.mark.posix
def test_root_replacement_during_refresh_rolls_back(
    indexed, database, tmp_path, monkeypatch
):
    index, project, root = indexed
    source = write(root, "module.py", "def original(): pass\n")
    index.refresh_index(project.id)
    repository = IndexRepository(create_session_factory(database[0]))
    before = repository.read(project.id)
    source.write_text("def changed(): pass\n")
    original_parse = service.parse_python

    def replacing_parse(path, content):
        root.rename(tmp_path / "moved-during-refresh")
        root.mkdir()
        write(root, "replacement.py", "def unauthorized_replacement(): pass\n")
        return original_parse(path, content)

    monkeypatch.setattr(service, "parse_python", replacing_parse)
    with pytest.raises(UnsafeProjectPath):
        index.refresh_index(project.id)
    assert repository.read(project.id) == before
    assert one(index, project, "original")
    assert not index.find_symbol(project.id, "changed")
    assert not index.find_symbol(project.id, "unauthorized_replacement")


@pytest.mark.posix
def test_file_replaced_by_symlink_between_stat_and_open(indexed, tmp_path, monkeypatch):
    index, project, root = indexed
    file = write(root, "module.py", "def original(): pass\n")
    index.refresh_index(project.id)
    outside = write(tmp_path, "outside.py", "def outside_secret(): pass\n")
    original_open = filesystem.os.open

    def racing_open(path, flags, *args, **kwargs):
        if path == "module.py":
            file.unlink()
            file.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    # Capability detection still refers to the same function after monkeypatch.
    monkeypatch.setattr(
        filesystem.os, "supports_dir_fd", filesystem.os.supports_dir_fd | {racing_open}
    )
    monkeypatch.setattr(filesystem.os, "open", racing_open)
    with pytest.raises(IndexRefreshError):
        index.refresh_index(project.id)
    assert one(index, project, "original")
    assert not index.find_symbol(project.id, "outside_secret")


def test_database_reopen_and_project_removal_cleanup(indexed, database, tmp_path):
    index, project, root = indexed
    write(root, "module.py", "def persisted(): pass\n")
    index.refresh_index(project.id)
    before = index.render_project_map(project.id)
    engine, url = database
    engine.dispose()
    reopened = create_database_engine(url)
    try:
        sessions = create_session_factory(reopened)
        new_registry = ProjectRegistry(
            ProjectRepository(sessions), base_directory=tmp_path
        )
        new_index = ProjectIndex(new_registry, IndexRepository(sessions))
        assert new_index.render_project_map(project.id) == before
        assert not new_index.get_index_status(project.id).needs_refresh
        new_registry.remove_project(project.id)
        with sessions() as session:
            for model in [
                IndexStateRecord,
                IndexedFileRecord,
                SymbolRecord,
                RelationshipRecord,
            ]:
                assert session.scalar(select(func.count()).select_from(model)) == 0
        assert (root / "module.py").is_file()
        with pytest.raises(ProjectNotFound):
            new_index.find_symbol(project.id, "persisted")
    finally:
        reopened.dispose()


def test_symbol_ids_are_project_scoped_and_ambiguity_is_preserved(
    indexed, registry, tmp_path
):
    index, project, root = indexed
    write(root, "same.py", "def run(): pass\ndef run(): pass\n")
    index.refresh_index(project.id)
    matches = index.find_symbol(project.id, "same.run")
    assert len(matches) == 2 and matches[0].id != matches[1].id
    other_root = tmp_path / "other"
    write(other_root, "same.py", "def run(): pass\ndef run(): pass\n")
    other = registry.register_project("Other", other_root)
    index.refresh_index(other.id)
    assert index.find_symbol(other.id, "run") == matches
    assert index.get_symbol(other.id, matches[0].id) == matches[0]
    for operation in [
        index.get_symbol,
        index.get_dependencies,
        index.get_dependents,
        index.get_related_symbols,
    ]:
        with pytest.raises(SymbolNotFound):
            operation(project.id, "missing")
    with pytest.raises(ProjectNotFound):
        index.refresh_index(uuid4())


def test_duplicate_module_names_do_not_produce_guessed_imports(indexed):
    index, project, root = indexed
    write(root, "pkg.py", "class Thing: pass\n")
    write(root, "pkg/__init__.py", "class Thing: pass\n")
    write(root, "client.py", "import pkg\nfrom pkg import Thing\n")
    index.refresh_index(project.id)
    assert len(index.find_symbol(project.id, "pkg.Thing")) == 2
    assert all(
        not e.target_id
        for e in index.get_relationships(project.id)
        if e.kind == "imports"
    )


def test_focused_map_prioritizes_matches_and_neighbors(indexed):
    index, project, root = indexed
    write(root, "aaa.py", "def irrelevant(): pass\n")
    write(
        root,
        "assessment/service.py",
        """def helper(): pass
class AssessmentService:
    def calculate(self):
        helper()
    def validate(self): pass
""",
    )
    write(root, "client.py", "from assessment.service import AssessmentService\n")
    index.refresh_index(project.id)
    focused = index.render_project_map(
        project.id, focus="AssessmentService", max_tokens=80
    )
    assert focused.startswith("assessment/service.py\n  class AssessmentService")
    assert "irrelevant" not in focused
    assert "calculate()" in focused
    assert "client.py" in focused
    assert index.render_project_map(project.id, focus="nothing matches") == ""
    assert focused == index.render_project_map(
        project.id, focus="AssessmentService", max_tokens=80
    )
    general = index.render_project_map(project.id, max_tokens=80)
    assert general.startswith("assessment/service.py")


@pytest.mark.parametrize("budget", [0, 1, 2, 5, 10, 25, 60, 200, 3000])
def test_map_is_bounded_for_large_repositories_and_tiny_budgets(indexed, budget):
    index, project, root = indexed
    # Many definitions, still only synthetic files inside this temporary project.
    for number in range(10):
        write(
            root,
            f"file_{number}.py",
            "\n".join(f"def function_{n}(): pass" for n in range(100)),
        )
    write(root, "unicode.py", "def café(): pass\n")
    index.refresh_index(project.id)
    result = index.render_project_map(project.id, max_tokens=budget)
    assert approximate_tokens(result) <= budget
    assert result == "" or result.endswith("\n")
    if budget <= 2:
        assert result == ""
    if budget == 3000:
        assert "function_" in result
        assert "pass" not in result


@pytest.mark.parametrize("budget", [-1, 1.5, True])
def test_invalid_budget_is_rejected(indexed, budget):
    index, project, _ = indexed
    with pytest.raises(ValueError):
        index.render_project_map(project.id, max_tokens=budget)


def test_new_migration_follows_registry_and_preserves_registration(
    database, registry, tmp_path
):
    engine, _ = database
    project = registry.register_project("Keep", tmp_path)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0001_projects")
        assert "projects" in inspect(connection).get_table_names()
        assert "indexed_files" not in inspect(connection).get_table_names()
        command.upgrade(config, "head")
        command.check(config)
    # Downgrading to 0001_projects drops the later directory identity. The stable
    # registration survives; safe repository tools require re-registration.
    restored = registry.get_project(project.id)
    assert (restored.id, restored.name, restored.root_path, restored.created_at) == (
        project.id,
        project.name,
        project.root_path,
        project.created_at,
    )
    assert restored.root_device is None
    assert restored.root_inode is None


def test_database_errors_are_translated(indexed, database):
    index, project, root = indexed
    write(root, "module.py", "def helper(): pass\n")
    SymbolRecord.__table__.drop(database[0])
    with pytest.raises(IndexStorageError) as error:
        index.refresh_index(project.id)
    assert "SELECT" not in str(error.value)
    assert error.value.__suppress_context__


def test_python_encoding_cookies(indexed):
    index, project, root = indexed
    (root / "latin.py").write_bytes(
        "# coding: latin-1\ndef café(): pass\n".encode("latin-1")
    )
    assert not index.refresh_index(project.id).failures
    assert one(index, project, "café")


@pytest.mark.parametrize(
    "target_source, import_statement",
    [
        ("class Thing: pass\nThing = something_else\n", "from target import Thing"),
        ("if condition:\n    class Thing: pass\n", "from target import Thing"),
        ("class C:\n    def method(self): pass\n", "from target.C import method"),
        ("@decorator\nclass Thing: pass\n", "from target import Thing"),
    ],
)
def test_uncertain_import_targets_remain_textual(
    indexed, target_source, import_statement
):
    index, project, root = indexed
    write(root, "target.py", target_source)
    write(root, "client.py", import_statement + "\n")
    index.refresh_index(project.id)
    assert all(
        e.target_id is None
        for e in index.get_relationships(project.id)
        if e.kind == "imports"
    )


@pytest.mark.parametrize(
    "source",
    [
        "class C(Base):\n    def helper(self): pass\n"
        "    def run(self): self.helper()\n",
        "@decorator\nclass C:\n    def helper(self): pass\n"
        "    def run(self): self.helper()\n",
        "class C(metaclass=Meta):\n    def helper(self): pass\n"
        "    def run(self): self.helper()\n",
        "class C:\n    def helper(self): pass\n"
        "    def __getattribute__(self, name): pass\n"
        "    def run(self): self.helper()\n",
        "class C:\n    def helper(self): pass\n    def run(self):\n"
        "        try: pass\n        except Error as self: self.helper()\n",
        "class C:\n    def helper(self): pass\n    def run(self):\n"
        "        match value:\n            case self: self.helper()\n",
    ],
)
def test_uncertain_receiver_dispatch_is_not_linked(indexed, source):
    index, project, root = indexed
    write(root, "module.py", source)
    index.refresh_index(project.id)
    assert not [
        e
        for e in index.get_relationships(project.id)
        if e.kind == "calls" and e.target_id
    ]


def test_map_preserves_class_blocks_when_ranking_interleaves_symbols(indexed):
    index, project, root = indexed
    write(
        root,
        "classes.py",
        """class First:
    def first_method(self): pass
class Second:
    def second_method(self): pass
""",
    )
    index.refresh_index(project.id)
    assert index.render_project_map(project.id) == (
        "classes.py\n"
        "  class First :1\n"
        "    method first_method() :2\n"
        "  class Second :3\n"
        "    method second_method() :4\n"
    )
    assert index.render_project_map(project.id, focus="second_method") == (
        "classes.py\n  class Second :3\n    method second_method() :4\n"
    )


@pytest.mark.posix
def test_directory_replacement_during_scan_rolls_back(indexed, tmp_path, monkeypatch):
    index, project, root = indexed
    write(root, "sub/source.py", "def original(): pass\n")
    index.refresh_index(project.id)
    previous = index.render_project_map(project.id)
    original_parse = service.parse_python
    write(root, "sub/new.py", "def new(): pass\n")

    def move_during_parse(path, content):
        if path == "sub/new.py":
            (root / "sub").rename(tmp_path / "moved_outside")
            (root / "sub").mkdir()
        return original_parse(path, content)

    monkeypatch.setattr(service, "parse_python", move_during_parse)
    with pytest.raises(IndexRefreshError):
        index.refresh_index(project.id)
    assert index.render_project_map(project.id) == previous


@pytest.mark.posix
def test_source_changed_during_read_aborts_refresh(indexed, monkeypatch):
    index, project, root = indexed
    write(root, "source.py", "def original(): pass\n")
    index.refresh_index(project.id)
    previous = index.render_project_map(project.id)
    original_fstat = filesystem.os.fstat
    file_stat_calls = 0

    def racing_fstat(fd):
        nonlocal file_stat_calls
        result = original_fstat(fd)
        if result.st_mode & 0o170000 == 0o100000:
            file_stat_calls += 1
            if file_stat_calls == 2:
                (root / "source.py").write_text("def changed_during_read(): pass\n")
                return original_fstat(fd)
        return result

    monkeypatch.setattr(filesystem.os, "fstat", racing_fstat)
    with pytest.raises(IndexRefreshError):
        index.refresh_index(project.id)
    assert index.render_project_map(project.id) == previous


def test_source_is_never_executed(indexed):
    index, project, root = indexed
    write(
        root,
        "untrusted.py",
        "raise AssertionError('must not execute')\ndef known(): pass\n",
    )
    assert not index.refresh_index(project.id).needs_refresh
    assert one(index, project, "known")


def test_relative_imports_from_project_root_initializer(indexed):
    index, project, root = indexed
    write(
        root,
        "__init__.py",
        "from .module import Thing\nfrom . import module\ndef root_helper(): pass\n",
    )
    write(root, "module.py", "class Thing: pass\n")
    write(root, "client.py", "import __root__\nfrom __root__ import root_helper\n")
    index.refresh_index(project.id)
    imports = [e for e in index.get_relationships(project.id) if e.kind == "imports"]
    assert {e.target_text for e in imports if e.target_id} == {
        "from .module import Thing",
        "from . import module",
    }
    assert (
        next(e for e in imports if e.target_text == "import __root__").target_id is None
    )
