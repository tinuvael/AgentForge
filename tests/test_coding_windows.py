"""Deterministic mutation API adversaries plus native-Windows acceptance cases."""

import ctypes as c
import hashlib
import ntpath
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentforge.coding.filesystem import mutate
from agentforge.coding.models import EditConflict
from agentforge.coding.win32 import Win32Mutation
from agentforge.projects.errors import UnsafeProjectPath
from agentforge.projects.windows import WindowsSafeFilesystemBackend
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_windows_filesystem import FakeWin32

coding = coding_fixture


class MutationAPI(FakeWin32):
    def __init__(self):
        super().__init__()
        self.writes = []
        self.deleted = []

    def open_mutation(self, path, *, create=False, directory=False):
        normal = path[4:]
        if create:
            if ntpath.normcase(normal) in self.nodes:
                raise FileExistsError(normal)
            self.add(normal, directory=directory)
        return self.open(path)

    def rewrite(self, handle, data):
        node = self.handles[handle]
        self.writes.append((node.path, data))
        node.content = data
        node.info = replace(node.info, size=len(data), written=node.info.written + 1)

    def delete_handle(self, handle):
        node = self.handles[handle]
        self.deleted.append(node.path)
        del self.nodes[ntpath.normcase(node.path)]


@pytest.fixture
def native_fake():
    api = MutationAPI()
    api.add(r"C:\Workspace", directory=True)
    api.add(r"C:\Workspace\sub", directory=True)
    api.add(r"C:\Workspace\sub\source.py", b"old\n")
    backend = WindowsSafeFilesystemBackend(api)
    return SimpleNamespace(
        api=api,
        backend=backend,
        identity=api.nodes[ntpath.normcase(r"C:\Workspace")].info.identity,
    )


def change(
    setup,
    path="sub/source.py",
    *,
    content=b"new\n",
    expected=None,
    create=False,
    delete=False,
):
    with setup.backend.anchored_root(r"C:\Workspace", setup.identity) as root:
        mutate(
            root,
            path,
            content,
            expected=expected or hashlib.sha256(b"old\n").hexdigest(),
            create=create,
            delete=delete,
            check=lambda: setup.backend._verify(root),
        )


def test_mocked_windows_create_edit_delete_and_conflict(native_fake):
    setup = native_fake
    change(setup)
    assert (
        setup.api.nodes[ntpath.normcase(r"C:\Workspace\sub\source.py")].content
        == b"new\n"
    )
    with pytest.raises(EditConflict):
        change(setup)
    change(setup, "sub/new.py", create=True)
    with pytest.raises(EditConflict):
        change(setup, "sub/new.py", create=True)
    change(setup, expected=hashlib.sha256(b"new\n").hexdigest(), delete=True)
    assert setup.api.deleted == [r"C:\Workspace\sub\source.py"]
    assert len(setup.api.closed) == len(setup.api.opened)


@pytest.mark.parametrize(
    "path",
    [
        "C:/secret",
        "C:relative",
        r"\\server\share\file",
        r"\\?\C:\secret",
        r"\??\C:\secret",
        "file:ADS",
        "file.",
        "file ",
        "NUL",
        "AUX.txt",
        "COM1",
        "LPT¹",
        "../secret",
    ],
)
def test_mocked_windows_path_adversaries(native_fake, path):
    with pytest.raises(UnsafeProjectPath):
        change(native_fake, path, create=True)
    assert not native_fake.api.writes


@pytest.mark.parametrize(
    "kind",
    [
        "file_reparse",
        "junction",
        "hardlink",
        "root_replaced",
        "ancestor_moved",
        "root_reparse",
    ],
)
def test_mocked_windows_identity_and_links(native_fake, kind):
    setup = native_fake
    if kind == "file_reparse":
        node = setup.api.nodes[ntpath.normcase(r"C:\Workspace\sub\source.py")]
        node.info = replace(node.info, attributes=0x400)
    elif kind == "junction":
        node = setup.api.nodes[ntpath.normcase(r"C:\Workspace\sub")]
        node.info = replace(node.info, attributes=0x410)
    elif kind == "hardlink":
        node = setup.api.nodes[ntpath.normcase(r"C:\Workspace\sub\source.py")]
        node.info = replace(node.info, links=2)
    elif kind == "root_replaced":
        setup.api.add(r"C:\Workspace", directory=True)
    elif kind == "root_reparse":
        node = setup.api.nodes[ntpath.normcase(r"C:\Workspace")]
        node.info = replace(node.info, attributes=0x410)
    else:

        def moved(path):
            if path.lower().endswith("source.py"):
                setup.api.nodes[
                    ntpath.normcase(r"C:\Workspace\sub")
                ].path = r"C:\outside"

        setup.api.on_open = moved
    with pytest.raises(UnsafeProjectPath):
        change(setup)
    assert not setup.api.writes


def test_mutation_wrapper_flags_and_handle_operations(monkeypatch):
    calls = []

    class Function:
        def __init__(self, name):
            self.name = name

        def __call__(self, *args):
            calls.append((self.name, args))
            if self.name == "CreateFileW":
                return 2**40 + 42
            if self.name == "WriteFile":
                args[3]._obj.value = args[2]
            return 1

    names = (
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
        "WriteFile",
        "FlushFileBuffers",
        "SetFilePointerEx",
        "SetEndOfFile",
        "SetFileInformationByHandle",
    )
    dll = SimpleNamespace(**{name: Function(name) for name in names})
    monkeypatch.setattr(c, "WinDLL", lambda *args, **kwargs: dll, raising=False)
    api = Win32Mutation()
    handle = api.open_mutation(r"\\?\C:\Workspace\file", create=True)
    assert handle > 2**32
    assert calls[-1][1][1:6] == (0xC0010000, 1, None, 1, 0x02200000)
    api.rewrite(handle, b"data")
    assert [name for name, _ in calls[-4:]] == [
        "SetFilePointerEx",
        "SetEndOfFile",
        "WriteFile",
        "FlushFileBuffers",
    ]
    api.delete_handle(handle)
    assert calls[-1][0] == "SetFileInformationByHandle" and calls[-1][1][1] == 4
    assert dll.SetFileInformationByHandle.argtypes[0] is c.c_void_p


@pytest.mark.windows
def test_native_windows_secure_mutation(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_bytes(b"old\n")
    backend = WindowsSafeFilesystemBackend(Win32Mutation())
    identity = backend.observe_root(root)
    with backend.anchored_root(root, identity) as node:
        mutate(
            node,
            "file.txt",
            b"new\n",
            expected=hashlib.sha256(b"old\n").hexdigest(),
            check=lambda: backend._verify(node),
        )
        mutate(
            node,
            "created.txt",
            b"created\n",
            create=True,
            expected=None,
            check=lambda: backend._verify(node),
        )
    assert (root / "file.txt").read_bytes() == b"new\n"
    with backend.anchored_root(root, identity) as node:
        mutate(
            node,
            "created.txt",
            b"",
            delete=True,
            expected=hashlib.sha256(b"created\n").hexdigest(),
            check=lambda: backend._verify(node),
        )
    assert not (root / "created.txt").exists()


@pytest.mark.windows
def test_native_windows_hardlink_and_junction_write_denied(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(b"old\n")
    os.link(outside / "file", root / "hardlink")
    import subprocess

    # Fixture setup only; no shell API exists in AgentForge. cmd built-in creates
    # the NTFS junction for a real native boundary test.
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "junction"), str(outside)],
        check=True,
        capture_output=True,
    )
    backend = WindowsSafeFilesystemBackend(Win32Mutation())
    identity = backend.observe_root(root)
    for path in ("hardlink", "junction/file"):
        with backend.anchored_root(root, identity) as node:
            with pytest.raises(UnsafeProjectPath):
                mutate(
                    node,
                    path,
                    b"attack",
                    expected=hashlib.sha256(b"old\n").hexdigest(),
                    check=lambda: backend._verify(node),
                )
    assert (outside / "file").read_bytes() == b"old\n"


@pytest.mark.windows
def test_native_windows_task_worktree_lifecycle(coding):
    from tests.test_coding_workspaces import primary_state, sha, write

    before = primary_state(coding.root)
    setup = coding.create(subproject=True)
    write(setup, "a.py", "native edited\n", sha("allowed\n"))
    assert coding.manager.diff(coding.task.task_id).changed_files == ("a.py",)
    assert primary_state(coding.root) == before
    coding.task.state = "completed"
    coding.manager.finalize(coding.task)
    assert (
        coding.manager.cleanup(coding.task.task_id, coding.task.task_id).state
        == "removed"
    )
    assert primary_state(coding.root) == before
