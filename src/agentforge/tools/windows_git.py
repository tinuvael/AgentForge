"""Offline Git snapshot, copied exclusively through locked Windows handles.

Git never opens the original worktree. Repository config, hooks, attributes/info,
alternates, gitfiles and common-directory redirection cannot enter the snapshot.
Temporary storage is private runtime state and deleted with the authorized root.
"""

import hashlib
import io
import ntpath
import os
import re
import shutil
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory

from agentforge.projects.backends import GitLocation
from agentforge.projects.errors import UnsafeProjectPath
from agentforge.tools.errors import GitFailure, GitUnavailable, NotGitRepository

SNAPSHOT_BYTES = 256 * 1024 * 1024
SNAPSHOT_FILES = 100_000
METADATA_FILE_BYTES = 128 * 1024 * 1024
SOURCE_FILE_BYTES = 2 * 1024 * 1024


@dataclass
class _Budget:
    size: int = 0
    files: int = 0

    def entry(self):
        self.files += 1
        if self.files > SNAPSHOT_FILES:
            raise GitFailure("Windows Git snapshot entry limit exceeded")

    def data(self, length):
        self.size += length
        if self.size > SNAPSHOT_BYTES:
            raise GitFailure("Windows Git snapshot byte limit exceeded")


class _Scratch:
    """Pin scratch directories before writes; exclusively create and seal files.

    Private runtime storage is trusted like the database and executable installation.
    Hashes also detect substitution in the short close-writer/open-reader transition.
    """

    def __init__(self, backend, destination, resources):
        self.backend, self.destination, self.resources = backend, destination, resources
        self.directories = {
            destination: resources.enter_context(backend.anchored_root(destination))
        }

    def parent(self, path):
        if path not in self.directories:
            parent = self.parent(path.parent)
            self.backend.validate_parts((path.name,))
            path.mkdir()  # Exclusive creation, beneath an already pinned parent.
            self.directories[path] = self.resources.enter_context(
                self.backend._open(ntpath.join(parent.path, path.name), parent=parent)
            )
        return self.directories[path]

    def copy(self, source, target, budget, cap):
        parent = self.parent(target.parent)
        self.backend.validate_parts((target.name,))
        digest = hashlib.sha256()
        size = 0
        with target.open("xb") as destination:  # CREATE_NEW: existing links fail.
            while data := source.read(64 * 1024):
                size += len(data)
                if size > cap:
                    raise GitFailure("Windows Git snapshot file limit exceeded")
                budget.data(len(data))
                digest.update(data)
                destination.write(data)
        sealed = self.resources.enter_context(
            self.backend._open(
                ntpath.join(parent.path, target.name), parent=parent, directory=False
            )
        )
        actual = hashlib.sha256()
        used = 0
        while data := self.backend.api.read(sealed.handle, 64 * 1024):
            used += len(data)
            if used > size:
                raise UnsafeProjectPath("Windows Git scratch file was substituted")
            actual.update(data)
        if used != size or actual.digest() != digest.digest():
            raise UnsafeProjectPath("Windows Git scratch file was substituted")

    def write_config(self, target, budget):
        data = (
            b"[core]\nrepositoryformatversion = 0\nbare = false\n"
            b"filemode = false\nignorecase = true\nautocrlf = false\n"
        )
        self.copy(io.BytesIO(data), target, budget, len(data))


def _names(node):
    with os.scandir("\\\\?\\" + node.path) as entries:
        return sorted(entry.name for entry in entries)


def _copy_file(backend, parent, name, target, budget, cap, scratch):
    observed = backend.stat(parent, name)
    if observed.reparse or observed.directory or observed.size > cap:
        raise GitFailure("Unsupported Windows Git snapshot file")
    with backend.regular_file(parent, name, expected=observed) as source:
        scratch.copy(source, target, budget, cap)


def _metadata_file(parts):
    if len(parts) == 1:
        return parts[0] in {"HEAD", "index", "packed-refs", "shallow"}
    if parts[0] == "refs":
        return len(parts) >= 3 and parts[1] in {"heads", "tags", "remotes"}
    if parts[0] == "objects" and len(parts) == 3:
        return (
            bool(re.fullmatch(r"[0-9a-f]{2}", parts[1]))
            and bool(re.fullmatch(r"[0-9a-f]{38}", parts[2]))
        ) or (
            parts[1] == "pack"
            and bool(re.fullmatch(r"pack-[0-9a-f]{40}\.(pack|idx|rev)", parts[2]))
        )
    return False


def _metadata_directory(parts):
    if parts[0] == "refs":
        return len(parts) == 1 or parts[1] in {"heads", "tags", "remotes"}
    return parts[0] == "objects" and (
        len(parts) == 1
        or len(parts) == 2
        and (parts[1] in {"pack", "info"} or re.fullmatch(r"[0-9a-f]{2}", parts[1]))
    )


def _copy_metadata(backend, node, destination, budget, scratch, parts=()):
    for name in _names(node):
        budget.entry()
        child_parts = (*parts, name)
        if child_parts in {
            ("commondir",),
            ("objects", "info", "alternates"),
            ("objects", "info", "http-alternates"),
        }:
            raise GitUnavailable("Redirected Windows Git metadata is unsupported")
        if not (_metadata_file(child_parts) or _metadata_directory(child_parts)):
            continue
        backend.validate_parts((name,))
        observed = backend.stat(node, name)
        if observed.reparse:
            raise UnsafeProjectPath("Reparsed Windows Git metadata is denied")
        if observed.directory:
            if not _metadata_directory(child_parts):
                raise GitFailure("Unsupported Windows Git metadata layout")
            with backend.directory(node, (name,), expected=observed) as child:
                scratch.parent(destination.joinpath(*child_parts))
                _copy_metadata(
                    backend, child, destination, budget, scratch, child_parts
                )
        elif _metadata_file(child_parts):
            _copy_file(
                backend,
                node,
                name,
                destination.joinpath(*child_parts),
                budget,
                METADATA_FILE_BYTES,
                scratch,
            )
        else:
            raise GitFailure("Unsupported Windows Git metadata layout")


def _pin_snapshot(backend, destination, resources):
    # Pin scratch ancestry and ALL scratch objects while Git runs. Temporary
    # config/files cannot be rewritten or redirected between validation and Git.
    root = resources.enter_context(backend.anchored_root(destination))

    def pin(node):
        for name in _names(node):
            backend.validate_parts((name,))
            child = resources.enter_context(
                backend._open(ntpath.join(node.path, name), parent=node, observe=True)
            )
            if child.info.reparse:
                raise UnsafeProjectPath("Reparsed Windows Git scratch object is denied")
            if child.info.directory:
                pin(child)

    pin(root)


def _git_executable(backend, repository, resources):
    # Windows shutil.which("git.exe") can implicitly search cwd. Resolve ONLY
    # absolute PATH entries (trusted runtime configuration), using qualified names
    # to bypass that behavior, and never execute content from the source worktree.
    boundary = PureWindowsPath(repository.path).parts
    from agentforge.projects.windows import drive_path

    for entry in os.environ.get("PATH", "").split(";"):
        directory = PureWindowsPath(entry)
        if not entry or not directory.is_absolute():
            continue
        candidate = directory / "git.exe"
        # Git for Windows cmd/bin entries are launchers. Run the real installed
        # binary directly, so timeout termination targets the command itself.
        if backend.same_component(directory.name, "cmd") or (
            backend.same_component(directory.name, "bin")
            and not backend.same_component(directory.parent.name, "mingw64")
            and not backend.same_component(directory.parent.name, "mingw32")
        ):
            candidates = [
                directory.parent / architecture / "bin" / "git.exe"
                for architecture in ("mingw64", "mingw32")
            ]
        else:
            candidates = [candidate]
        for candidate in candidates:
            try:
                normalized = drive_path(candidate)
            except UnsafeProjectPath:
                continue
            parts = PureWindowsPath(normalized).parts
            if len(parts) >= len(boundary) and all(
                backend.same_component(a, b)
                for a, b in zip(parts, boundary, strict=False)
            ):
                continue
            found = shutil.which(str(candidate))
            if found:
                parent = resources.enter_context(
                    backend.anchored_root(str(candidate.parent))
                )
                # Pin the executable against substitution/reparse and refuse
                # unsupported identity/volume cases just as for source objects.
                resources.enter_context(backend.regular_file(parent, candidate.name))
                return found
    raise GitUnavailable("A trusted Git for Windows executable is unavailable")


def snapshot_git(backend, root):
    if root.resources is None:
        raise GitFailure("Windows Git requires an authorized root lifetime")
    with ExitStack() as source_resources:
        repository = metadata = None
        for ancestor in reversed(root.ancestors):
            try:
                observed = backend.stat(ancestor, ".git")
            except FileNotFoundError:
                continue
            if observed.reparse or not observed.directory:
                raise GitUnavailable("Windows Git requires a normal .git directory")
            repository = ancestor
            metadata = source_resources.enter_context(
                backend.directory(ancestor, (".git",), expected=observed)
            )
            break
        if repository is None:
            raise NotGitRepository("Project is not a Git worktree")
        executable = _git_executable(backend, repository, root.resources)
        # Relative components are derived from the locked ancestry, not prefixes.
        prefix_parts = PureWindowsPath(root.path).parts[
            len(PureWindowsPath(repository.path).parts) :
        ]
        prefix = "/".join(prefix_parts)
        temporary = root.resources.enter_context(
            TemporaryDirectory(prefix="agentforge-git-")
        )
        destination = Path(temporary)
        scratch = _Scratch(backend, destination, root.resources)
        git_directory = destination / ".git"
        scratch.parent(git_directory)
        budget = _Budget()
        _copy_metadata(backend, metadata, git_directory, budget, scratch)
        if not (git_directory / "HEAD").is_file():
            raise GitFailure("Windows Git HEAD is unavailable")
        # Never copy repository configuration. Fixed SHA-1, ordinary worktrees
        # only; extensions, includes, filters, executable drivers and redirection
        # are deliberately unsupported in v1.
        scratch.write_config(git_directory / "config", budget)
        worktree = destination.joinpath(*prefix_parts)
        scratch.parent(worktree)
        traversal = backend.walk_files(root)
        try:
            for path, parent, name in traversal:
                budget.entry()
                _copy_file(
                    backend,
                    parent,
                    name,
                    worktree.joinpath(*path.split("/")),
                    budget,
                    SOURCE_FILE_BYTES,
                    scratch,
                )
        finally:
            traversal.close()
        _pin_snapshot(backend, destination, root.resources)
        return GitLocation(
            str(worktree),
            prefix=prefix,
            repository_root=Path(repository.path),
            executable=executable,
        )
