"""Deterministic Windows authorization tests; these are NOT native integration."""

import ctypes
import ntpath
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config

from agentforge.db.database import create_session_factory
from agentforge.db.index import IndexRepository
from agentforge.db.projects import ProjectRepository
from agentforge.index.models import IndexRefreshError
from agentforge.index.service import ProjectIndex
from agentforge.projects import win32, windows
from agentforge.projects.errors import InvalidProjectPath, UnsafeProjectPath
from agentforge.projects.identity import RootIdentity
from agentforge.projects.service import ProjectRegistry
from agentforge.projects.win32 import ObjectInfo
from agentforge.projects.windows import (
    WindowsSafeFilesystemBackend,
    drive_path,
    ordinary_final,
    validate_windows_parts,
)
from agentforge.tools.errors import SensitivePath
from agentforge.tools.policy import FILE_SCAN_BYTES, relative_path
from agentforge.tools.service import RepositoryTools


class FakeWin32:
    """Identity-bearing open objects; hooks inject races independently of paths."""

    def __init__(self):
        self.nodes, self.handles, self.positions = {}, {}, {}
        self.opened, self.closed, self.read_paths = [], [], []
        self.serial = 0
        self.on_open = lambda path: None
        self.on_read = lambda node: None
        self.supported = True
        self.case_sensitive = set()
        self.add("C:\\", directory=True)

    def add(self, path, content=b"", *, directory=False, reparse=False, links=1):
        self.serial += 1
        node = SimpleNamespace(
            path=path,
            content=content,
            info=ObjectInfo(
                RootIdentity("windows", "0000000000000001", f"{self.serial:032x}"),
                (0x10 if directory else 0) | (0x400 if reparse else 0),
                len(content),
                links=links,
            ),
        )
        self.nodes[ntpath.normcase(path)] = node
        return node

    def open(self, path):
        assert path.startswith("\\\\?\\")
        path = path[4:]
        self.on_open(path)
        node = self.nodes.get(ntpath.normcase(path))
        if node is None:
            raise FileNotFoundError(path)
        handle = max(self.handles, default=0) + 1
        self.handles[handle] = node
        self.positions[handle] = 0
        self.opened.append(path)
        return handle

    def close(self, handle):
        assert handle not in self.closed
        self.closed.append(handle)

    def info(self, handle):
        return self.handles[handle].info

    def final_path(self, handle):
        return "\\\\?\\" + self.handles[handle].path

    def same_path(self, a, b):
        return a.lower() == b.lower()  # Fixtures use ASCII; native uses ordinal API.

    def require_supported_volume(self, handle, anchor):
        if not self.supported:
            raise UnsafeProjectPath("Unsupported fake filesystem")

    def require_case_insensitive(self, handle):
        if self.handles[handle].path.lower() in self.case_sensitive:
            raise UnsafeProjectPath("Unsupported fake case-sensitive directory")

    def read(self, handle, size):
        node = self.handles[handle]
        self.on_read(node)
        self.read_paths.append(node.path)
        offset = self.positions[handle]
        data = node.content[offset : offset + size]
        self.positions[handle] += len(data)
        return data

    def scandir(self, path):
        parent = ntpath.normcase(str(path)[4:])
        names = [
            SimpleNamespace(name=ntpath.basename(node.path))
            for key, node in self.nodes.items()
            if ntpath.normcase(ntpath.dirname(key)) == parent and key != parent
        ]

        class Entries:
            def __enter__(self):
                return iter(names)

            def __exit__(self, *args):
                pass

        return Entries()


@pytest.fixture
def mocked_windows(database, monkeypatch):
    api = FakeWin32()
    api.add(r"C:\Repo", directory=True)
    backend = WindowsSafeFilesystemBackend(api)
    original = os.scandir
    monkeypatch.setattr(
        windows.os,
        "scandir",
        lambda path: (
            api.scandir(path) if str(path).startswith("\\\\?\\") else original(path)
        ),
    )
    monkeypatch.setattr(os, "supports_fd", os.supports_fd | {os.scandir})
    registry = ProjectRegistry(
        ProjectRepository(create_session_factory(database[0])),
        base_directory="C:\\",
        filesystem=backend,
    )
    project = registry.register_project("Mock Windows", r"C:\Repo")
    return SimpleNamespace(
        api=api,
        backend=backend,
        registry=registry,
        project=project,
        tools=RepositoryTools(registry),
    )


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "sub/../../escape",
        "/outside",
        "C:/outside",
        "D:/outside",
        "C:outside",
        r"\\server\share\file",
        r"\\?\C:\outside",
        r"\??\C:\outside",
        r"sub\file",
        "source.py:secret",
        "source.py::$DATA",
        "a/.. /file",
        "NUL",
        "NUL.txt",
        "COM1.txt",
        "LPT¹",
        "CON .py",
        "file.",
        "file ",
        "a/aux/file",
        "bad*name",
        "bad?name",
        'bad"name',
        "a\x00b",
    ],
)
def test_windows_tool_path_validation(mocked_windows, path):
    setup = mocked_windows
    for call in (
        lambda: setup.tools.read_file(setup.project.id, path),
        lambda: setup.tools.list_files(setup.project.id, path),
        lambda: setup.tools.search_code(setup.project.id, "needle", path=path),
        lambda: setup.tools.git_grep(setup.project.id, "needle", path=path),
        lambda: setup.tools.git_status(setup.project.id, path=path),
        lambda: setup.tools.git_diff(setup.project.id, path=path),
    ):
        with pytest.raises(UnsafeProjectPath):
            call()


@pytest.mark.parametrize(
    "path",
    [
        r"\\server\share",
        r"\\?\C:\Repo",
        r"\\.\C:\Repo",
        r"\??\C:\Repo",
        r"\Repo",
        r"C:Repo",
        "C:/Repo/..",
        "C:/Repo.",
        "C:/Repo ",
        "C:/Repo:stream",
    ],
)
def test_windows_root_validation_before_normalization(path):
    with pytest.raises(UnsafeProjectPath):
        drive_path(path)


def test_windows_normal_drive_paths_and_ordinal_boundary(mocked_windows):
    setup = mocked_windows
    assert drive_path("C:/Repo/sub") == r"C:\Repo\sub"
    assert ordinary_final(r"\\?\C:\Repo") == r"C:\Repo"
    setup.api.add(r"C:\Repo\Nested", directory=True)
    setup.api.add(r"C:\Repo\Nested\Source.py", b"needle\n")
    assert (
        setup.tools.read_file(setup.project.id, "nested/source.py").content
        == "needle\n"
    )
    assert setup.tools.list_files(setup.project.id).paths == ("Nested/Source.py",)
    assert (
        setup.tools.search_code(setup.project.id, "needle").matches[0].path
        == "Nested/Source.py"
    )
    assert setup.project.root_device is setup.project.root_inode is None
    assert (
        setup.registry.get_project(setup.project.id).root_identity
        == setup.project.root_identity
    )
    assert not setup.backend.same_component("allowed", "allowed-secret")
    assert not setup.backend.same_component("straße", "strasse")


@pytest.mark.parametrize("directory", [False, True])
def test_reparse_denied_without_read_and_skipped_in_scan(mocked_windows, directory):
    setup = mocked_windows
    setup.api.add(
        r"C:\Repo\escape", b"OUTSIDE SECRET", directory=directory, reparse=True
    )
    setup.api.add(r"C:\Repo\safe.py", b"def safe(): pass\n")
    path = "escape/secret.py" if directory else "escape"
    with pytest.raises(UnsafeProjectPath):
        setup.tools.read_file(setup.project.id, path)
    assert setup.tools.list_files(setup.project.id).paths == ("safe.py",)
    assert setup.tools.search_code(setup.project.id, "SECRET").matches == ()
    assert not any("escape" in p for p in setup.api.read_paths)


def test_windows_index_uses_same_handles_and_policy(mocked_windows, database):
    setup = mocked_windows
    for name in (".ENV.py", "private.KEY", "NUL.py", "file.py:stream"):
        setup.api.add("C:\\Repo\\" + name, b"def secret(): pass\n")
    setup.api.add(r"C:\Repo\BUILD", directory=True)
    setup.api.add(r"C:\Repo\BUILD\hidden.py", b"def hidden(): pass\n")
    setup.api.add(r"C:\Repo\safe.py", b"def safe(): pass\n")
    setup.api.add(r"C:\Repo\alias.py", b"def escaped(): pass\n", reparse=True)
    index = ProjectIndex(
        setup.registry, IndexRepository(create_session_factory(database[0]))
    )
    assert index.refresh_index(setup.project.id).file_count == 1
    assert index.find_symbol(setup.project.id, "safe")
    assert not index.find_symbol(setup.project.id, "secret")
    assert setup.tools.list_files(setup.project.id, "BUILD").paths == ()
    assert setup.tools.read_file(setup.project.id, "BUILD/hidden.py").content
    with pytest.raises(SensitivePath):
        setup.tools.read_file(setup.project.id, ".ENV.py")
    assert all(
        p.lower().endswith(("safe.py", "hidden.py")) for p in setup.api.read_paths
    )


@pytest.mark.parametrize(
    "kind",
    ["replacement", "wrong_identity", "reparse", "unsupported", "case_sensitive"],
)
def test_windows_roots_fail_closed(mocked_windows, kind):
    setup = mocked_windows
    if kind == "replacement":
        setup.api.add(r"C:\Repo", directory=True)
    elif kind == "wrong_identity":
        setup.registry._repository.remove(setup.project.id)
        setup.registry._repository.add(
            replace(
                setup.project, root_identity=RootIdentity("windows", "other", "other")
            )
        )
    elif kind == "reparse":
        node = setup.api.nodes[ntpath.normcase(r"C:\Repo")]
        node.info = replace(node.info, attributes=0x410)
    elif kind == "unsupported":
        setup.api.supported = False
    else:
        setup.api.case_sensitive.add(r"c:\repo")
    with pytest.raises(UnsafeProjectPath):
        setup.tools.list_files(setup.project.id)


def test_root_change_during_access_and_closed_capability(mocked_windows):
    setup = mocked_windows
    with pytest.raises(UnsafeProjectPath):
        with setup.registry.open_root(setup.project.id) as (_, node):
            setup.api.handles[node.handle].path = r"C:\Repo-moved"
    assert len(setup.api.closed) == len(setup.api.opened)
    with pytest.raises(UnsafeProjectPath):
        setup.backend.stat(node, "anything")


def test_windows_file_replacement_before_open_denied(mocked_windows):
    setup = mocked_windows
    setup.api.add(r"C:\Repo\source.py", b"safe\n")
    opened = 0

    def replace_on_open(path):
        nonlocal opened
        if path.lower().endswith("source.py"):
            opened += 1
            if opened == 2:
                setup.api.add(path, b"replacement\n")

    setup.api.on_open = replace_on_open
    with pytest.raises(UnsafeProjectPath, match="replaced"):
        setup.tools.read_file(setup.project.id, "source.py")
    assert not setup.api.read_paths


def test_windows_read_mutation_detected_and_index_rolls_back(mocked_windows, database):
    setup = mocked_windows
    setup.api.add(r"C:\Repo\source.py", b"def original(): pass\n")
    index = ProjectIndex(
        setup.registry, IndexRepository(create_session_factory(database[0]))
    )
    index.refresh_index(setup.project.id)
    previous = index.render_project_map(setup.project.id)

    def mutate(node):
        node.info = replace(node.info, written=node.info.written + 1)

    setup.api.on_read = mutate
    with pytest.raises(UnsafeProjectPath):
        setup.tools.read_file(setup.project.id, "source.py")
    with pytest.raises(IndexRefreshError):
        index.refresh_index(setup.project.id)
    assert index.render_project_map(setup.project.id) == previous


def test_directory_move_on_early_exit_is_checked(mocked_windows, monkeypatch):
    setup = mocked_windows
    sub = setup.api.add(r"C:\Repo\sub", directory=True)
    setup.api.add(r"C:\Repo\sub\source.py", b"needle\nneedle\n")
    from agentforge.tools import filesystem

    original = filesystem.read_bytes

    def move_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        sub.path = r"C:\moved"
        return result

    monkeypatch.setattr(filesystem, "read_bytes", move_after_read)
    with pytest.raises(UnsafeProjectPath):
        setup.tools.search_code(setup.project.id, "needle", max_results=1)


def test_windows_hardlinks_and_volume_mismatch_denied(mocked_windows):
    setup = mocked_windows
    setup.api.add(r"C:\Repo\hardlink.py", b"secret", links=2)
    with pytest.raises(UnsafeProjectPath):
        setup.tools.read_file(setup.project.id, "hardlink.py")
    node = setup.api.add(r"C:\Repo\mount", directory=True)
    node.info = replace(
        node.info, identity=RootIdentity("windows", "other_volume", "id")
    )
    with pytest.raises(UnsafeProjectPath):
        setup.tools.list_files(setup.project.id, "mount")


def test_windows_8dot3_alias_denied(mocked_windows):
    setup = mocked_windows
    node = setup.api.add(r"C:\Repo\short.py", b"text")
    node.path = r"C:\Repo\ActualLongName.py"
    with pytest.raises(UnsafeProjectPath):
        setup.tools.read_file(setup.project.id, "short.py")


@pytest.mark.parametrize("backend_kind", ["posix", "windows_mock"])
def test_common_repository_and_index_contract(
    backend_kind, registry, mocked_windows, tmp_path, database
):
    if backend_kind == "posix":
        if os.name == "nt":
            pytest.skip("POSIX descriptor contract requires POSIX")
        root = tmp_path / "common"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "source.py").write_bytes(b"def needle(): pass\n")
        (root / ".env").write_bytes(b"SECRET")
        project = registry.register_project("Common", root)
        tools = RepositoryTools(registry)
    else:
        setup = mocked_windows
        setup.api.add(r"C:\Repo\sub", directory=True)
        setup.api.add(r"C:\Repo\sub\source.py", b"def needle(): pass\n")
        setup.api.add(r"C:\Repo\.env", b"SECRET")
        registry, project, tools = setup.registry, setup.project, setup.tools
    assert tools.list_files(project.id).paths == ("sub/source.py",)
    assert (
        tools.read_file(project.id, "sub/source.py").content == "def needle(): pass\n"
    )
    assert tools.search_code(project.id, "needle").matches[0].line_number == 1
    with pytest.raises(UnsafeProjectPath):
        tools.read_file(project.id, "../outside")
    with pytest.raises(SensitivePath):
        tools.read_file(project.id, ".env")
    index = ProjectIndex(registry, IndexRepository(create_session_factory(database[0])))
    assert index.refresh_index(project.id).file_count == 1
    assert index.find_symbol(project.id, "needle")


def test_index_oversized_source_rolls_back(mocked_windows, database):
    setup = mocked_windows
    source = setup.api.add(r"C:\Repo\source.py", b"def original(): pass\n")
    index = ProjectIndex(
        setup.registry, IndexRepository(create_session_factory(database[0]))
    )
    index.refresh_index(setup.project.id)
    previous = index.render_project_map(setup.project.id)
    source.content = b"#" * (FILE_SCAN_BYTES + 1)
    source.info = replace(source.info, size=len(source.content))
    with pytest.raises(IndexRefreshError, match="scan limit"):
        index.refresh_index(setup.project.id)
    assert index.render_project_map(setup.project.id) == previous


def test_identity_json_version_and_platform_fail_closed(mocked_windows):
    identity = mocked_windows.project.root_identity
    assert RootIdentity.from_json(identity.as_json()) == identity
    for invalid in (
        {},
        {**identity.as_json(), "version": 2},
        {**identity.as_json(), "kind": "other"},
    ):
        with pytest.raises(UnsafeProjectPath):
            RootIdentity.from_json(invalid)
    with pytest.raises(UnsafeProjectPath):
        with mocked_windows.backend.anchored_root(
            r"C:\Repo", RootIdentity("posix", "1", "2")
        ):
            pass


def test_wrapper_flags_layout_and_128bit_identity(monkeypatch):
    calls = []

    class Function:
        def __init__(self, name):
            self.name = name

        def __call__(self, *args):
            calls.append((self.name, args))
            if self.name == "CreateFileW":
                return 2**40 + 10  # Ensure handles are not truncated to 32 bits.
            if self.name == "GetFileType":
                return 1
            if self.name == "GetFileInformationByHandle":
                args[1]._obj.links = 1
            if self.name == "GetFileInformationByHandleEx" and args[1] == 18:
                info = args[2]._obj
                info.volume = 2**60 + 1
                info.file_id[15] = 128
            if self.name == "CompareStringOrdinal":
                return 2
            return 1

    dll = SimpleNamespace(
        **{
            name: Function(name)
            for name in (
                "CreateFileW",
                "CloseHandle",
                "GetFileType",
                "GetFileInformationByHandle",
                "GetFileInformationByHandleEx",
                "GetFinalPathNameByHandleW",
                "GetVolumeInformationByHandleW",
                "GetDriveTypeW",
                "CompareStringOrdinal",
                "ReadFile",
            )
        }
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: dll, raising=False)
    api = win32.Win32()
    handle = api.open(r"\\?\C:\Repo")
    info = api.info(handle)
    assert handle > 2**32
    assert info.identity.file_id == "00" * 15 + "80"
    assert info.identity.volume == f"{2**60 + 1:016x}"
    assert ctypes.sizeof(win32._HandleInfo) == 52
    assert ctypes.sizeof(win32._FileIdInfo) == 24
    assert dll.CreateFileW.restype is ctypes.c_void_p
    assert calls[0][1][1:6] == (0x80000000, 1, None, 3, 0x02200000)
    assert api.same_path("a", "A")
    assert calls[-1] == ("CompareStringOrdinal", ("a", -1, "A", -1, True))
    api.close(handle)


def test_windows_registration_reparse_is_invalid(mocked_windows):
    setup = mocked_windows
    setup.api.add(r"C:\Alias", directory=True, reparse=True)
    with pytest.raises(InvalidProjectPath):
        setup.registry.register_project("Alias", r"C:\Alias")
    with pytest.raises(UnsafeProjectPath):
        validate_windows_parts(relative_path("bad."))


def test_os_ordinal_sensitive_aliases_cannot_bypass_python_lower(mocked_windows):
    s = mocked_windows
    ordinary = s.api.same_path
    s.api.same_path = lambda a, b: ordinary(
        a.replace("ſ", "s").replace("ı", "i"), b.replace("ſ", "s").replace("ı", "i")
    )
    for path in ("credentialſ.json", ".ſsh/id_rsa", ".gıt/config"):
        with pytest.raises(SensitivePath):
            s.tools.read_file(s.project.id, path)
    assert not s.backend.automatic(("credentialſ.json",))
    assert s.backend.excluded_directory("dıst")
    assert not s.tools.public_index_path("credentialſ.json")


def test_windows_identity_migration_downgrade_never_reauthorizes(
    mocked_windows, database
):
    s = mocked_windows
    s.api.add(r"C:\Repo\source.py", b"def cached(): pass\n")
    index = ProjectIndex(
        s.registry, IndexRepository(create_session_factory(database[0]))
    )
    index.refresh_index(s.project.id)
    previous = index.render_project_map(s.project.id)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with database[0].begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0005_task_telemetry")
        command.upgrade(config, "head")
        command.check(config)
    restored = s.registry.get_project(s.project.id)
    assert restored.root_identity is restored.root_device is restored.root_inode is None
    with pytest.raises(UnsafeProjectPath, match="re-registered"):
        s.tools.list_files(s.project.id)
    assert index.render_project_map(s.project.id) == previous


def test_wrapper_identity_api_failure_has_no_pathname_fallback(monkeypatch):
    api = win32.Win32.__new__(win32.Win32)
    api.api = SimpleNamespace(
        GetFileType=lambda handle: 1,
        GetFileInformationByHandle=lambda *args: 1,
        GetFileInformationByHandleEx=lambda *args: 0,
    )

    def unavailable():
        raise OSError("Unsupported identity API")

    monkeypatch.setattr(api, "_error", unavailable)
    with pytest.raises(OSError):
        api.info(123)


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_index_observation_open_replacement_rolls_back(mocked_windows, database, kind):
    s = mocked_windows
    s.api.add(r"C:\Repo\sub", directory=True)
    s.api.add(r"C:\Repo\sub\source.py", b"def original(): pass\n")
    index = ProjectIndex(
        s.registry, IndexRepository(create_session_factory(database[0]))
    )
    index.refresh_index(s.project.id)
    previous = index.render_project_map(s.project.id)
    count = 0
    target = r"C:\Repo\sub\source.py" if kind == "file" else r"C:\Repo\sub"

    def swap(path):
        nonlocal count
        if path.lower() == target.lower():
            count += 1
            if count == (3 if kind == "file" else 2):
                s.api.add(
                    path, b"def replacement(): pass\n", directory=kind == "directory"
                )

    s.api.on_open = swap
    with pytest.raises(IndexRefreshError):
        index.refresh_index(s.project.id)
    assert index.render_project_map(s.project.id) == previous
