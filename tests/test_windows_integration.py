"""Actual native Windows/NTFS integration. Never emulated on POSIX.

Run: python -m pytest -m windows -rs
Some symlink tests additionally need Windows Developer Mode or link privilege.
"""

import os
import subprocess
from dataclasses import replace

import pytest

from agentforge.db.database import create_session_factory
from agentforge.db.projects import ProjectRepository
from agentforge.projects.errors import InvalidProjectPath, UnsafeProjectPath
from agentforge.projects.identity import RootIdentity
from agentforge.projects.service import ProjectRegistry
from agentforge.tools.errors import SensitivePath
from agentforge.tools.service import RepositoryTools
from tests.test_index import indexed as index_fixture
from tests.test_index import (
    test_symbols_locations_containment_and_nested_identity as index_contract,
)
from tests.test_mcp import setup as mcp_fixture
from tests.test_mcp import (
    test_sdk_repo_explorer_result_reasoning_privacy_and_multiworker as mcp_contract,
)
from tests.test_repository_tools import git_tools as git_tools_fixture
from tests.test_repository_tools import local_git as git_fixture
from tests.test_repository_tools import (
    test_complete_read_line_range_empty_and_utf8_budget as read_contract,
)
from tests.test_repository_tools import (
    test_git_diffs_staged_unstaged_deleted_binary_and_limits as diff_contract,
)
from tests.test_repository_tools import (
    test_git_sensitivity_and_rename_across_boundary as rename_contract,
)
from tests.test_repository_tools import (
    test_git_subproject_never_exposes_siblings as git_boundary_contract,
)
from tests.test_repository_tools import (
    test_list_order_scope_and_limits as list_contract,
)
from tests.test_repository_tools import (
    test_literal_search_case_scope_order_and_budgets as search_contract,
)
from tests.test_repository_tools import tools as tools_fixture

pytestmark = pytest.mark.windows
# Execute the SAME high-level contracts with the selected native backend.
tools, git_tools, local_git, setup = (
    tools_fixture,
    git_tools_fixture,
    git_fixture,
    mcp_fixture,
)
test_native_list_contract = list_contract
test_native_read_contract = read_contract
test_native_search_contract = search_contract
test_native_git_diff_contract = diff_contract
test_native_git_subproject_contract = git_boundary_contract
test_native_cross_boundary_rename_contract = rename_contract
test_native_mcp_remote_worker_contract = mcp_contract
indexed = index_fixture
test_native_index_contract = index_contract


def test_native_root_identity_and_replacement(tools, registry, database, tmp_path):
    _, project_id, root = tools
    project = registry.get_project(project_id)
    assert project.root_identity.kind == "windows"
    assert len(project.root_identity.volume) == 16
    assert len(project.root_identity.file_id) == 32
    reopened = ProjectRegistry(
        ProjectRepository(create_session_factory(database[0])), base_directory=tmp_path
    )
    assert reopened.get_project(project_id).root_identity == project.root_identity
    root.rename(tmp_path / "old-root")
    root.mkdir()
    with pytest.raises(UnsafeProjectPath):
        RepositoryTools(reopened).list_files(project_id)


def test_native_identity_mismatch(tools, registry):
    service, project_id, _ = tools
    project = registry.get_project(project_id)
    registry.remove_project(project_id)
    registry._repository.add(
        replace(project, root_identity=RootIdentity("windows", "0" * 16, "0" * 32))
    )
    with pytest.raises(UnsafeProjectPath):
        service.list_files(project_id)


def test_native_handles_prevent_root_directory_file_replacement(
    tools, registry, tmp_path
):
    _, project_id, root = tools
    (root / "sub").mkdir()
    target = root / "sub" / "source.py"
    target.write_bytes(b"safe\n")
    backend = registry.filesystem
    with registry.open_root(project_id) as (_, node):
        with pytest.raises(OSError):
            root.rename(tmp_path / "moved")
        with backend.directory(node, ("sub",)) as sub:
            with pytest.raises(OSError):
                (root / "sub").rename(root / "moved-sub")
            with backend.regular_file(sub, "source.py") as file:
                with pytest.raises(OSError):
                    target.unlink()
                with pytest.raises(OSError):
                    target.write_bytes(b"replacement")
                assert file.read() == b"safe\n"
    target.write_bytes(b"allowed after read\n")


def test_native_replacement_between_observation_and_open(tools, registry, monkeypatch):
    service, project_id, root = tools
    target = root / "source.py"
    target.write_bytes(b"safe\n")
    api = registry.filesystem.api
    original = api.open
    count = 0

    def swap(path):
        nonlocal count
        if path.lower().endswith("source.py"):
            count += 1
            if count == 2:
                target.rename(root / "old.py")
                target.write_bytes(b"replacement\n")
        return original(path)

    monkeypatch.setattr(api, "open", swap)
    with pytest.raises(UnsafeProjectPath):
        service.read_file(project_id, "source.py")


def test_native_case_ads_and_sensitive_policy(tools):
    service, project_id, root = tools
    (root / "Nested").mkdir()
    (root / "Nested" / "Source.py").write_bytes(b"needle\n")
    (root / ".ENV").write_bytes(b"PRIVATE\n")
    assert service.read_file(project_id, "nested/source.py").content == "needle\n"
    (root / "Nested" / "Source.py:secret").write_bytes(b"ADS PRIVATE\n")
    assert service.list_files(project_id).paths == ("Nested/Source.py",)
    with pytest.raises(UnsafeProjectPath):
        service.read_file(project_id, "Nested/Source.py:secret")
    with pytest.raises(SensitivePath):
        service.read_file(project_id, ".env")


@pytest.mark.parametrize("directory", [False, True])
def test_native_symlink_escape(tools, tmp_path, directory):
    service, project_id, root = tools
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_bytes(b"OUTSIDE SECRET\n")
    link = root / "escape"
    try:
        link.symlink_to(
            outside if directory else outside / "secret.py",
            target_is_directory=directory,
        )
    except OSError:
        pytest.skip(
            "Windows symlink creation requires Developer Mode or link privilege"
        )
    with pytest.raises(UnsafeProjectPath):
        service.read_file(project_id, "escape/secret.py" if directory else "escape")
    assert service.list_files(project_id).paths == ()


def test_native_junction_escape_and_registration(tools, registry, tmp_path):
    service, project_id, root = tools
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_bytes(b"OUTSIDE SECRET\n")
    junction = root / "junction"
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
        shell=False,
        check=True,
        capture_output=True,
        timeout=5,
    )
    try:
        with pytest.raises(UnsafeProjectPath):
            service.read_file(project_id, "junction/secret.py")
        assert service.list_files(project_id).paths == ()
        with pytest.raises(InvalidProjectPath):
            registry.register_project("Junction", junction)
    finally:
        os.rmdir(junction)


def test_native_git_snapshot_is_pinned_and_readonly(git_tools, registry, monkeypatch):
    service, project_id, root = git_tools
    from agentforge.tools import git_backend

    original = git_backend.subprocess.Popen
    invocations = []
    before = {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }

    def checked(args, **kwargs):
        assert kwargs["shell"] is False and kwargs["close_fds"] is True
        assert "pass_fds" not in kwargs
        assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == ""
        cwd = args[args.index("-C") + 1]
        from pathlib import Path

        scratch = Path(cwd)
        assert scratch != root
        with pytest.raises(OSError):
            (scratch / ".git" / "config").write_text("hostile")
        with pytest.raises(OSError):
            scratch.rename(scratch.with_name("replaced"))
        invocations.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(git_backend.subprocess, "Popen", checked)
    service.git_status(project_id)
    service.git_grep(project_id, "needle")
    assert invocations
    assert before == {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
