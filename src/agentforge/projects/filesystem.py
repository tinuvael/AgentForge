"""No-follow, descriptor-anchored filesystem primitives for registered roots."""

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from agentforge.projects.errors import UnsafeProjectPath
from agentforge.projects.exclusions import excluded_directory
from agentforge.projects.paths import validate_registered_root


def identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


@contextmanager
def anchored_root(root: Path, expected: tuple[int, int] | None = None):
    """Anchor every ancestor, and reject replacement before returning any result."""
    validate_registered_root(root)
    if (
        not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.scandir not in os.supports_fd
    ):
        raise UnsafeProjectPath("Safe access requires POSIX no-follow descriptors")
    descriptors = []
    links = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(root.anchor, flags)
        descriptors.append(fd)
        for component in root.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            descriptors.append(child)
            links.append((fd, component, child))
            fd = child
        if expected is not None and identity(os.fstat(fd)) != expected:
            raise UnsafeProjectPath("Registered project root was replaced")
        yield fd
        for parent, name, child in links:
            if identity(os.fstat(child)) != identity(
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            ):
                raise UnsafeProjectPath("Project ancestry changed during access")
        validate_registered_root(root)
    except OSError:
        raise UnsafeProjectPath("Project could not be accessed safely") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


@contextmanager
def anchored_directory(root_fd: int, parts: tuple[str, ...]):
    descriptors = []
    links = []
    fd = root_fd
    try:
        for name in parts:
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            descriptors.append(child)
            links.append((fd, name, child))
            fd = child
        try:
            yield fd
        finally:
            # Traversals can stop early on an output limit: generator.close must
            # still verify every directory whose descriptor was used.
            for parent, name, child in links:
                if identity(os.fstat(child)) != identity(
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                ):
                    raise UnsafeProjectPath("Directory changed during access")
    finally:
        for child in reversed(descriptors):
            os.close(child)


@contextmanager
def regular_file(directory_fd: int, name: str):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as file:
        before = os.fstat(file.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeProjectPath("Only regular files can be read")
        yield file
        after = os.fstat(file.fileno())
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if identity(before) != identity(current) or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise UnsafeProjectPath("File changed during access")


def walk_files(
    directory_fd: int,
    parts: tuple[str, ...] = (),
    *,
    include: Callable[[tuple[str, ...]], bool] | None = None,
) -> Iterator[tuple[str, int, str]]:
    """Yield paths and live parent descriptors, skipping symlinks/special files."""
    with os.scandir(directory_fd) as entries:
        names = sorted(entry.name for entry in entries)
    candidates = []
    for name in names:
        if include is not None and not include((*parts, name)):
            continue
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(observed.st_mode):
            if not excluded_directory(name):
                candidates.append((name + "/", name, True))
        elif stat.S_ISREG(observed.st_mode):
            candidates.append((name, name, False))
    for _, name, directory in sorted(candidates):
        if directory:
            with anchored_directory(directory_fd, (name,)) as child:
                yield from walk_files(child, (*parts, name), include=include)
        else:
            yield "/".join((*parts, name)), directory_fd, name
