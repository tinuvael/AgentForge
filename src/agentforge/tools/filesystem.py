"""Bounded source reads using the same descriptors as the Project Index."""

import codecs
import errno
import os
import stat
from contextlib import contextmanager

from agentforge.projects.errors import UnsafeProjectPath
from agentforge.projects.exclusions import excluded_directory
from agentforge.projects.filesystem import anchored_directory, regular_file, walk_files
from agentforge.tools.errors import (
    InvalidToolArgument,
    PathNotFound,
    RepositoryIOError,
    UnsupportedTextFile,
)
from agentforge.tools.policy import (
    FILE_SCAN_BYTES,
    automatic,
    relative_path,
    require_public,
    sensitive,
)


@contextmanager
def directory(root_fd: int, parts: tuple[str, ...]):
    try:
        with anchored_directory(root_fd, parts) as fd:
            yield fd
    except FileNotFoundError:
        raise PathNotFound("Project path does not exist") from None
    except OSError:
        raise UnsafeProjectPath("Directory cannot be safely accessed") from None


def files(root_fd: int, parts: tuple[str, ...]):
    require_public(parts)
    with directory(root_fd, parts) as fd:
        if any(excluded_directory(p) for p in parts):
            return

        def include(candidate):
            if sensitive(candidate):
                return False
            try:
                relative_path("/".join(candidate))
            except (InvalidToolArgument, UnsafeProjectPath):
                return False
            return True

        traversal = walk_files(fd, parts, include=include)
        try:
            for path, parent, name in traversal:
                if automatic(tuple(path.split("/"))):
                    yield path, parent, name
        finally:
            traversal.close()


def read_bytes(
    parent_fd: int,
    name: str,
    budget: int = FILE_SCAN_BYTES,
    *,
    until_line: int | None = None,
):
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(observed.st_mode):
            raise InvalidToolArgument("read_file requires a regular file")
        if not stat.S_ISREG(observed.st_mode):
            raise UnsafeProjectPath("Symlinks and special files are denied")
        with regular_file(parent_fd, name) as file:
            if until_line is None:
                data = file.read(budget + 1)
            else:
                pieces = bytearray()
                for _ in range(until_line):
                    line = file.readline(budget + 1 - len(pieces))
                    pieces.extend(line)
                    if not line or len(pieces) > budget:
                        break
                data = bytes(pieces)
        return data[:budget], len(data) > budget
    except FileNotFoundError:
        raise PathNotFound("Project file does not exist") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise UnsafeProjectPath("Symlink replacement was rejected") from None
        raise RepositoryIOError("Project file could not be read safely") from None


def text_content(data: bytes, truncated: bool = False) -> str:
    try:
        # An incomplete final UTF-8 character at the scan cap is not binary.
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        text = decoder.decode(data, final=not truncated)
    except UnicodeError:
        raise UnsupportedTextFile("Only UTF-8 text files are supported") from None
    if any((ord(c) < 32 or 127 <= ord(c) <= 159) and c not in "\t\n\r" for c in text):
        raise UnsupportedTextFile("Binary or control-character content is unsupported")
    return text
