"""First-release schema: configuration, structural cache and durable history."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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


class WorkerDiagnosticRecord(Base):
    """One bounded factual checkpoint per stable Worker ID and probe kind."""

    __tablename__ = "worker_diagnostic_observations"
    __table_args__ = (
        CheckConstraint(
            "probe_kind IN ('health', 'generation', 'tools', 'streaming')",
            name="ck_worker_diagnostics_probe_kind",
        ),
    )

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    probe_kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    configuration_fingerprint: Mapped[str] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latest: Mapped[dict] = mapped_column(JSON(none_as_null=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectRecord(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("root_path", name="uq_projects_root_path"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    root_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    root_identity: Mapped[dict] = mapped_column(JSON(none_as_null=True))


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
        CheckConstraint(
            "telemetry_status IN ('pending', 'recorded', 'unavailable')",
            name="ck_tasks_telemetry_status",
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
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    telemetry_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    queue_duration_seconds: Mapped[float | None] = mapped_column(Float)


class TaskTelemetryRecord(Base):
    """Immutable terminal observations; intentionally no cascading identity FKs."""

    __tablename__ = "task_telemetry"
    __table_args__ = (
        CheckConstraint(
            "state IN ('completed', 'failed', 'cancelled')",
            name="ck_task_telemetry_state",
        ),
        Index("ix_task_telemetry_created", "created_at", "task_id"),
        Index(
            "ix_task_telemetry_project_created", "project_id", "created_at", "task_id"
        ),
        Index("ix_task_telemetry_agent_created", "agent_id", "created_at", "task_id"),
        Index("ix_task_telemetry_worker_created", "worker_id", "created_at", "task_id"),
        Index(
            "ix_task_telemetry_provider_created", "provider", "created_at", "task_id"
        ),
        Index("ix_task_telemetry_model_created", "model", "created_at", "task_id"),
        Index("ix_task_telemetry_state_created", "state", "created_at", "task_id"),
    )

    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    agent_id: Mapped[str] = mapped_column(String(100))
    worker_id: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(40))
    error_category: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    queue_duration_seconds: Mapped[float | None] = mapped_column(Float)
    execution_duration_seconds: Mapped[float | None] = mapped_column(Float)
    total_duration_seconds: Mapped[float | None] = mapped_column(Float)
    model_call_count: Mapped[int | None] = mapped_column(Integer)
    model_request_duration_seconds: Mapped[float | None] = mapped_column(Float)
    backend_total_duration_seconds: Mapped[float | None] = mapped_column(Float)
    model_load_duration_seconds: Mapped[float | None] = mapped_column(Float)
    prompt_evaluation_duration_seconds: Mapped[float | None] = mapped_column(Float)
    generation_duration_seconds: Mapped[float | None] = mapped_column(Float)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    observed_prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    observed_completion_tokens: Mapped[int | None] = mapped_column(Integer)
    prompt_observed_turns: Mapped[int] = mapped_column(Integer)
    completion_observed_turns: Mapped[int] = mapped_column(Integer)
    token_usage_complete: Mapped[bool] = mapped_column(Boolean)
    ttft_seconds: Mapped[float | None] = mapped_column(Float)
    tokens_per_second: Mapped[float | None] = mapped_column(Float)
    tool_call_count: Mapped[int | None] = mapped_column(Integer)
    total_tool_duration_seconds: Mapped[float | None] = mapped_column(Float)
    tool_output_bytes: Mapped[int | None] = mapped_column(Integer)


class CouncilRecord(Base):
    __tablename__ = "councils"
    __table_args__ = (Index("ix_councils_created", "created_at", "council_id"),)

    council_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    # Like Tasks, history survives Project deregistration.
    project_id: Mapped[UUID] = mapped_column(Uuid)
    agent_id: Mapped[str] = mapped_column(String(100))
    request: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CouncilParticipantRecord(Base):
    __tablename__ = "council_participants"
    __table_args__ = (
        UniqueConstraint("council_id", "worker_id", name="uq_council_worker"),
        UniqueConstraint("task_id", name="uq_council_task"),
        CheckConstraint("ordinal >= 0 AND ordinal < 16", name="ck_council_ordinal"),
    )

    council_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("councils.council_id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    # RESTRICT/NO ACTION: deleting membership never deletes Tasks, and a referenced
    # Task cannot disappear leaving an accidental incomplete historical Council.
    task_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("tasks.task_id"))
    worker_id: Mapped[str] = mapped_column(Text)


class CodingWorkspaceRecord(Base):
    """Private lifecycle metadata, never source or complete diffs."""

    __tablename__ = "coding_workspaces"
    __table_args__ = (
        UniqueConstraint("branch_name", "repository_path", name="uq_coding_branch"),
        UniqueConstraint("workspace_id", name="uq_coding_workspace_id"),
        CheckConstraint(
            "state IN ('provisioning', 'ready', 'completed', 'failed', 'cancelled', "
            "'interrupted', 'cleanup_pending', 'removed', 'suspicious')",
            name="ck_coding_workspace_state",
        ),
    )

    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    worker_id: Mapped[str] = mapped_column(Text)
    branch_name: Mapped[str] = mapped_column(Text)
    base_commit: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32))
    repository_path: Mapped[str] = mapped_column(Text)
    worktree_path: Mapped[str] = mapped_column(Text)
    prefix: Mapped[str] = mapped_column(Text)
    identities: Mapped[dict] = mapped_column(JSON)
    observations: Mapped[dict] = mapped_column(JSON)
