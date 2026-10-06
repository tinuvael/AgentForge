"""Transport-independent refresh, structural queries and compact map operations."""

from uuid import UUID

from agentforge.db.index import IndexRepository
from agentforge.index.models import (
    FileFailure,
    FileUpdate,
    IndexSnapshot,
    IndexStatus,
    Relationship,
    Symbol,
    SymbolNotFound,
)
from agentforge.index.python_parser import parse_python
from agentforge.index.render import render_map
from agentforge.index.scanner import content_hash, scan_python_files
from agentforge.projects.service import ProjectRegistry


class ProjectIndex:
    def __init__(self, registry: ProjectRegistry, repository: IndexRepository):
        self._registry = registry
        self._repository = repository

    def _snapshot(self, project_id: UUID | str) -> tuple[UUID, IndexSnapshot]:
        project = self._registry.get_project(project_id)
        return project.id, self._repository.read(project.id)

    def refresh_index(self, project_id: UUID | str) -> IndexStatus:
        inspection = self._registry.inspect_project(project_id)
        project = inspection.project

        def updates(hashes: dict[str, str]):
            for path, content in scan_python_files(project.root_path):
                digest = content_hash(content)
                if hashes.get(path) == digest:
                    yield FileUpdate(path, digest)
                    continue
                try:
                    parsed = parse_python(path, content)
                except (SyntaxError, ValueError, UnicodeError) as error:
                    # Do not persist source excerpts or arbitrary parser messages.
                    line = getattr(error, "lineno", None)
                    message = "Invalid Python source" + (
                        f" at line {line}" if line else ""
                    )
                    yield FileUpdate(path, digest, parse_error=message)
                else:
                    yield FileUpdate(path, digest, parsed=parsed)

        self._repository.refresh(project.id, updates, inspection.git.head_commit)
        snapshot = self._repository.read(project.id)
        return self._status(project.id, snapshot, ())

    @staticmethod
    def _status(
        identity: UUID, snapshot: IndexSnapshot, changed: tuple[str, ...]
    ) -> IndexStatus:
        return IndexStatus(
            identity,
            snapshot.indexed_at,
            snapshot.observed_head,
            len(snapshot.files),
            sum(s.kind != "module" for s in snapshot.symbols),
            tuple(
                FileFailure(f.relative_path, f.parse_error, f.parsed_hash is not None)
                for f in snapshot.files
                if f.parse_error
            ),
            changed,
        )

    def get_index_status(self, project_id: UUID | str) -> IndexStatus:
        """Scan live hashes without silently refreshing structural records."""
        project = self._registry.get_project(project_id)
        snapshot = self._repository.read(project.id)
        stored = {f.relative_path: f.observed_hash for f in snapshot.files}
        live = {
            path: content_hash(content)
            for path, content in scan_python_files(project.root_path)
        }
        changed = tuple(
            sorted(
                path
                for path in stored.keys() | live.keys()
                if stored.get(path) != live.get(path)
            )
        )
        return self._status(project.id, snapshot, changed)

    def find_symbol(self, project_id: UUID | str, query: str) -> list[Symbol]:
        """Case-insensitive exact simple/qualified matches, else substring matches.

        All ambiguous matches are returned in stable path/line/ID order. Empty
        queries return no results. Module symbols are included in these queries.
        """
        _, snapshot = self._snapshot(project_id)
        query = query.strip().casefold()
        if not query:
            return []
        exact = [
            s
            for s in snapshot.symbols
            if query in {s.name.casefold(), s.qualified_name.casefold()}
        ]
        return exact or [
            s for s in snapshot.symbols if query in s.qualified_name.casefold()
        ]

    @staticmethod
    def _symbol(snapshot: IndexSnapshot, symbol_id: str) -> Symbol:
        for symbol in snapshot.symbols:
            if symbol.id == symbol_id:
                return symbol
        raise SymbolNotFound("Symbol ID is not present in this project index")

    def get_symbol(self, project_id: UUID | str, symbol_id: str) -> Symbol:
        _, snapshot = self._snapshot(project_id)
        return self._symbol(snapshot, symbol_id)

    def get_dependencies(
        self, project_id: UUID | str, symbol_id: str
    ) -> list[Relationship]:
        """Outgoing resolved edges, including containment; retain each edge's kind."""
        _, snapshot = self._snapshot(project_id)
        self._symbol(snapshot, symbol_id)
        return [
            e
            for e in snapshot.relationships
            if e.source_id == symbol_id and e.target_id
        ]

    def get_dependents(
        self, project_id: UUID | str, symbol_id: str
    ) -> list[Relationship]:
        """Incoming resolved edges, including containment."""
        _, snapshot = self._snapshot(project_id)
        self._symbol(snapshot, symbol_id)
        return [e for e in snapshot.relationships if e.target_id == symbol_id]

    def get_related_symbols(
        self, project_id: UUID | str, symbol_id: str
    ) -> list[Relationship]:
        """Union of incoming/outgoing resolved edges, with direction and kind intact."""
        _, snapshot = self._snapshot(project_id)
        self._symbol(snapshot, symbol_id)
        return [
            e
            for e in snapshot.relationships
            if e.target_id and (e.source_id == symbol_id or e.target_id == symbol_id)
        ]

    def get_relationships(self, project_id: UUID | str) -> list[Relationship]:
        """Include textual imports/simple calls; None means no known target."""
        _, snapshot = self._snapshot(project_id)
        return list(snapshot.relationships)

    def render_project_map(
        self, project_id: UUID | str, focus: str | None = None, max_tokens: int = 3000
    ) -> str:
        _, snapshot = self._snapshot(project_id)
        return render_map(snapshot, focus, max_tokens)
