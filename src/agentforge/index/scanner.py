"""Read-only platform-authorized traversal under the registered canonical root."""

import hashlib
from collections.abc import Iterator

from agentforge.index.models import IndexRefreshError
from agentforge.projects.backends import backend_for
from agentforge.projects.errors import UnsafeProjectPath
from agentforge.tools.policy import FILE_SCAN_BYTES


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def scan_python_files(root_fd: int) -> Iterator[tuple[str, bytes]]:
    """Skip all symlinks, even internal aliases; never follow them during I/O.

    The caller must keep the Registry's identity-checked open_root context active
    while consuming this iterator. The scanner never reopens a root pathname.
    A read/traversal failure aborts refresh rather than looking like deleted files.
    """
    try:
        backend = backend_for(root_fd)
        traversal = backend.walk_files(root_fd)
        try:
            for path, directory_fd, name in traversal:
                if name.endswith(".py"):
                    observed = backend.stat(directory_fd, name)
                    with backend.regular_file(
                        directory_fd, name, expected=observed
                    ) as file:
                        content = file.read(FILE_SCAN_BYTES + 1)
                    if len(content) > FILE_SCAN_BYTES:
                        raise IndexRefreshError("Python source exceeds the scan limit")
                    yield path, content
        finally:
            traversal.close()
    except (OSError, UnsafeProjectPath):
        raise IndexRefreshError("Project source could not be read safely") from None
