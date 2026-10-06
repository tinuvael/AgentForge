"""Project Index persistence with a single transaction per complete refresh."""

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agentforge.db.models import (
    IndexedFileRecord,
    IndexStateRecord,
    ProjectRecord,
    RelationshipRecord,
    SymbolRecord,
)
from agentforge.index.models import (
    FileUpdate,
    IndexedFile,
    IndexSnapshot,
    IndexStorageError,
    Relationship,
    Symbol,
)
from agentforge.projects.errors import ProjectNotFound


class IndexRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self._sessions = sessions

    def read(self, project_id: UUID) -> IndexSnapshot:
        try:
            with self._sessions() as session:
                state = session.get(IndexStateRecord, project_id)
                files = list(
                    session.scalars(
                        select(IndexedFileRecord)
                        .where(IndexedFileRecord.project_id == project_id)
                        .order_by(IndexedFileRecord.relative_path)
                    )
                )
                stale_paths = {file.relative_path for file in files if file.parse_error}
                symbols = session.scalars(
                    select(SymbolRecord)
                    .where(SymbolRecord.project_id == project_id)
                    .order_by(
                        SymbolRecord.relative_path,
                        SymbolRecord.start_line,
                        SymbolRecord.id,
                    )
                )
                edges = session.scalars(
                    select(RelationshipRecord)
                    .where(RelationshipRecord.project_id == project_id)
                    .order_by(
                        RelationshipRecord.relative_path, RelationshipRecord.ordinal
                    )
                )
                indexed_at = state.indexed_at if state else None
                if indexed_at and indexed_at.tzinfo is None:
                    indexed_at = indexed_at.replace(tzinfo=UTC)
                return IndexSnapshot(
                    indexed_at,
                    state.observed_head if state else None,
                    tuple(
                        IndexedFile(
                            f.relative_path,
                            f.module_name,
                            f.observed_hash,
                            f.parsed_hash,
                            f.parse_error,
                        )
                        for f in files
                    ),
                    tuple(
                        Symbol(
                            s.id,
                            s.relative_path,
                            s.kind,
                            s.name,
                            s.qualified_name,
                            s.start_line,
                            s.end_line,
                            s.parent_id,
                            s.relative_path in stale_paths,
                            s.import_candidate,
                        )
                        for s in symbols
                    ),
                    tuple(
                        Relationship(e.source_id, e.kind, e.target_text, e.target_id)
                        for e in edges
                    ),
                )
        except SQLAlchemyError:
            raise IndexStorageError("Could not read project index") from None

    def refresh(
        self,
        project_id: UUID,
        prepare: Callable[[dict[str, str]], Iterable[FileUpdate]],
        head: str | None,
    ):
        """Consume the scan inside the transaction; any failed scan rolls back.

        Unchanged files have no parsed value or error. Syntax errors update observed
        hashes/errors while preserving last valid symbols and extraction facts.
        Resolution is recomputed from cached facts, so new/deleted modules affect
        imports in unchanged files too. Target IDs are always validated explicitly.
        """
        try:
            with self._sessions.begin() as session:
                if session.get(ProjectRecord, project_id) is None:
                    raise ProjectNotFound("Project ID is not registered")
                existing = {
                    f.relative_path: f
                    for f in session.scalars(
                        select(IndexedFileRecord).where(
                            IndexedFileRecord.project_id == project_id
                        )
                    )
                }
                seen = set()
                hashes = {path: file.observed_hash for path, file in existing.items()}
                for update in prepare(hashes):
                    path = update.relative_path
                    seen.add(path)
                    file = existing.get(path)
                    if file is None:
                        file = IndexedFileRecord(
                            project_id=project_id,
                            relative_path=path,
                            module_name="",
                            language="python",
                        )
                        session.add(file)
                    file.observed_hash = update.observed_hash
                    if update.parse_error:
                        file.parse_error = update.parse_error
                    elif update.parsed:
                        file.module_name = update.parsed.module_name
                        file.parsed_hash = update.observed_hash
                        file.parse_error = None
                        session.flush()
                        session.execute(
                            delete(SymbolRecord).where(
                                SymbolRecord.project_id == project_id,
                                SymbolRecord.relative_path == path,
                            )
                        )
                        session.add_all(
                            SymbolRecord(
                                project_id=project_id,
                                id=s.id,
                                relative_path=path,
                                kind=s.kind,
                                name=s.name,
                                qualified_name=s.qualified_name,
                                start_line=s.start_line,
                                end_line=s.end_line,
                                parent_id=s.parent_id,
                                import_candidate=s.import_candidate,
                            )
                            for s in update.parsed.symbols
                        )
                        session.flush()
                        session.add_all(
                            RelationshipRecord(
                                project_id=project_id,
                                relative_path=path,
                                ordinal=i,
                                source_id=edge.source_id,
                                kind=edge.kind,
                                target_text=edge.target_text,
                                target_key=edge.target_key,
                                target_id=None,
                            )
                            for i, edge in enumerate(update.parsed.relationships)
                        )
                for path in existing.keys() - seen:
                    session.execute(
                        delete(IndexedFileRecord).where(
                            IndexedFileRecord.project_id == project_id,
                            IndexedFileRecord.relative_path == path,
                        )
                    )
                session.flush()
                symbols = list(
                    session.scalars(
                        select(SymbolRecord).where(
                            SymbolRecord.project_id == project_id
                        )
                    )
                )
                by_name = defaultdict(list)
                ids = {symbol.id for symbol in symbols}
                for symbol in symbols:
                    if symbol.import_candidate:
                        by_name[symbol.qualified_name].append(symbol.id)
                for edge in session.scalars(
                    select(RelationshipRecord).where(
                        RelationshipRecord.project_id == project_id
                    )
                ):
                    if edge.kind == "imports":
                        matches = by_name.get(edge.target_key, [])
                        edge.target_id = matches[0] if len(matches) == 1 else None
                    else:
                        edge.target_id = (
                            edge.target_key if edge.target_key in ids else None
                        )
                state = session.get(IndexStateRecord, project_id)
                if state is None:
                    state = IndexStateRecord(project_id=project_id)
                    session.add(state)
                state.indexed_at = datetime.now(UTC)
                state.observed_head = head
        except SQLAlchemyError:
            raise IndexStorageError("Could not refresh project index") from None
