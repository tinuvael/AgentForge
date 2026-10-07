"""Task storage with short transactions and conditional lifecycle checkpoints."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agentforge.agents.models import ExecutionObservations, ExecutionResult, TraceEvent
from agentforge.db.models import ProjectRecord, TaskRecord, TaskTelemetryRecord
from agentforge.db.telemetry import record_checkpoint
from agentforge.tasks.models import (
    Task,
    TaskHistoryItem,
    TaskNotFound,
    TaskReason,
    TaskState,
    TaskStorageError,
    TaskValidationError,
    validate_transition,
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _task(record: TaskRecord) -> Task:
    return Task(
        **{
            name: _utc(getattr(record, name))
            if name.endswith("_at")
            else getattr(record, name)
            for name in Task.model_fields
        }
    )


def cancelled_result(result: ExecutionResult) -> ExecutionResult:
    """Cancellation won the durable completion race; discard a late answer.

    Preserve runtime metadata and add an explicit cancellation boundary if the
    runtime returned before observing the signal.
    """
    trace = result.trace
    if result.reason != "cancelled":
        trace += (
            TraceEvent(step=result.steps, kind="termination", reason="cancelled"),
        )
    return result.model_copy(
        update={
            "state": "cancelled",
            "reason": "cancelled",
            "final_answer": None,
            "trace": trace,
        }
    )


class TaskRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions
        # One active Task Engine per database in this process (no distributed lease).
        bind = sessions.kw["bind"]
        url = bind.url
        self.ownership_key = (
            ("sqlite", str(Path(url.database).resolve()))
            if url.get_backend_name() == "sqlite"
            and url.database not in (None, "", ":memory:")
            else bind
        )

    @contextmanager
    def _session(self):
        try:
            with self._sessions() as session:
                yield session
        except SQLAlchemyError:
            raise TaskStorageError("Could not access Task storage") from None

    @staticmethod
    def _checkpoint(session, task_id, source, target, **values) -> Task | None:
        validate_transition(source, target)
        now = datetime.now(UTC)
        record = session.scalar(
            update(TaskRecord)
            .where(TaskRecord.task_id == task_id, TaskRecord.state == source)
            .values(state=target, updated_at=now, **values)
            .returning(TaskRecord),
            execution_options={"synchronize_session": False},
        )
        return _task(record) if record else None

    def add(
        self,
        *,
        project_id: UUID,
        agent_id: str,
        worker_id: str,
        request: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> Task:
        now = datetime.now(UTC)
        with self._session() as session:
            record = TaskRecord(
                task_id=uuid4(),
                project_id=project_id,
                agent_id=agent_id,
                worker_id=worker_id,
                request=request,
                state="queued",
                created_at=now,
                updated_at=now,
                provider=provider,
                model=model,
            )
            session.add(record)
            session.commit()
            return _task(record)

    def get(self, task_id: UUID) -> Task:
        with self._session() as session:
            record = session.get(TaskRecord, task_id)
            if record is None:
                raise TaskNotFound("Task ID is not registered")
            return _task(record)

    def list(
        self,
        *,
        state: TaskState | None = None,
        project_id: UUID | None = None,
        agent_id: str | None = None,
        worker_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Task]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 1000
            or type(offset) is not int
            or offset < 0
        ):
            raise TaskValidationError("Invalid Task list bounds")
        if state is not None and state not in {
            "queued",
            "running",
            "completed",
            "failed",
            "cancelled",
        }:
            raise TaskValidationError("Invalid Task state filter")
        query = select(TaskRecord)
        for name, value in (
            ("state", state),
            ("project_id", project_id),
            ("agent_id", agent_id),
            ("worker_id", worker_id),
        ):
            if value is not None:
                query = query.where(getattr(TaskRecord, name) == value)
        query = query.order_by(TaskRecord.created_at.desc(), TaskRecord.task_id.desc())
        with self._session() as session:
            return [
                _task(row) for row in session.scalars(query.limit(limit).offset(offset))
            ]

    def active_counts(self) -> dict[TaskState, int]:
        with self._session() as session:
            return dict(
                session.execute(
                    select(TaskRecord.state, func.count())
                    .where(TaskRecord.state.in_(("queued", "running")))
                    .group_by(TaskRecord.state)
                ).all()
            )

    def state_counts(self) -> dict[TaskState, int]:
        with self._session() as session:
            return dict(
                session.execute(
                    select(TaskRecord.state, func.count()).group_by(TaskRecord.state)
                ).all()
            )

    def history(
        self,
        *,
        state: TaskState | None = None,
        project_id: UUID | None = None,
        agent_id: str | None = None,
        worker_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[TaskHistoryItem]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 101
            or type(offset) is not int
            or not 0 <= offset <= 1_000_000
            or state is not None
            and state not in {"queued", "running", "completed", "failed", "cancelled"}
        ):
            raise TaskValidationError("Invalid Task history query")
        row = TaskRecord
        query = (
            select(
                row.task_id,
                row.project_id,
                ProjectRecord.name.label("project_name"),
                row.agent_id,
                row.worker_id,
                row.state,
                row.created_at,
                row.started_at,
                row.finished_at,
                row.error_code,
                TaskTelemetryRecord.execution_duration_seconds,
            )
            .outerjoin(ProjectRecord, row.project_id == ProjectRecord.id)
            .outerjoin(TaskTelemetryRecord, row.task_id == TaskTelemetryRecord.task_id)
        )
        for name, value in (
            ("state", state),
            ("project_id", project_id),
            ("agent_id", agent_id),
            ("worker_id", worker_id),
        ):
            if value is not None:
                query = query.where(getattr(row, name) == value)
        query = query.order_by(row.created_at.desc(), row.task_id.desc())
        with self._session() as session:
            return [
                TaskHistoryItem(
                    **{
                        name: _utc(value) if name.endswith("_at") else value
                        for name, value in values.items()
                    }
                )
                for values in session.execute(
                    query.limit(limit).offset(offset)
                ).mappings()
            ]

    def next_queued_id(self) -> UUID | None:
        with self._session() as session:
            return session.scalar(
                select(TaskRecord.task_id)
                .where(TaskRecord.state == "queued")
                .order_by(TaskRecord.created_at, TaskRecord.task_id)
                .limit(1)
            )

    def claim(
        self,
        task_id: UUID,
        *,
        target: tuple[str | None, str | None] | None = None,
        queue_duration_seconds: float | None = None,
    ) -> Task | None:
        with self._session() as session:
            task = self._checkpoint(
                session,
                task_id,
                "queued",
                "running",
                started_at=datetime.now(UTC),
                queue_duration_seconds=queue_duration_seconds,
                **(
                    {"provider": target[0], "model": target[1]}
                    if target is not None
                    else {}
                ),
            )
            session.commit()
            return task

    def cancel(
        self, task_id: UUID, *, queue_duration_seconds: float | None = None
    ) -> Task:
        now = datetime.now(UTC)
        with self._session() as session:
            task = self._checkpoint(
                session,
                task_id,
                "queued",
                "cancelled",
                finished_at=now,
                reason="cancelled",
                cancellation_requested_at=now,
                queue_duration_seconds=queue_duration_seconds,
            )
            if task is not None:
                record = session.get(TaskRecord, task_id)
                record.telemetry_status = record_checkpoint(
                    session,
                    task,
                    observations=ExecutionObservations(),
                    execution_duration_seconds=0.0,
                )
                task = _task(record)
            if task is None:
                # Running cancellation is a durable request, not premature terminality.
                record = session.scalar(
                    update(TaskRecord)
                    .where(
                        TaskRecord.task_id == task_id,
                        TaskRecord.state == "running",
                        TaskRecord.cancellation_requested_at.is_(None),
                    )
                    .values(cancellation_requested_at=now, updated_at=now)
                    .returning(TaskRecord),
                    execution_options={"synchronize_session": False},
                )
                task = _task(record) if record else None
            if task is None:
                record = session.get(TaskRecord, task_id)
                if record is None:
                    raise TaskNotFound("Task ID is not registered")
                task = _task(record)
            session.commit()
            return task

    def finish(
        self,
        task_id: UUID,
        *,
        result: ExecutionResult | None = None,
        error_code: TaskReason | None = None,
        observations: ExecutionObservations | None = None,
        execution_duration_seconds: float | None = None,
    ) -> Task:
        """Committed running cancellation beats completion; terminal rows stay put."""
        state = result.state if result is not None else "failed"
        reason = result.reason if result is not None else error_code
        if reason is None:
            raise TaskValidationError("Task outcome is required")
        validate_transition("running", state)
        now = datetime.now(UTC)
        with self._session() as session:
            for cancellation in (False, True):
                target = "cancelled" if cancellation else state
                validate_transition("running", target)
                outcome = (
                    cancelled_result(result) if cancellation and result else result
                )
                record = session.scalar(
                    update(TaskRecord)
                    .where(
                        TaskRecord.task_id == task_id,
                        TaskRecord.state == "running",
                        TaskRecord.cancellation_requested_at.is_not(None)
                        if cancellation
                        else TaskRecord.cancellation_requested_at.is_(None),
                    )
                    .values(
                        state=target,
                        reason="cancelled" if cancellation else reason,
                        error_code=reason if target == "failed" else None,
                        execution_result=outcome.model_dump(mode="json")
                        if outcome
                        else None,
                        finished_at=now,
                        updated_at=now,
                    )
                    .returning(TaskRecord),
                    execution_options={"synchronize_session": False},
                )
                if record is not None:
                    task = _task(record)
                    record.telemetry_status = record_checkpoint(
                        session,
                        task,
                        result=outcome,
                        observations=observations,
                        execution_duration_seconds=execution_duration_seconds,
                    )
                    task = _task(record)
                    session.commit()
                    return task
            record = session.get(TaskRecord, task_id)
            if record is None:
                raise TaskNotFound("Task ID is not registered")
            return _task(record)

    def recover_running(self) -> int:
        """Exclusive startup only: started work is never replayed after process loss."""
        validate_transition("running", "failed")
        now = datetime.now(UTC)
        with self._session() as session:
            records = session.scalars(
                update(TaskRecord)
                .where(TaskRecord.state == "running")
                .values(
                    state="failed",
                    reason="execution_interrupted",
                    error_code="execution_interrupted",
                    finished_at=now,
                    updated_at=now,
                )
                .returning(TaskRecord),
                execution_options={"synchronize_session": False},
            )
            changed = 0
            for record in records.all():
                record.telemetry_status = record_checkpoint(session, _task(record))
                changed += 1
            session.commit()
            return changed
