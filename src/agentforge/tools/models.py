"""Small immutable tool results, independent of runtime and transport."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FileList:
    paths: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True)
class FileRead:
    path: str
    requested_start_line: int
    requested_end_line: int | None
    start_line: int | None
    end_line: int | None
    content: str
    truncated: bool


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line_number: int
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class SearchResult:
    matches: tuple[SearchMatch, ...]
    truncated: bool
    skipped_binary_files: int = 0


@dataclass(frozen=True)
class GitChange:
    path: str
    index_status: str
    worktree_status: str

    @property
    def staged(self) -> bool:
        return self.index_status not in {" ", "?"}

    @property
    def unstaged(self) -> bool:
        return self.worktree_status not in {" ", "?"}

    @property
    def untracked(self) -> bool:
        return self.index_status == "?"

    @property
    def conflicted(self) -> bool:
        return self.index_status + self.worktree_status in {
            "DD",
            "AU",
            "UD",
            "UA",
            "DU",
            "AA",
            "UU",
        }


@dataclass(frozen=True)
class GitStatus:
    branch: str | None
    detached: bool
    changes: tuple[GitChange, ...]
    truncated: bool


@dataclass(frozen=True)
class GitDiff:
    content: str
    staged: bool
    truncated: bool
