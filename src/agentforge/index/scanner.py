"""Read-only, descriptor-anchored traversal under the registry's canonical root."""

import hashlib
import os  # noqa: F401 -- preserve the scanner's filesystem test seam
from collections.abc import Iterator
from pathlib import Path

from agentforge.index.models import IndexRefreshError
from agentforge.projects.errors import UnsafeProjectPath
from agentforge.projects.exclusions import EXCLUDED_DIRECTORIES  # noqa: F401
from agentforge.projects.filesystem import anchored_root, regular_file, walk_files


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def scan_python_files(root: Path) -> Iterator[tuple[str, bytes]]:
    """Skip all symlinks, even internal aliases; never follow them during I/O.

    Registry validation defines the boundary. POSIX no-follow directory descriptors
    enforce it across path replacement races, including canonical-root ancestors.
    A read/traversal failure aborts refresh rather than looking like deleted files.
    """
    try:
        with anchored_root(root) as root_fd:
            traversal = walk_files(root_fd)
            try:
                for path, directory_fd, name in traversal:
                    if name.endswith(".py"):
                        with regular_file(directory_fd, name) as file:
                            content = file.read()
                        yield path, content
            finally:
                traversal.close()
    except (OSError, UnsafeProjectPath):
        raise IndexRefreshError("Project source could not be read safely") from None
