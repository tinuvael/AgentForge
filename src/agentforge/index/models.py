"""Small parser and query values; no persistence or inference dependencies."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

SymbolKind = Literal["module", "class", "function", "method"]
RelationshipKind = Literal["contains", "imports", "calls"]


@dataclass(frozen=True)
class Symbol:
    id: str
    relative_path: str
    kind: SymbolKind
    name: str
    qualified_name: str
    start_line: int
    end_line: int | None
    parent_id: str | None = None
    stale: bool = False
    import_candidate: bool = False


@dataclass(frozen=True)
class Relationship:
    source_id: str
    kind: RelationshipKind
    target_text: str
    target_id: str | None


@dataclass(frozen=True)
class ParsedRelationship:
    source_id: str
    kind: RelationshipKind
    target_text: str
    # Local symbol ID for containment/calls; root-relative qualified name for imports.
    target_key: str | None


@dataclass(frozen=True)
class ParsedFile:
    module_name: str
    symbols: tuple[Symbol, ...]
    relationships: tuple[ParsedRelationship, ...]


@dataclass(frozen=True)
class FileFailure:
    relative_path: str
    message: str
    has_previous_structure: bool


@dataclass(frozen=True)
class IndexStatus:
    project_id: UUID
    indexed_at: datetime | None
    observed_head: str | None
    file_count: int
    symbol_count: int
    failures: tuple[FileFailure, ...]
    # Live content-hash comparison, including additions/deletions; not HEAD based.
    changed_paths: tuple[str, ...]

    @property
    def needs_refresh(self) -> bool:
        return self.indexed_at is None or bool(self.changed_paths or self.failures)


class ProjectIndexError(Exception):
    """Base failure for indexing operations."""


class IndexRefreshError(ProjectIndexError):
    """Filesystem changed during a scan or could not be read safely."""


class IndexStorageError(ProjectIndexError):
    """Database operation failed; database details are not public API."""


class SymbolNotFound(ProjectIndexError):
    """Symbol ID does not exist in this project's current index."""


@dataclass(frozen=True)
class IndexedFile:
    relative_path: str
    module_name: str
    observed_hash: str
    parsed_hash: str | None
    parse_error: str | None


@dataclass(frozen=True)
class FileUpdate:
    relative_path: str
    observed_hash: str
    parsed: ParsedFile | None = None
    parse_error: str | None = None


@dataclass(frozen=True)
class IndexSnapshot:
    indexed_at: datetime | None
    observed_head: str | None
    files: tuple[IndexedFile, ...]
    symbols: tuple[Symbol, ...]
    relationships: tuple[Relationship, ...]
