"""Short durable coding metadata transactions; no sessions span tools/inference."""

from datetime import UTC
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from agentforge.coding.models import CodingError
from agentforge.db.models import CodingWorkspaceRecord


class WorkspaceRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def get(self, task_id: UUID):
        try:
            with self.sessions() as session:
                row = session.get(CodingWorkspaceRecord, task_id)
                if row is None:
                    raise CodingError("Coding workspace is unavailable")
                result = {
                    column.name: getattr(row, column.name)
                    for column in row.__table__.columns
                }
                if result["created_at"].tzinfo is None:
                    result["created_at"] = result["created_at"].replace(tzinfo=UTC)
                return result
        except SQLAlchemyError:
            raise CodingError("Coding metadata storage is unavailable") from None

    def add(self, **values):
        try:
            with self.sessions.begin() as session:
                session.add(CodingWorkspaceRecord(**values))
        except SQLAlchemyError:
            raise CodingError(
                "Coding workspace identity already exists or storage is unavailable"
            ) from None

    def update(self, task_id, **values):
        try:
            with self.sessions.begin() as session:
                row = session.get(CodingWorkspaceRecord, task_id)
                if row is None:
                    raise CodingError("Coding workspace is unavailable")
                for key, value in values.items():
                    setattr(row, key, value)
        except SQLAlchemyError:
            raise CodingError("Coding metadata storage is unavailable") from None

    def list(self):
        try:
            with self.sessions() as session:
                return list(session.scalars(select(CodingWorkspaceRecord.task_id)))
        except SQLAlchemyError:
            raise CodingError("Coding metadata storage is unavailable") from None
