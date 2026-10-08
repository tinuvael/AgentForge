"""Short SQLite checkpoint transactions; no prompts, bodies or inference leases."""

from datetime import UTC

from pydantic import ValidationError
from sqlalchemy import case, delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError

from agentforge.db.models import WorkerDiagnosticRecord
from agentforge.workers.diagnostics import (
    DiagnosticHistory,
    DiagnosticObservation,
    DiagnosticsUnavailable,
)


def utc(value):
    return value.replace(tzinfo=UTC) if value is not None else None


class DiagnosticsRepository:
    def __init__(self, sessions):
        self._sessions = sessions

    def histories(self, worker_id, fingerprint) -> dict[str, DiagnosticHistory]:
        try:
            with self._sessions() as session:
                records = session.scalars(
                    select(WorkerDiagnosticRecord).where(
                        WorkerDiagnosticRecord.worker_id == worker_id
                    )
                )
                return {
                    row.probe_kind: DiagnosticHistory(
                        latest=DiagnosticObservation.model_validate(row.latest),
                        last_success_at=utc(row.last_success_at),
                        last_failure_at=utc(row.last_failure_at),
                    )
                    if row.configuration_fingerprint == fingerprint
                    else DiagnosticHistory(previous_configuration=True)
                    for row in records
                }
        except (SQLAlchemyError, ValidationError):
            raise DiagnosticsUnavailable("Diagnostic storage unavailable") from None

    def save(self, observation, fingerprint, configured_ids):
        row = WorkerDiagnosticRecord
        success = observation.status in {"available", "successful"}
        failure = observation.status == "failed"
        values = {
            "worker_id": observation.configuration.worker_id,
            "probe_kind": observation.probe_kind,
            "configuration_fingerprint": fingerprint,
            "checked_at": observation.checked_at,
            "latest": observation.model_dump(
                mode="json", exclude={"tokens_per_second", "unavailable_metrics"}
            ),
            "last_success_at": observation.checked_at if success else None,
            "last_failure_at": observation.checked_at if failure else None,
        }
        statement = insert(row).values(**values)
        same = row.configuration_fingerprint == fingerprint
        updates = dict(values)
        for field, occurred in (
            ("last_success_at", success),
            ("last_failure_at", failure),
        ):
            if not occurred:
                updates[field] = case((same, getattr(row, field)), else_=None)
        statement = statement.on_conflict_do_update(
            index_elements=[row.worker_id, row.probe_kind],
            set_=updates,
            where=row.checked_at <= observation.checked_at,
        )
        try:
            with self._sessions.begin() as session:
                # Bound retained identities to this operator's full configuration.
                session.execute(delete(row).where(row.worker_id.not_in(configured_ids)))
                session.execute(statement)
        except SQLAlchemyError:
            raise DiagnosticsUnavailable("Diagnostic storage unavailable") from None
