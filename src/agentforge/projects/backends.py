"""Internal platform capabilities for repository access and Git processes."""

import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentforge.projects import filesystem as posix
from agentforge.projects.errors import InvalidProjectPath, UnsafeProjectPath
from agentforge.projects.identity import RootIdentity
from agentforge.projects.paths import canonical_project_root, resolve_project_path
from agentforge.tools.errors import GitUnavailable


@dataclass(frozen=True)
class GitLocation:
    cwd: str
    descriptors: tuple[int, ...] = ()
    prefix: str | None = None
    repository_root: Path | None = None
    executable: str = "git"

    @property
    def isolated(self):
        return self.prefix is not None

    def process_options(self):
        if self.isolated:
            return {"close_fds": True}
        return {"pass_fds": self.descriptors, "start_new_session": True}

    def stop(self, process):
        try:
            if self.isolated:
                process.kill()  # Snapshot config cannot launch helpers/filters.
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class SafeFilesystemBackend(Protocol):
    def canonical_root(self, path, *, base_directory): ...
    def observe_root(self, root) -> RootIdentity: ...
    def anchored_root(self, root, expected: RootIdentity): ...
    def resolve_path(self, root, candidate, *, expected=None): ...
    def directory(self, root, parts, *, expected=None): ...
    def stat(self, directory, name): ...
    def regular_file(self, directory, name, *, expected=None): ...
    def walk_files(self, directory, parts=(), *, include=None): ...
    def git_location(self, root, handle) -> GitLocation: ...
    def inspect_git(self, handle): ...
    def validate_parts(self, parts): ...
    def same_component(self, left, right) -> bool: ...
    def automatic(self, parts) -> bool: ...
    def excluded_directory(self, name) -> bool: ...
    def require_public(self, parts): ...


class PosixSafeFilesystemBackend:
    canonical_root = staticmethod(canonical_project_root)
    walk_files = staticmethod(posix.walk_files)

    def directory(self, root, parts, *, expected=None):
        return posix.anchored_directory(root, parts)

    def resolve_path(self, root, candidate, *, expected=None):
        if expected is not None:
            try:
                with self.anchored_root(root, expected):
                    return resolve_project_path(root, candidate)
            except InvalidProjectPath:
                raise UnsafeProjectPath(
                    "Project path cannot be safely resolved"
                ) from None
        return resolve_project_path(root, candidate)

    def observe_root(self, root):
        observed = root.stat()
        return RootIdentity("posix", str(observed.st_dev), str(observed.st_ino))

    def anchored_root(self, root, expected):
        if expected.kind != "posix":
            raise UnsafeProjectPath("Registered filesystem platform has changed")
        return posix.anchored_root(root, (int(expected.volume), int(expected.file_id)))

    def stat(self, directory, name):
        return os.stat(name, dir_fd=directory, follow_symlinks=False)

    def regular_file(self, directory, name, *, expected=None):
        return posix.regular_file(directory, name)

    def git_location(self, root, handle):
        cwd = f"/proc/self/fd/{handle}"
        if not Path(cwd).is_dir():
            raise GitUnavailable("Git tools require Linux descriptor-backed cwd")
        return GitLocation(cwd, (handle,))

    def inspect_git(self, handle):
        from agentforge.projects.git import _inspect_posix_git

        return _inspect_posix_git(handle)

    def validate_parts(self, parts):
        return None

    def same_component(self, left, right):
        return left == right

    def automatic(self, parts):
        from agentforge.tools.policy import automatic

        return automatic(parts)

    def excluded_directory(self, name):
        from agentforge.projects.exclusions import excluded_directory

        return excluded_directory(name)

    def require_public(self, parts):
        from agentforge.tools.policy import require_public

        require_public(parts)


POSIX = PosixSafeFilesystemBackend()


def select_backend() -> SafeFilesystemBackend:
    if os.name == "nt":
        from agentforge.projects.windows import WindowsSafeFilesystemBackend

        return WindowsSafeFilesystemBackend()
    return POSIX


def backend_for(handle) -> SafeFilesystemBackend:
    # POSIX read-only descriptors and backend-owned directory capabilities.
    return POSIX if isinstance(handle, int) else handle.backend
