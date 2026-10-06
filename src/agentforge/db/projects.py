"""Small project storage boundary with operation-scoped transactions."""

from datetime import UTC
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agentforge.db.models import ProjectRecord
from agentforge.projects.errors import ProjectAlreadyRegistered, ProjectStorageError
from agentforge.projects.models import Project


def _project(record: ProjectRecord) -> Project:
    # SQLite drops timezone offsets; all writes use UTC, as do returned values.
    created_at = record.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return Project(
        record.id, record.name, Path(record.root_path), created_at.astimezone(UTC)
    )


class ProjectRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def add(self, project: Project) -> Project:
        try:
            with self._sessions() as session:
                session.add(
                    ProjectRecord(
                        id=project.id,
                        name=project.name,
                        root_path=str(project.root_path),
                        created_at=project.created_at,
                    )
                )
                session.commit()
            return project
        except IntegrityError:
            # Check after rollback in a new session: only a root collision is a
            # duplicate, including another registrar winning a concurrent insert.
            if self.find_by_root(project.root_path) is not None:
                raise ProjectAlreadyRegistered(
                    "Project root is already registered"
                ) from None
            raise ProjectStorageError("Could not store project") from None
        except SQLAlchemyError:
            raise ProjectStorageError("Could not store project") from None

    def find_by_root(self, root: Path) -> Project | None:
        try:
            with self._sessions() as session:
                record = session.scalar(
                    select(ProjectRecord).where(ProjectRecord.root_path == str(root))
                )
                return _project(record) if record else None
        except SQLAlchemyError:
            raise ProjectStorageError("Could not read project") from None

    def get(self, project_id: UUID) -> Project | None:
        try:
            with self._sessions() as session:
                record = session.get(ProjectRecord, project_id)
                return _project(record) if record else None
        except SQLAlchemyError:
            raise ProjectStorageError("Could not read project") from None

    def list(self) -> list[Project]:
        try:
            with self._sessions() as session:
                records = session.scalars(
                    select(ProjectRecord).order_by(
                        ProjectRecord.created_at, ProjectRecord.id
                    )
                )
                return [_project(record) for record in records]
        except SQLAlchemyError:
            raise ProjectStorageError("Could not list projects") from None

    def remove(self, project_id: UUID) -> bool:
        try:
            with self._sessions() as session:
                result = session.execute(
                    delete(ProjectRecord).where(ProjectRecord.id == project_id)
                )
                session.commit()
                return result.rowcount > 0
        except SQLAlchemyError:
            raise ProjectStorageError("Could not remove project") from None
