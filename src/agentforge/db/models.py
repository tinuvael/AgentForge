"""Project configuration and deterministic structural cache records."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from agentforge.db.database import Base


class ProjectRecord(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("root_path", name="uq_projects_root_path"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    root_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    root_device: Mapped[str | None] = mapped_column(Text)
    root_inode: Mapped[str | None] = mapped_column(Text)


class IndexStateRecord(Base):
    __tablename__ = "project_indexes"

    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_head: Mapped[str | None] = mapped_column(String(40))


class IndexedFileRecord(Base):
    __tablename__ = "indexed_files"

    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    relative_path: Mapped[str] = mapped_column(Text, primary_key=True)
    language: Mapped[str] = mapped_column(String(20), default="python")
    module_name: Mapped[str] = mapped_column(Text)
    observed_hash: Mapped[str] = mapped_column(String(64))
    parsed_hash: Mapped[str | None] = mapped_column(String(64))
    parse_error: Mapped[str | None] = mapped_column(Text)


class SymbolRecord(Base):
    __tablename__ = "index_symbols"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "relative_path"],
            ["indexed_files.project_id", "indexed_files.relative_path"],
            ondelete="CASCADE",
        ),
        Index("ix_index_symbols_name", "project_id", "name"),
    )

    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    relative_path: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(Text)
    qualified_name: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int | None] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(Text)
    import_candidate: Mapped[bool] = mapped_column(Boolean)


class RelationshipRecord(Base):
    __tablename__ = "index_relationships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "source_id"],
            ["index_symbols.project_id", "index_symbols.id"],
            ondelete="CASCADE",
        ),
        Index("ix_index_relationships_target", "project_id", "target_id"),
    )

    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    relative_path: Mapped[str] = mapped_column(Text, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))
    target_text: Mapped[str] = mapped_column(Text)
    target_key: Mapped[str | None] = mapped_column(Text)
    # Recomputed transactionally after file replacement, never trusted as a FK.
    target_id: Mapped[str | None] = mapped_column(Text)


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_tasks_state",
        ),
        Index("ix_tasks_created", "created_at", "task_id"),
        Index("ix_tasks_state_created", "state", "created_at", "task_id"),
        Index("ix_tasks_project", "project_id"),
        Index("ix_tasks_agent", "agent_id"),
        Index("ix_tasks_worker", "worker_id"),
    )

    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    # Historical identity survives Project deregistration; intentionally no FK.
    project_id: Mapped[UUID] = mapped_column(Uuid)
    agent_id: Mapped[str] = mapped_column(String(100))
    worker_id: Mapped[str] = mapped_column(Text)
    request: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    reason: Mapped[str | None] = mapped_column(String(40))
    error_code: Mapped[str | None] = mapped_column(String(40))
    execution_result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
