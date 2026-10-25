"""Read-only setup checks using the existing coding filesystem boundary."""

import os
import sys
from pathlib import Path

from agentforge.coding.filesystem import CodingDirectory
from agentforge.coding.models import CodingError
from agentforge.projects import filesystem as posix


def check_coding_host(manager) -> None:
    """Check configured executables and registered roots; execute no commands.

    Manager construction already checks the pre-created private workspace parent.
    Task-time repository/identity checks remain authoritative.
    """
    config, backend = manager.config, manager.filesystem
    executables = [config.git_executable]
    executables.extend(Path(c.argv[0]) for c in config.validations.values())
    for position, executable in enumerate(executables):
        if executable.is_relative_to(manager.parent):
            raise CodingError("Executables must be outside coding workspaces")
        if os.name == "nt" and executable.suffix.lower() != ".exe":
            raise CodingError("Windows requires direct executable files")
        with backend.anchored_root(
            executable.parent, backend.observe_root(executable.parent)
        ) as directory:
            # Match pinned_repository: trusted POSIX Git installation may use
            # hardlinks/mounts. Darwin's /usr/bin toolchain dispatchers are also
            # hardlinked; this exception never applies to repository files.
            opened = (
                posix.regular_file(
                    directory.fd, executable.name, trusted_executable=True
                )
                if isinstance(directory, CodingDirectory)
                and (
                    position == 0
                    or (
                        sys.platform == "darwin"
                        and executable.parent == Path("/usr/bin")
                    )
                )
                else backend.regular_file(directory, executable.name)
            )
            with opened:
                if not os.access(executable, os.X_OK):
                    raise CodingError("Configured executable is not executable")
    offset = 0
    while projects := manager.projects.list_projects(limit=100, offset=offset):
        for project in projects:
            root = project.root_path
            if root.is_relative_to(manager.parent) or manager.parent.is_relative_to(
                root
            ):
                raise CodingError("Workspace parent overlaps a registered root")
            if any(executable.is_relative_to(root) for executable in executables):
                raise CodingError("Executables must be outside registered roots")
        offset += len(projects)
