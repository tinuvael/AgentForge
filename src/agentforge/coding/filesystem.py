"""No-follow coding writes and secure managed-tree removal.

POSIX operations are dir_fd relative. Windows operations act on identity-checked
exclusive file handles beneath the existing backend's pinned no-delete ancestry.
No recursive deletion is available to models.
"""

import hashlib
import ntpath
import os
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from uuid import uuid4

from agentforge.coding.models import EditConflict
from agentforge.projects import filesystem as posix
from agentforge.projects.backends import PosixSafeFilesystemBackend
from agentforge.projects.errors import ProjectError, UnsafeProjectPath
from agentforge.projects.windows import WindowsDirectory, validate_windows_parts
from agentforge.tools.errors import InvalidToolArgument
from agentforge.tools.policy import relative_path


def parts_for(backend, path, *, public=True):
    parts = relative_path(path)
    # Use the strict cross-platform subset even on POSIX; no Windows aliases can
    # enter portable workspaces. Git administrative paths are never model writable.
    validate_windows_parts(parts)
    if not parts:
        raise InvalidToolArgument("A file path is required")
    backend.validate_parts(parts)
    if public:
        backend.require_public(parts)
    return parts


def _mount(fd):
    try:
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as file:
            for line in file:
                if line.startswith("mnt_id:"):
                    return line.split()[1]
    except OSError:
        pass
    raise UnsafeProjectPath("Coding requires descriptor mount identity")


@dataclass
class CodingDirectory:
    fd: int
    backend: "CodingPosixBackend"


class CodingPosixBackend(PosixSafeFilesystemBackend):
    """Workspace reads reject hardlinks and directory mount transitions too."""

    @contextmanager
    def anchored_root(self, root, expected):
        try:
            with super().anchored_root(root, expected) as fd:
                yield CodingDirectory(fd, self)
        except ProjectError:
            raise UnsafeProjectPath("Coding root could not be authorized") from None

    @contextmanager
    def directory(self, root, parts, *, expected=None):
        with posix.anchored_directory(root.fd, parts) as fd:
            if _mount(fd) != _mount(root.fd):
                raise UnsafeProjectPath("Coding mount transitions are denied")
            yield CodingDirectory(fd, self)

    def stat(self, directory, name):
        return super().stat(directory.fd, name)

    @contextmanager
    def regular_file(self, directory, name, *, expected=None):
        with posix.regular_file(directory.fd, name) as file:
            before = os.fstat(file.fileno())
            if before.st_nlink != 1 or _mount(file.fileno()) != _mount(directory.fd):
                raise UnsafeProjectPath("Coding hardlinks and mounts are denied")
            if expected is not None and posix.identity(before) != posix.identity(
                expected
            ):
                raise UnsafeProjectPath("Coding file changed before access")
            yield file
            if os.fstat(file.fileno()).st_nlink != 1:
                raise UnsafeProjectPath("Coding file acquired another link")

    def walk_files(self, directory, parts=(), *, include=None):
        traversal = posix.walk_files(directory.fd, parts, include=include)
        try:
            for path, parent, name in traversal:
                observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if observed.st_nlink != 1 or _mount(parent) != _mount(directory.fd):
                    raise UnsafeProjectPath("Coding hardlinks and mounts are denied")
                yield path, CodingDirectory(parent, self), name
        finally:
            traversal.close()


def _ordinary(observed):
    links = observed.links if hasattr(observed, "links") else observed.st_nlink
    if not stat.S_ISREG(observed.st_mode) or links != 1:
        raise UnsafeProjectPath("Only ordinary single-link files are writable")


@contextmanager
def _parent(root, parts, check, *, mkdir=False):
    if isinstance(root, WindowsDirectory):
        with ExitStack() as stack:
            node = root
            nodes = [root]
            for name in parts:
                if mkdir:
                    check()
                    try:
                        os.mkdir("\\\\?\\" + ntpath.join(node.path, name))
                    except FileExistsError:
                        pass
                node = stack.enter_context(root.backend.directory(node, (name,)))
                nodes.append(node)

            def verify():
                check()
                for ancestor in nodes:
                    root.backend._verify(ancestor)

            verify()
            yield node, verify
            verify()
        return
    root_fd = root.fd if isinstance(root, CodingDirectory) else root
    with ExitStack() as stack:
        current = root_fd
        links = []
        for name in parts:
            if mkdir:
                check()
                try:
                    os.mkdir(name, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
            child = stack.enter_context(posix.anchored_directory(current, (name,)))
            if _mount(child) != _mount(root_fd):
                raise UnsafeProjectPath("Coding mount transitions are denied")
            links.append((current, name, child))
            current = child

        def verify():
            check()
            for parent, name, child in links:
                if posix.identity(os.fstat(child)) != posix.identity(
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                ):
                    raise UnsafeProjectPath("Coding write ancestry changed")

        verify()
        yield current, verify
        verify()


def _precondition(data, expected):
    if expected is None or hashlib.sha256(data).hexdigest() != expected:
        raise EditConflict("File changed; reread before editing")


def mutate(
    root,
    path,
    data,
    *,
    expected,
    check,
    create=False,
    delete=False,
    mkdir=False,
    public=True,
    provision_mode=None,
):
    """expected SHA for edit/delete; exclusive absent-path precondition for create.

    Provisioning alone may supply raw bytes/private paths. Tools use UTF-8 checks
    before entering this primitive. No caller-controlled modes/links are exposed.
    """
    backend = (
        root.backend if not isinstance(root, int) else PosixSafeFilesystemBackend()
    )
    parts = parts_for(backend, path, public=public)
    with _parent(root, parts[:-1], check, mkdir=mkdir) as (parent, verify):
        if isinstance(parent, WindowsDirectory):
            return _windows_mutate(
                parent, parts[-1], data, expected, verify, create, delete
            )
        name = parts[-1]
        existing = None
        try:
            observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise EditConflict("File disappeared; reread before editing") from None
        else:
            _ordinary(observed)
            if create:
                raise EditConflict("Create requires an absent file")
            with posix.regular_file(parent, name) as file:
                current = os.fstat(file.fileno())
                _ordinary(current)
                if _mount(file.fileno()) != _mount(parent):
                    raise UnsafeProjectPath("Coding file mount is denied")
                old = file.read(2_097_153)
                _precondition(old, expected)
                existing = current
        verify()

        def unchanged():
            verify()
            try:
                now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if existing is not None:
                    raise EditConflict("File disappeared before write") from None
            else:
                _ordinary(now)
                if existing is None or (
                    posix.identity(now),
                    now.st_size,
                    now.st_mtime_ns,
                    now.st_ctime_ns,
                ) != (
                    posix.identity(existing),
                    existing.st_size,
                    existing.st_mtime_ns,
                    existing.st_ctime_ns,
                ):
                    raise EditConflict("File changed before write")

        if delete:
            unchanged()
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
            return
        temporary = ".agentforge-write-" + uuid4().hex
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                os.fsync(file.fileno())
                if existing is not None:
                    # Preserve only ordinary bits, never setuid/setgid.
                    os.fchmod(file.fileno(), stat.S_IMODE(existing.st_mode) & 0o777)
                elif provision_mode is not None:
                    os.fchmod(file.fileno(), provision_mode)
                unchanged()
                if create:
                    # Linux renameat2 publishes without overwriting concurrent
                    # creation and without an intermediate hardlink state.
                    _publish_new(parent, temporary, name)
                else:
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def _publish_new(parent, temporary, name):
    import ctypes as c
    import errno

    library = c.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise UnsafeProjectPath("Atomic no-replace creation is unavailable")
    rename.argtypes = [c.c_int, c.c_char_p, c.c_int, c.c_char_p, c.c_uint]
    rename.restype = c.c_int
    if rename(parent, os.fsencode(temporary), parent, os.fsencode(name), 1):
        if c.get_errno() == errno.EEXIST:
            raise EditConflict("File appeared before creation")
        raise OSError(c.get_errno(), "Atomic creation failed")


def _windows_mutate(parent, name, data, expected, verify, create, delete):
    backend = parent.backend
    verify()
    try:
        handle = backend.api.open_mutation(
            "\\\\?\\" + ntpath.join(parent.path, name), create=create
        )
    except (FileExistsError, FileNotFoundError):
        raise EditConflict("File existence changed; reread before editing") from None
    try:
        info = backend.api.info(handle)
        node = WindowsDirectory(backend, ntpath.join(parent.path, name), handle, info)
        _ordinary(info)
        backend._verify(node)
        if info.identity.volume != parent.info.identity.volume:
            raise UnsafeProjectPath("Coding Windows volume transition is denied")
        if not create:
            old = bytearray()
            while len(old) <= 2_097_152 and (chunk := backend.api.read(handle, 65536)):
                old.extend(chunk)
            _precondition(bytes(old), expected)
        verify()
        backend._verify(node)
        if backend.api.info(handle).links != 1:
            raise UnsafeProjectPath("Coding Windows hardlink is denied")
        if delete:
            backend.api.delete_handle(handle)
        else:
            # Exclusive no-write/no-delete sharing closes pathname/precondition
            # races. Handle rewrite is durable but not crash-atomic on Windows.
            backend.api.rewrite(handle, data)
    finally:
        backend.api.close(handle)


def remove_tree(root, check, *, max_entries=100_000):
    """Manager-only removal: no-follow, pinned ancestry, links/mounts refused.

    Leave the root itself to its identity-checked parent owner. Unknown/orphan
    directories never reach this function.
    """
    used = 0

    def remove(node):
        nonlocal used
        check()
        if isinstance(node, WindowsDirectory):
            backend = node.backend
            with os.scandir("\\\\?\\" + node.path) as entries:
                names = [entry.name for entry in entries]
            for name in names:
                used += 1
                if used > max_entries:
                    raise UnsafeProjectPath("Managed cleanup entry limit exceeded")
                backend.validate_parts((name,))
                observed = backend.stat(node, name)
                if observed.reparse or observed.links != 1:
                    raise UnsafeProjectPath("Suspicious managed cleanup object")
                if observed.directory:
                    with backend.directory(node, (name,), expected=observed) as child:
                        remove(child)
                    backend.api.remove_directory(node, name, observed.identity)
                else:
                    backend.api.remove_file(node, name, observed.identity)
            return
        with os.scandir(node) as entries:
            names = [entry.name for entry in entries]
        for name in names:
            used += 1
            if used > max_entries:
                raise UnsafeProjectPath("Managed cleanup entry limit exceeded")
            observed = os.stat(name, dir_fd=node, follow_symlinks=False)
            check()
            if stat.S_ISDIR(observed.st_mode):
                with posix.anchored_directory(node, (name,)) as child:
                    if _mount(child) != _mount(node):
                        raise UnsafeProjectPath("Cleanup mount transition denied")
                    remove(child)
                if posix.identity(
                    os.stat(name, dir_fd=node, follow_symlinks=False)
                ) != posix.identity(observed):
                    raise UnsafeProjectPath("Cleanup directory replaced")
                os.rmdir(name, dir_fd=node)
            else:
                _ordinary(observed)
                if os.stat(name, dir_fd=node, follow_symlinks=False) != observed:
                    raise UnsafeProjectPath("Cleanup file replaced")
                os.unlink(name, dir_fd=node)

    remove(root.fd if isinstance(root, CodingDirectory) else root)
