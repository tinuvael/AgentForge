"""Read-only repository capabilities selected exclusively by registered UUID."""

import difflib
import fnmatch
from contextlib import contextmanager
from uuid import UUID

from agentforge.projects.service import ProjectRegistry
from agentforge.tools import filesystem as fs
from agentforge.tools.errors import (
    GitFailure,
    InvalidToolArgument,
    PathNotFound,
    RepositoryIOError,
    SensitivePath,
    UnsupportedTextFile,
)
from agentforge.tools.git_backend import _Git
from agentforge.tools.models import (
    FileList,
    FileRead,
    GitChange,
    GitDiff,
    GitStatus,
    SearchMatch,
    SearchResult,
)
from agentforge.tools.policy import (
    DEFAULT_FILES,
    DEFAULT_LINE_BYTES,
    DEFAULT_LINES,
    DEFAULT_OUTPUT_BYTES,
    DEFAULT_RESULTS,
    FILE_SCAN_BYTES,
    HARD_FILES,
    HARD_LINE_BYTES,
    HARD_LINES,
    HARD_OUTPUT_BYTES,
    HARD_RESULTS,
    SCAN_ENTRIES,
    TOTAL_SEARCH_BYTES,
    clip,
    limit,
    query_text,
    relative_path,
    require_public,
    utf8_size,
)


class RepositoryTools:
    def __init__(self, registry: ProjectRegistry):
        self._registry = registry

    def _parts(self, path):
        parts = relative_path(path)
        self._registry.filesystem.validate_parts(parts)
        self._registry.filesystem.require_public(parts)
        return parts

    def public_index_path(self, path: str) -> bool:
        from agentforge.projects.errors import UnsafeProjectPath

        try:
            self._parts(path)
        except (UnsafeProjectPath, InvalidToolArgument, SensitivePath):
            return False
        return True

    @contextmanager
    def _root(self, project_id: UUID | str):
        try:
            with self._registry.open_root(project_id) as root:
                yield root
        except OSError:
            raise RepositoryIOError("Repository access failed") from None

    def list_files(
        self,
        project_id: UUID | str,
        path: str | None = None,
        *,
        max_results: int = DEFAULT_FILES,
        max_bytes: int = DEFAULT_OUTPUT_BYTES,
    ) -> FileList:
        limit(max_results, HARD_FILES, "max_results")
        limit(max_bytes, HARD_OUTPUT_BYTES, "max_bytes")
        parts = self._parts(path)
        paths = []
        used = 0
        truncated = False
        with self._root(project_id) as (_, fd):
            traversal = fs.files(fd, parts)
            try:
                for entry, _, _ in traversal:
                    size = len(entry.encode("utf-8")) + 1
                    if len(paths) == max_results or used + size > max_bytes:
                        truncated = True
                        break
                    paths.append(entry)
                    used += size
            finally:
                traversal.close()
        return FileList(tuple(paths), truncated)

    def read_file(
        self,
        project_id: UUID | str,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        max_bytes: int = DEFAULT_OUTPUT_BYTES,
        max_lines: int = DEFAULT_LINES,
    ) -> FileRead:
        limit(max_bytes, HARD_OUTPUT_BYTES, "max_bytes")
        limit(max_lines, HARD_LINES, "max_lines")
        limit(start_line, 1_000_000, "start_line")
        if end_line is not None:
            limit(end_line, 1_000_000, "end_line")
            if end_line < start_line:
                raise InvalidToolArgument("end_line must not precede start_line")
        parts = self._parts(path)
        require_public(parts)
        if not parts:
            raise InvalidToolArgument("read_file requires a file path")
        with self._root(project_id) as (_, fd):
            with fs.directory(fd, parts[:-1]) as parent:
                data, source_truncated = fs.read_bytes(
                    parent, parts[-1], until_line=end_line
                )
        text = fs.text_content(data, source_truncated)
        lines = text.splitlines(keepends=True)
        selected = lines[start_line - 1 : end_line]
        line_truncated = len(selected) > max_lines
        content, output_truncated = clip("".join(selected[:max_lines]), max_bytes)
        returned_lines = len(content.splitlines())
        # A scan cap only makes the request incomplete if its end was not reached.
        incomplete = source_truncated and (end_line is None or end_line >= len(lines))
        return FileRead(
            "/".join(parts),
            start_line,
            end_line,
            start_line if returned_lines else None,
            start_line + returned_lines - 1 if returned_lines else None,
            content,
            incomplete or line_truncated or output_truncated,
        )

    def search_code(
        self,
        project_id: UUID | str,
        query: str,
        *,
        path: str | None = None,
        glob: str | None = None,
        case_sensitive: bool = False,
        max_results: int = DEFAULT_RESULTS,
        max_bytes: int = DEFAULT_OUTPUT_BYTES,
        max_line_bytes: int = DEFAULT_LINE_BYTES,
    ) -> SearchResult:
        query_text(query, case_sensitive)
        limit(max_results, HARD_RESULTS, "max_results")
        limit(max_bytes, HARD_OUTPUT_BYTES, "max_bytes")
        limit(max_line_bytes, HARD_LINE_BYTES, "max_line_bytes")
        if glob is not None and (
            not isinstance(glob, str)
            or not glob
            or utf8_size(glob) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in glob)
        ):
            raise InvalidToolArgument("glob must contain 1 to 256 UTF-8 bytes")
        parts = self._parts(path)
        needle = query if case_sensitive else query.casefold()
        matches = []
        used = scanned = visited = skipped = 0
        truncated = False
        with self._root(project_id) as (_, fd):
            traversal = fs.files(fd, parts)
            try:
                for entry, parent, name in traversal:
                    visited += 1
                    if visited > SCAN_ENTRIES or scanned >= TOTAL_SEARCH_BYTES:
                        truncated = True
                        break
                    if glob is not None and not fnmatch.fnmatchcase(entry, glob):
                        continue
                    data, partial = fs.read_bytes(
                        parent, name, min(FILE_SCAN_BYTES, TOTAL_SEARCH_BYTES - scanned)
                    )
                    scanned += len(data)
                    truncated |= partial
                    try:
                        text = fs.text_content(data, partial)
                    except UnsupportedTextFile:
                        skipped += 1
                        continue
                    for number, line in enumerate(text.splitlines(), 1):
                        haystack = line if case_sensitive else line.casefold()
                        if needle not in haystack:
                            continue
                        snippet, clipped = clip(line, max_line_bytes)
                        size = (
                            len(entry.encode("utf-8"))
                            + len(snippet.encode("utf-8"))
                            + 32
                        )
                        if len(matches) == max_results or used + size > max_bytes:
                            return SearchResult(tuple(matches), True, skipped)
                        matches.append(SearchMatch(entry, number, snippet, clipped))
                        used += size
            finally:
                traversal.close()
        return SearchResult(tuple(matches), truncated, skipped)

    def _git_scope(self, path: str | None) -> str:
        parts = self._parts(path)
        require_public(parts)
        return "/".join(parts) or "."

    @staticmethod
    def _changes(git: _Git, scope: str, budget: int):
        output = git.status(scope, budget)
        changes = []
        for record in output.data.split(b"\0")[:-1]:
            try:
                text = record.decode("utf-8")
                path = git._scoped(text[3:])
                if len(text) < 4 or text[2] != " ":
                    raise ValueError
                if path is not None:
                    changes.append(GitChange(path, text[0], text[1]))
            except (ValueError, UnicodeError):
                raise GitFailure("Git status could not be decoded") from None
        return sorted(changes, key=lambda c: c.path), output.truncated

    def git_status(
        self,
        project_id: UUID | str,
        *,
        path: str | None = None,
        max_results: int = DEFAULT_FILES,
        max_bytes: int = DEFAULT_OUTPUT_BYTES,
    ) -> GitStatus:
        limit(max_results, HARD_FILES, "max_results")
        limit(max_bytes, HARD_OUTPUT_BYTES, "max_bytes")
        scope = self._git_scope(path)
        with self._root(project_id) as (project, fd):
            git = _Git(project.root_path, fd)
            branch, detached = git.branch()
            changes, truncated = self._changes(git, scope, max_bytes)
        return GitStatus(
            branch,
            detached,
            tuple(changes[:max_results]),
            truncated or len(changes) > max_results,
        )

    @staticmethod
    def _indexed(git: _Git, scope: str, budget: int):
        output = git.indexed_files(scope, budget)
        entries = []
        for record in output.data.split(b"\0")[:-1]:
            try:
                metadata, name = record.decode("utf-8").split("\t", 1)
                mode, object_id, stage = metadata.split(" ")
                path = git._scoped(name)
                if path is not None and mode in {"100644", "100755"} and stage == "0":
                    entries.append((path, object_id))
            except (ValueError, UnicodeError):
                raise GitFailure("Git index could not be decoded") from None
        return sorted(entries), output.truncated

    def git_grep(
        self,
        project_id: UUID | str,
        query: str,
        *,
        path: str | None = None,
        case_sensitive: bool = False,
        max_results: int = DEFAULT_RESULTS,
        max_bytes: int = DEFAULT_OUTPUT_BYTES,
        max_line_bytes: int = DEFAULT_LINE_BYTES,
    ) -> SearchResult:
        query_text(query, case_sensitive)
        limit(max_results, HARD_RESULTS, "max_results")
        limit(max_bytes, HARD_OUTPUT_BYTES, "max_bytes")
        limit(max_line_bytes, HARD_LINE_BYTES, "max_line_bytes")
        scope = self._git_scope(path)
        matches = []
        used = 0
        with self._root(project_id) as (project, fd):
            git = _Git(project.root_path, fd)
            entries, truncated = self._indexed(git, scope, DEFAULT_OUTPUT_BYTES)
            if not entries:
                return SearchResult((), truncated)
            output = git.grep(query, [p for p, _ in entries], case_sensitive, max_bytes)
            if output.returncode not in {0, 1} and not output.truncated:
                raise GitFailure("Git grep failed")
            truncated |= output.truncated
            # git grep -n -z: filename NUL line-number NUL matching-line LF.
            remaining = output.data
            while remaining:
                try:
                    name, number, rest = remaining.split(b"\0", 2)
                    line, remaining = rest.split(b"\n", 1)
                    entry = git._scoped(name.decode("utf-8"))
                    if entry is None:
                        continue
                    snippet, partial = clip(fs.text_content(line), max_line_bytes)
                    size = (
                        len(entry.encode("utf-8")) + len(snippet.encode("utf-8")) + 32
                    )
                    if len(matches) == max_results or used + size > max_bytes:
                        truncated = True
                        break
                    matches.append(SearchMatch(entry, int(number), snippet, partial))
                    used += size
                except UnsupportedTextFile:
                    continue
                except (ValueError, UnicodeError):
                    if output.truncated:
                        break
                    raise GitFailure("Git grep output could not be decoded") from None
        return SearchResult(
            tuple(sorted(matches, key=lambda m: (m.path, m.line_number))), truncated
        )

    def git_diff(
        self,
        project_id: UUID | str,
        *,
        path: str | None = None,
        staged: bool = False,
        max_bytes: int = DEFAULT_OUTPUT_BYTES,
    ) -> GitDiff:
        if type(staged) is not bool:
            raise InvalidToolArgument("staged must be a boolean")
        limit(max_bytes, HARD_OUTPUT_BYTES, "max_bytes")
        scope = self._git_scope(path)
        chunks = []
        used = 0
        with self._root(project_id) as (project, fd):
            git = _Git(project.root_path, fd)
            changes, truncated = self._changes(git, scope, DEFAULT_OUTPUT_BYTES)
            truncated |= any(c.conflicted for c in changes)
            changed = (
                [c.path for c in changes if c.staged]
                if staged
                else [c.path for c in changes if c.unstaged and not c.conflicted]
            )
            if len(changed) > DEFAULT_RESULTS:
                changed = changed[:DEFAULT_RESULTS]
                truncated = True
            if staged:
                if not changed:
                    return GitDiff("", True, truncated)
                output = git.staged_diff(changed, max_bytes)
                # Strict textual decoding; never publish arbitrary binary bytes.
                content = fs.text_content(output.data, output.truncated)
                return GitDiff(content, True, truncated or output.truncated)
            entries, partial = self._indexed(git, scope, DEFAULT_OUTPUT_BYTES)
            truncated |= partial
            objects = dict(entries)
            for entry in changed:
                if used >= max_bytes:
                    truncated = True
                    break
                if entry not in objects:
                    # Symlink, submodule and conflict content is unsupported.
                    truncated = True
                    continue
                old = git.blob(objects[entry], FILE_SCAN_BYTES)
                parts = tuple(entry.split("/"))
                try:
                    with fs.directory(fd, parts[:-1]) as parent:
                        data, partial = fs.read_bytes(parent, parts[-1])
                except PathNotFound:
                    data, partial = b"", False
                if old.truncated or partial:
                    # A prefix-only comparison would fabricate a complete diff.
                    truncated = True
                    continue
                try:
                    before = fs.text_content(old.data).splitlines(keepends=True)
                    after = fs.text_content(data).splitlines(keepends=True)
                except UnsupportedTextFile:
                    snippet = f"Binary/non-text file differs: {entry}\n"
                    lines = iter((snippet,))
                else:
                    if len(before) > HARD_LINES or len(after) > HARD_LINES:
                        truncated = True
                        continue
                    lines = (
                        iter((f"Metadata-only change: {entry}\n",))
                        if before == after
                        else difflib.unified_diff(
                            before, after, fromfile="a/" + entry, tofile="b/" + entry
                        )
                    )
                for line in lines:
                    if not line.endswith("\n"):
                        line += "\n\\ No newline at end of file\n"
                    snippet, partial = clip(line, max_bytes - used)
                    chunks.append(snippet)
                    used += len(snippet.encode("utf-8"))
                    if partial:
                        truncated = True
                        break
        return GitDiff("".join(chunks), False, truncated)
