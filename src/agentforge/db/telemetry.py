"""Short-session telemetry reads, SQL comparisons and isolated checkpoint writes."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agentforge.agents.models import ExecutionObservations, ExecutionResult
from agentforge.db.models import TaskRecord, TaskTelemetryRecord
from agentforge.tasks.models import Task
from agentforge.telemetry.models import (
    ComparisonGroup,
    TaskTelemetry,
    TelemetryComparison,
    TelemetryFilter,
    TelemetryNotFound,
    TelemetryUnavailable,
)
from agentforge.telemetry.recording import summarize


def record_checkpoint(
    session: Session,
    task: Task,
    *,
    result: ExecutionResult | None = None,
    observations: ExecutionObservations | None = None,
    execution_duration_seconds: float | None = None,
) -> str:
    """Task writes precede this savepoint; telemetry failure cannot roll them back.

    The caller persists the returned status in the same terminal transaction.
    Never expose or log exception messages or SQL parameter payloads.
    """
    try:
        with session.begin_nested():
            snapshot = summarize(
                task,
                queue_duration_seconds=task.queue_duration_seconds,
                result=result,
                observations=observations,
                execution_duration_seconds=execution_duration_seconds,
            )
            session.add(TaskTelemetryRecord(**snapshot.model_dump()))
            session.flush()
    except Exception:
        return "unavailable"
    return "recorded"


def _snapshot(record: TaskTelemetryRecord) -> TaskTelemetry:
    values = {name: getattr(record, name) for name in TaskTelemetry.model_fields}
    for name in ("created_at", "started_at", "finished_at"):
        value = values[name]
        if value is not None:
            values[name] = (
                value.replace(tzinfo=UTC)
                if value.tzinfo is None
                else value.astimezone(UTC)
            )
    return TaskTelemetry(**values)


class TelemetryRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self._sessions = sessions

    @contextmanager
    def _session(self):
        try:
            with self._sessions() as session:
                yield session
        except SQLAlchemyError:
            raise TelemetryUnavailable("Could not access telemetry storage") from None

    @staticmethod
    def _filtered(query, filters: TelemetryFilter):
        for name in (
            "project_id",
            "agent_id",
            "worker_id",
            "provider",
            "model",
            "state",
        ):
            value = getattr(filters, name)
            if value is not None:
                query = query.where(getattr(TaskTelemetryRecord, name) == value)
        if filters.created_from is not None:
            query = query.where(
                TaskTelemetryRecord.created_at >= filters.created_from.astimezone(UTC)
            )
        if filters.created_before is not None:
            query = query.where(
                TaskTelemetryRecord.created_at < filters.created_before.astimezone(UTC)
            )
        return query

    def get_for_task(self, task_id: UUID) -> TaskTelemetry:
        with self._session() as session:
            record = session.get(TaskTelemetryRecord, task_id)
            if record is not None:
                return _snapshot(record)
            status = session.scalar(
                select(TaskRecord.telemetry_status).where(TaskRecord.task_id == task_id)
            )
            if status == "unavailable":
                raise TelemetryUnavailable("Telemetry was not recorded for this Task")
            raise TelemetryNotFound("No terminal telemetry for this Task")

    def list(
        self, filters: TelemetryFilter, *, limit: int, offset: int
    ) -> list[TaskTelemetry]:
        query = self._filtered(select(TaskTelemetryRecord), filters).order_by(
            TaskTelemetryRecord.created_at.desc(), TaskTelemetryRecord.task_id.desc()
        )
        with self._session() as session:
            return [
                _snapshot(record)
                for record in session.scalars(query.limit(limit).offset(offset))
            ]

    def compare(
        self,
        group: ComparisonGroup,
        filters: TelemetryFilter,
        *,
        limit: int,
        offset: int,
    ) -> list[TelemetryComparison]:
        row = TaskTelemetryRecord
        key = getattr(row, group)
        completed = func.sum(case((row.state == "completed", 1), else_=0))
        failed = func.sum(case((row.state == "failed", 1), else_=0))
        cancelled = func.sum(case((row.state == "cancelled", 1), else_=0))
        # Only complete compatible Task pairs enter the throughput denominator.
        throughput_count = func.count(row.tokens_per_second)
        outputs = func.sum(
            case((row.tokens_per_second.is_not(None), row.completion_tokens))
        )
        durations = func.sum(
            case((row.tokens_per_second.is_not(None), row.generation_duration_seconds))
        )
        query = (
            self._filtered(
                select(
                    key.label("value"),
                    func.count().label("execution_count"),
                    completed.label("completed_count"),
                    failed.label("failed_count"),
                    cancelled.label("cancelled_count"),
                    func.count(row.execution_duration_seconds).label(
                        "runtime_observation_count"
                    ),
                    func.avg(row.execution_duration_seconds).label(
                        "average_execution_duration_seconds"
                    ),
                    func.sum(row.observed_prompt_tokens).label(
                        "observed_prompt_tokens"
                    ),
                    func.sum(row.observed_completion_tokens).label(
                        "observed_completion_tokens"
                    ),
                    func.sum(
                        case((row.token_usage_complete.is_(True), 1), else_=0)
                    ).label("token_complete_execution_count"),
                    throughput_count.label("throughput_execution_count"),
                    (outputs * 1.0 / func.nullif(durations, 0)).label(
                        "tokens_per_second"
                    ),
                ),
                filters,
            )
            .group_by(key)
            .order_by(key.is_not(None), key)
        )
        with self._session() as session:
            return [
                TelemetryComparison(
                    group=group,
                    success_rate=row["completed_count"] / row["execution_count"],
                    **row,
                )
                for row in session.execute(query.limit(limit).offset(offset)).mappings()
            ]
