"""Local SQLite executor ownership, independent of SQLite transaction locking.

The sidecar is permanent: unlinking it could give contenders different inodes.
Parents are trusted operator storage, never repository/model-controlled paths.
"""

import ctypes
import errno
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from threading import Lock

from sqlalchemy import event

from agentforge.projects.errors import UnsafeProjectPath
from agentforge.tasks.models import TaskStorageError

BUSY_MESSAGE = (
    "Another AgentForge executor already owns this database. "
    "Stop the other MCP/web process or use a separate database."
)
UNSAFE_MESSAGE = (
    "Database executor ownership is unavailable. Use a supported local database "
    "in trusted writable storage; check permissions and filesystem configuration."
)


class ExecutorOwnershipError(TaskStorageError, RuntimeError):
    """Fixed public diagnostic; never attach filesystem exceptions."""


_OWNERS: set[object] = set()
_OWNERS_LOCK = Lock()


def _local_posix(fd):
    # A whitelist avoids silently treating NFS/SMB/FUSE as local locking.
    library = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(4096)
    if sys.platform == "linux":
        function = library.fstatfs
        supported = {
            0xEF53,
            0x58465342,
            0x9123683E,
            0x01021994,
        }  # ext, XFS, Btrfs, tmpfs
    else:
        raise ExecutorOwnershipError(UNSAFE_MESSAGE)
    function.argtypes = [ctypes.c_int, ctypes.c_void_p]
    function.restype = ctypes.c_int
    if function(fd, buffer) != 0:
        raise ExecutorOwnershipError(UNSAFE_MESSAGE)
    value = ctypes.c_long.from_buffer(buffer).value & 0xFFFFFFFF
    if value not in supported:
        raise ExecutorOwnershipError(UNSAFE_MESSAGE)


def _lock(fd):
    try:
        if os.name == "nt":
            import msvcrt

            # Nonblocking, one byte at offset zero, including an empty sidecar.
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise ExecutorOwnershipError(BUSY_MESSAGE) from None
        raise ExecutorOwnershipError(UNSAFE_MESSAGE) from None


class DatabaseOwnership:
    def __init__(self, database):
        self.database = database
        self._resources: ExitStack | None = None
        url = database.url
        self._memory = url.get_backend_name() == "sqlite" and url.database in {
            None,
            "",
            ":memory:",
        }
        self._supported = (
            url.drivername in {"sqlite", "sqlite+pysqlite"}
            and not url.query
            and not url.host
            and not url.username
            and not url.password
        )
        # The engine factory freezes the actual SQLite filename. Re-resolving it
        # here could lock a different file after a symlink/working-directory change.
        self.path = None if self._memory else Path(url.database or "")
        if self.path is not None and not self.path.is_absolute():
            self._supported = False
        self.key = database if self._memory else os.path.normcase(str(self.path))
        self._verify = lambda: None

    @property
    def held(self):
        return self._resources is not None

    def acquire(self):
        if self._resources is not None:
            self.verify()
            return
        if not self._supported:
            raise ExecutorOwnershipError(UNSAFE_MESSAGE)
        with _OWNERS_LOCK:
            if self.key in _OWNERS:
                raise ExecutorOwnershipError(BUSY_MESSAGE)
            _OWNERS.add(self.key)
        resources = ExitStack()
        try:
            if not self._memory:
                if os.name == "nt":
                    self._windows(resources)
                else:
                    self._posix(resources)
                self.verify()
                # A previous short-lived owner may have left idle SQLite handles.
                # Never reuse their inode after an offline database replacement.
                self.database.pool.dispose()
                event.listen(self.database, "before_cursor_execute", self._before_sql)
                resources.callback(
                    event.remove,
                    self.database,
                    "before_cursor_execute",
                    self._before_sql,
                )
            self._resources = resources
        except BaseException as error:
            try:
                resources.close()
            finally:
                with _OWNERS_LOCK:
                    _OWNERS.discard(self.key)
            self._verify = lambda: None
            if isinstance(error, (OSError, ValueError, UnsafeProjectPath)):
                raise ExecutorOwnershipError(UNSAFE_MESSAGE) from None
            raise

    def _posix(self, resources):
        path = self.path
        if path.resolve() != path:
            raise ExecutorOwnershipError(UNSAFE_MESSAGE)
        parent = path.parent
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        resources.callback(os.close, directory)
        parent_info = os.fstat(directory)
        if parent_info.st_uid != os.geteuid() or parent_info.st_mode & 0o022:
            raise ExecutorOwnershipError(UNSAFE_MESSAGE)
        _local_posix(directory)
        sidecar = path.with_name(path.name + ".agentforge.lock")
        pins = []
        for name in (sidecar.name, path.name):
            fd = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            resources.callback(os.close, fd)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not info.st_ino
                or info.st_uid != os.geteuid()
                or info.st_mode & (0o077 if name == sidecar.name else 0o022)
            ):
                raise ExecutorOwnershipError(UNSAFE_MESSAGE)
            # Also lock the DB inode: rename/bind aliases cannot evade ownership.
            # Local flock does not overlap SQLite's POSIX byte-range locks.
            _lock(fd)
            pins.append((name, fd, (info.st_dev, info.st_ino)))

        def verify():
            current = parent.stat()
            if (current.st_dev, current.st_ino) != (
                parent_info.st_dev,
                parent_info.st_ino,
            ) or path.resolve() != path:
                raise ExecutorOwnershipError(UNSAFE_MESSAGE)
            for name, fd, expected in pins:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                opened = os.fstat(fd)
                if (
                    (current.st_dev, current.st_ino) != expected
                    or not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or opened.st_nlink != 1
                ):
                    raise ExecutorOwnershipError(UNSAFE_MESSAGE)

        self._verify = verify

    def _windows(self, resources):
        import msvcrt

        from agentforge.projects.win32 import Win32
        from agentforge.projects.windows import (
            WindowsSafeFilesystemBackend,
            drive_path,
            ordinary_final,
            validate_windows_parts,
        )

        # Reject alternate namespaces/streams/trailing-dot names before Win32 I/O.
        path = Path(drive_path(self.path))
        validate_windows_parts((path.name,))

        class OwnershipDirectories(Win32):
            def open(self, path):
                # Publication of a sibling policy needs directory WRITE sharing.
                # Keep DELETE denied so pinned ancestry cannot be renamed; DB
                # and lock file handles below retain their original protections.
                handle = self.api.CreateFileW(
                    path, 0x80000000, 1 | 2, None, 3, 0x02200000, None
                )
                if handle == ctypes.c_void_p(-1).value:
                    self._error()
                return handle

        backend = WindowsSafeFilesystemBackend(OwnershipDirectories())
        parent = resources.enter_context(backend.anchored_root(path.parent))
        api = backend.api
        handles = []
        # DB handle permits SQLite read/write, but denies deletion/replacement.
        for target, access, disposition in (
            (path, 0x80000000, 4),  # OPEN_ALWAYS; schema check still never migrates
            (path.with_name(path.name + ".agentforge.lock"), 0xC0000000, 4),
        ):
            handle = api.api.CreateFileW(
                "\\\\?\\" + str(target), access, 3, None, disposition, 0x00200000, None
            )  # SHARE_READ|WRITE, no DELETE; OPEN_REPARSE_POINT
            if handle == ctypes.c_void_p(-1).value:
                api._error()
            if target == path:
                resources.callback(api.close, handle)
            else:
                try:
                    fd = msvcrt.open_osfhandle(
                        handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT
                    )
                except BaseException:
                    api.close(handle)
                    raise
                resources.callback(os.close, fd)
            info = api.info(handle)
            if info.directory or info.reparse or info.links != 1:
                raise ExecutorOwnershipError(UNSAFE_MESSAGE)
            final = Path(ordinary_final(api.final_path(handle)))
            if not api.same_path(str(final), str(target)):
                raise ExecutorOwnershipError(UNSAFE_MESSAGE)
            handles.append((handle, info.identity, final))
        _lock(fd)

        def verify():
            backend._verify(parent)
            for handle, identity, final in handles:
                info = api.info(handle)
                if (
                    info.identity != identity
                    or info.links != 1
                    or info.reparse
                    or Path(ordinary_final(api.final_path(handle))) != final
                ):
                    raise ExecutorOwnershipError(UNSAFE_MESSAGE)

        self._verify = verify

    def verify(self):
        try:
            self._verify()
        except (OSError, ValueError, UnsafeProjectPath):
            raise ExecutorOwnershipError(UNSAFE_MESSAGE) from None

    def _before_sql(self, *_):
        self.verify()

    def release(self):
        resources, self._resources = self._resources, None
        if resources is None:
            return
        try:
            resources.close()
        finally:
            self._verify = lambda: None
            with _OWNERS_LOCK:
                _OWNERS.discard(self.key)
