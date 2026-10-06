"""Small project storage boundary with operation-scoped transactions."""

from datetime import UTC
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agentforge.db.models import ProjectRecord
from agentforge.projects.errors import ProjectAlreadyRegistered, ProjectStorageError
from agentforge.projects.identity import RootIdentity
from agentforge.projects.models import Project


def _project(record: ProjectRecord) -> Project:
    # SQLite drops timezone offsets; all writes use UTC, as do returned values.
    created_at = record.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return Project(
        record.id,
        record.name,
        Path(record.root_path),
        created_at.astimezone(UTC),
        int(record.root_device) if record.root_device is not None else None,
        int(record.root_inode) if record.root_inode is not None else None,
        RootIdentity.from_json(record.root_identity)
        if record.root_identity is not None
        else None,
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
                        root_device=str(project.root_device)
                        if project.root_device is not None
                        else None,
                        root_inode=str(project.root_inode)
                        if project.root_inode is not None
                        else None,
                        root_identity=project.root_identity.as_json()
                        if project.root_identity is not None
                        else None,
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

    def count(self) -> int:
        try:
            with self._sessions() as session:
                return session.scalar(select(func.count()).select_from(ProjectRecord))
        except SQLAlchemyError:
            raise ProjectStorageError("Could not count projects") from None

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[Project]:
        try:
            with self._sessions() as session:
                records = session.scalars(
                    select(ProjectRecord)
                    .order_by(ProjectRecord.created_at, ProjectRecord.id)
                    .limit(limit)
                    .offset(offset)
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
