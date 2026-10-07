"""Native Windows handle authorization; local NTFS, no reparse traversal."""

import io
import ntpath
import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from agentforge.projects.errors import InvalidProjectPath, UnsafeProjectPath
from agentforge.projects.exclusions import EXCLUDED_DIRECTORIES
from agentforge.projects.models import GitMetadata
from agentforge.projects.win32 import ObjectInfo, Win32

_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"
}


def validate_windows_parts(parts):
    for name in parts:
        if (
            not name
            or name in {".", ".."}
            or name.endswith((".", " "))
            or any(c in name for c in '<>:"/\\|?*')
            or any(
                ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in name
            )
            or name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED
        ):
            raise UnsafeProjectPath("Unsupported Windows path component")


def drive_path(path, base=None):
    """Reject NT namespaces/UNC/drive-relative paths BEFORE normalization."""
    raw = str(path).replace("/", "\\")
    if not raw or raw.startswith("\\") or raw.startswith("\\\\"):
        raise UnsafeProjectPath("Windows roots require an ordinary drive path")
    drive, tail = ntpath.splitdrive(raw)
    if drive:
        if (
            len(drive) != 2
            or not drive[0].isascii()
            or not drive[0].isalpha()
            or drive[1] != ":"
            or not tail.startswith("\\")
        ):
            raise UnsafeProjectPath("Windows roots must be drive-absolute")
    elif base is not None:
        raw = ntpath.join(str(base), raw)
        drive, tail = ntpath.splitdrive(raw)
    else:
        raise UnsafeProjectPath("Windows roots must be drive-absolute")
    components = tail[1:].split("\\") if tail != "\\" else []
    validate_windows_parts(components)
    if len(raw) > 32000:
        raise UnsafeProjectPath("Windows path exceeds the safe limit")
    return str(PureWindowsPath(raw))


def ordinary_final(path):
    if not path.startswith("\\\\?\\") or path.startswith("\\\\?\\UNC\\"):
        raise UnsafeProjectPath("Unsupported final Windows namespace")
    return drive_path(path[4:])


@dataclass
class WindowsDirectory:
    backend: "WindowsSafeFilesystemBackend"
    path: str
    handle: int
    info: ObjectInfo
    ancestors: tuple = ()
    resources: ExitStack | None = None
    live: bool = True


class _Reader(io.RawIOBase):
    def __init__(self, api, handle):
        super().__init__()
        self.api, self.handle = api, handle

    def readable(self):
        return True

    def readinto(self, buffer):
        data = self.api.read(self.handle, len(buffer))
        buffer[: len(data)] = data
        return len(data)


class WindowsSafeFilesystemBackend:
    def __init__(self, api=None):
        self.api = api if api is not None else Win32()

    validate_parts = staticmethod(validate_windows_parts)

    def same_component(self, left, right):
        return self.api.same_path(left, right)

    def automatic(self, parts):
        return not self.sensitive(parts) and not any(
            self.excluded_directory(p) for p in parts[:-1]
        )

    def sensitive(self, parts):
        from agentforge.tools.policy import _PRIVATE_DIRECTORIES, _PRIVATE_NAMES

        # Authorization uses the OS's case semantics, including aliases which
        # Python lower/casefold may handle differently (e.g. dotless i/long s).
        for name in parts:
            if (
                any(
                    self.same_component(name, value)
                    for value in _PRIVATE_DIRECTORIES | _PRIVATE_NAMES
                )
                or self.same_component(name, ".env")
                or self.same_component(name[:5], ".env.")
                or any(
                    self.same_component(name[-len(suffix) :], suffix)
                    for suffix in (".pem", ".key", ".p12", ".pfx")
                )
            ):
                return True
        return False

    def require_public(self, parts):
        from agentforge.tools.errors import SensitivePath

        if self.sensitive(parts):
            raise SensitivePath("Sensitive repository paths are denied")

    def excluded_directory(self, name):
        return any(
            self.same_component(name, value) for value in EXCLUDED_DIRECTORIES
        ) or self.same_component(name[-9:], ".egg-info")

    def _verify(self, node):
        if not node.live:
            raise UnsafeProjectPath("Windows filesystem capability is closed")
        current = self.api.info(node.handle)
        if current.reparse or current.identity != node.info.identity:
            raise UnsafeProjectPath("Windows filesystem object changed during access")
        if not self.api.same_path(
            ordinary_final(self.api.final_path(node.handle)), node.path
        ):
            raise UnsafeProjectPath("Windows filesystem location changed during access")

    @contextmanager
    def _open(self, path, *, parent=None, directory=True, observe=False):
        if parent is not None:
            self._verify(parent)
        handle = self.api.open("\\\\?\\" + path)
        try:
            info = self.api.info(handle)
            node = WindowsDirectory(self, path, handle, info)
            if (
                parent is not None
                and info.identity.volume != parent.info.identity.volume
            ):
                raise UnsafeProjectPath("Windows volume boundary changed")
            if info.reparse:
                if observe:
                    yield node  # Metadata only: callers skip/deny, never read/follow.
                    return
                raise UnsafeProjectPath("Windows reparse points are denied")
            if info.directory != directory and not observe:
                raise UnsafeProjectPath("Unsupported Windows filesystem object")
            if not info.directory and info.links != 1:
                raise UnsafeProjectPath("Windows hard-linked files are unsupported")
            self._verify(node)  # Reject aliases such as DOS 8.3 names.
            if info.directory:
                self.api.require_case_insensitive(handle)
            yield node
            self._verify(node)
        finally:
            if "node" in locals():
                node.live = False
            self.api.close(handle)

    @contextmanager
    def anchored_root(self, root, expected=None):
        if expected is not None and expected.kind != "windows":
            raise UnsafeProjectPath("Registered filesystem platform has changed")
        nodes = []
        try:
            path = PureWindowsPath(drive_path(root))
            with ExitStack() as stack:
                for i in range(1, len(path.parts) + 1):
                    current = str(PureWindowsPath(*path.parts[:i]))
                    node = stack.enter_context(
                        self._open(current, parent=nodes[-1] if nodes else None)
                    )
                    nodes.append(node)
                    if i == 1:
                        self.api.require_supported_volume(node.handle, path.anchor)
                node = nodes[-1]
                if expected is not None and node.info.identity != expected:
                    raise UnsafeProjectPath("Registered project root was replaced")
                node.ancestors = tuple(nodes)
                node.resources = stack
                yield node
                for ancestor in reversed(nodes):
                    self._verify(ancestor)
        except OSError:
            raise UnsafeProjectPath(
                "Windows project could not be accessed safely"
            ) from None

    def canonical_root(self, path, *, base_directory):
        try:
            with self.anchored_root(drive_path(path, base_directory)) as node:
                return Path(ordinary_final(self.api.final_path(node.handle)))
        except (OSError, UnsafeProjectPath, ValueError):
            raise InvalidProjectPath(
                "Windows project root cannot be authorized"
            ) from None

    def observe_root(self, root):
        with self.anchored_root(root) as node:
            return node.info.identity

    def resolve_path(self, root, candidate, *, expected=None):
        # This helper returns a point-in-time validated path, never an I/O
        # capability. Windows deliberately rejects absolute candidates and aliases.
        from agentforge.tools.policy import relative_path

        parts = relative_path(str(candidate))
        self.validate_parts(parts)
        if expected is None:
            raise UnsafeProjectPath(
                "Registered root identity is required for safe file access"
            )
        with self.anchored_root(root, expected) as node:
            if not parts:
                return Path(node.path)
            with self.directory(node, parts[:-1]) as parent:
                with self._open(
                    ntpath.join(parent.path, parts[-1]), parent=parent, observe=True
                ) as child:
                    if child.info.reparse:
                        raise UnsafeProjectPath("Windows reparse points are denied")
                    return Path(child.path)

    @contextmanager
    def directory(self, root, parts, *, expected=None):
        self.validate_parts(parts)
        self._verify(root)
        with ExitStack() as stack:
            node = root
            for name in parts:
                node = stack.enter_context(
                    self._open(ntpath.join(node.path, name), parent=node)
                )
            if expected is not None and node.info.identity != expected.identity:
                raise UnsafeProjectPath("Windows directory was replaced before access")
            yield node

    def stat(self, directory, name):
        self.validate_parts((name,))
        with self._open(
            ntpath.join(directory.path, name), parent=directory, observe=True
        ) as node:
            return node.info

    @contextmanager
    def regular_file(self, directory, name, *, expected=None):
        self.validate_parts((name,))
        with self._open(
            ntpath.join(directory.path, name), parent=directory, directory=False
        ) as node:
            if expected is not None and expected.identity != node.info.identity:
                raise UnsafeProjectPath("Windows file was replaced before read")
            with io.BufferedReader(_Reader(self.api, node.handle)) as file:
                yield file
            if self.api.info(node.handle) != node.info:
                raise UnsafeProjectPath("Windows file changed during access")

    def walk_files(self, directory, parts=(), *, include=None):
        self._verify(directory)
        # The directory and every ancestor are still locked against write/delete.
        with os.scandir("\\\\?\\" + directory.path) as entries:
            names = sorted(entry.name for entry in entries)
        candidates = []
        for name in names:
            try:
                self.validate_parts((name,))
            except UnsafeProjectPath:
                continue
            if not self.automatic((*parts, name)):
                continue
            if include is not None and not include((*parts, name)):
                continue
            info = self.stat(directory, name)
            if info.reparse:
                continue
            if info.directory:
                if not self.excluded_directory(name):
                    candidates.append((name + "/", name, True, info))
            else:
                candidates.append((name, name, False, info))
        try:
            for _, name, is_directory, observed in sorted(candidates):
                if is_directory:
                    with self.directory(directory, (name,), expected=observed) as child:
                        yield from self.walk_files(
                            child, (*parts, name), include=include
                        )
                else:
                    yield "/".join((*parts, name)), directory, name
        finally:
            self._verify(directory)  # Also validate generator.close at output limits.

    def git_location(self, root, handle):
        from agentforge.tools.windows_git import snapshot_git

        return snapshot_git(self, handle)

    def inspect_git(self, handle):
        from agentforge.tools.errors import (
            GitFailure,
            GitTimeout,
            GitUnavailable,
            NotGitRepository,
        )
        from agentforge.tools.git_backend import _Git

        observed = datetime.now(UTC)
        try:
            git = _Git(Path(handle.path), handle)
            branch, _ = git.branch()
            head = git._run(
                ["rev-parse", "--verify", "HEAD^{commit}"], 128, allow_failure=True
            )
            return GitMetadata(
                "repository",
                observed,
                git.location.repository_root,
                branch,
                head.data.decode("ascii").strip()
                if head.returncode == 0 and not head.truncated
                else None,
            )
        except NotGitRepository:
            return GitMetadata("not_repository", observed)
        except (OSError, GitFailure, GitUnavailable, GitTimeout, UnicodeError):
            return GitMetadata("unavailable", observed)
