"""Read-only, descriptor-anchored traversal under the registry's canonical root."""

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from agentforge.index.models import IndexRefreshError
from agentforge.projects.paths import validate_registered_root

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "node_modules",
        "dist",
        "build",
        "vendor",
        "third_party",
        ".idea",
        ".vscode",
        ".cache",
        ".secrets",
        ".ipynb_checkpoints",
        ".eggs",
        "site-packages",
    }
)


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def scan_python_files(root: Path) -> Iterator[tuple[str, bytes]]:
    """Skip all symlinks, even internal aliases; never follow them during I/O.

    Registry validation defines the boundary. POSIX no-follow directory descriptors
    enforce it across path replacement races, including canonical-root ancestors.
    A read/traversal failure aborts refresh rather than looking like deleted files.
    """
    validate_registered_root(root)
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise IndexRefreshError(
            "Safe indexing requires no-follow directory descriptors"
        )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = None
    try:
        root_fd = os.open(root.anchor, directory_flags)
        for component in root.parts[1:]:
            child_fd = os.open(component, directory_flags, dir_fd=root_fd)
            os.close(root_fd)
            root_fd = child_fd
        root_stat = os.fstat(root_fd)

        def walk(directory_fd: int, parts: tuple[str, ...]):
            with os.scandir(directory_fd) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(entry_stat.st_mode):
                    if name in EXCLUDED_DIRECTORIES or name.endswith(".egg-info"):
                        continue
                    child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                    try:
                        yield from walk(child_fd, (*parts, name))
                        opened = os.fstat(child_fd)
                        current = os.stat(
                            name, dir_fd=directory_fd, follow_symlinks=False
                        )
                        if (opened.st_dev, opened.st_ino) != (
                            current.st_dev,
                            current.st_ino,
                        ):
                            raise IndexRefreshError("Directory changed during indexing")
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(entry_stat.st_mode) and name.endswith(".py"):
                    fd = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory_fd,
                    )
                    with os.fdopen(fd, "rb") as file:
                        before = os.fstat(file.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise IndexRefreshError(
                                "Source ceased to be a regular file"
                            )
                        content = file.read()
                        after = os.fstat(file.fileno())
                        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                            after.st_size,
                            after.st_mtime_ns,
                            after.st_ctime_ns,
                        ):
                            raise IndexRefreshError("Source changed while being read")
                    yield "/".join((*parts, name)), content

        yield from walk(root_fd, ())
        validate_registered_root(root)
        current = root.stat()
        if (current.st_dev, current.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            raise IndexRefreshError("Project root changed during indexing")
    except OSError:
        raise IndexRefreshError("Project source could not be read safely") from None
    finally:
        if root_fd is not None:
            os.close(root_fd)
