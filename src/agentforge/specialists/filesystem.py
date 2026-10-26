"""Private flat store: opaque names, pinned roots, no-follow and atomic publish."""

import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from agentforge.projects.backends import select_backend
from agentforge.projects.errors import UnsafeProjectPath
from agentforge.specialists.models import SpecialistError


class PrivateFiles:
    def __init__(self, path):
        self.backend = select_backend()
        if os.name == "nt":
            from agentforge.projects.mutation import Win32Mutation

            self.backend.api = Win32Mutation()
        # Operator creates the directory first; composition performs no writes.
        if Path(path).is_symlink():
            raise SpecialistError("artifact_store_unsafe")
        self.path = self.backend.canonical_root(path, base_directory=Path.cwd())
        self.identity = self.backend.observe_root(self.path)

    @contextmanager
    def root(self):
        with self.backend.anchored_root(self.path, self.identity) as root:
            if os.name != "nt":
                info = os.fstat(root)
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise SpecialistError("artifact_store_unsafe")
            yield root

    @staticmethod
    def name(identity, *, pending=False):
        if not isinstance(identity, UUID):
            raise SpecialistError("artifact_not_found")
        return identity.hex + (".pending" if pending else ".blob")

    def write(self, identity, data):
        name, temporary = self.name(identity), self.name(identity, pending=True)
        with self.root() as root:
            if os.name == "nt":
                self._windows_write(root, temporary, name, data)
                return
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=root,
            )
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                    os.fchmod(file.fileno(), 0o400)
                import ctypes as c

                library = c.CDLL(None, use_errno=True)
                rename = getattr(
                    library,
                    "renameatx_np" if sys.platform == "darwin" else "renameat2",
                    None,
                )
                if rename is None:
                    raise SpecialistError("artifact_store_unsafe")
                rename.argtypes = [c.c_int, c.c_char_p, c.c_int, c.c_char_p, c.c_uint]
                rename.restype = c.c_int
                if rename(
                    root,
                    temporary.encode(),
                    root,
                    name.encode(),
                    4 if sys.platform == "darwin" else 1,
                ):
                    raise OSError(c.get_errno(), "Artifact publication failed")
                os.fsync(root)
            finally:
                try:
                    os.unlink(temporary, dir_fd=root)
                except FileNotFoundError:
                    pass

    def _windows_write(self, root, temporary, name, data):
        import ctypes as c
        import ntpath

        from agentforge.projects.win32 import DWORD, HANDLE

        api = self.backend.api
        handle = api.open_mutation(
            "\\\\?\\" + ntpath.join(root.path, temporary), create=True
        )
        try:
            api.rewrite(handle, data)

            class RenameInfo(c.Structure):
                _fields_ = [
                    ("replace", c.c_ubyte),
                    ("root", HANDLE),
                    ("length", DWORD),
                    ("name", c.c_wchar * 1),
                ]

            target = ntpath.join(root.path, name).encode("utf-16-le")
            buffer = c.create_string_buffer(RenameInfo.name.offset + len(target) + 2)
            info = RenameInfo.from_buffer(buffer)
            info.length = len(target)
            c.memmove(c.addressof(buffer) + RenameInfo.name.offset, target, len(target))
            self.backend._verify(root)
            if not api.api.SetFileInformationByHandle(handle, 3, buffer, len(buffer)):
                api._error()
            if not api.api.FlushFileBuffers(handle):
                api._error()
        finally:
            api.close(handle)

    def read(self, identity, maximum):
        with self.root() as root:
            try:
                with self.backend.regular_file(root, self.name(identity)) as file:
                    if os.name != "nt" and os.fstat(file.fileno()).st_nlink != 1:
                        raise SpecialistError("artifact_store_unsafe")
                    data = file.read(maximum + 1)
                    if len(data) > maximum:
                        raise SpecialistError("artifact_integrity")
                    return data
            except FileNotFoundError:
                raise SpecialistError("artifact_missing") from None

    def remove(self, identity):
        with self.root() as root:
            for pending in (False, True):
                name = self.name(identity, pending=pending)
                try:
                    observed = self.backend.stat(root, name)
                    if os.name == "nt":
                        self.backend.api.remove_file(root, name, observed.identity)
                    else:
                        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                            raise UnsafeProjectPath("Artifact cleanup object changed")
                        os.unlink(name, dir_fd=root)
                        os.fsync(root)
                except FileNotFoundError:
                    pass
