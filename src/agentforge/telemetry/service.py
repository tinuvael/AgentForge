"""Observation queries only; no selection policy, scoring or routing advice."""

from uuid import UUID

from pydantic import ValidationError

from agentforge.db.telemetry import TelemetryRepository
from agentforge.telemetry.models import (
    ComparisonGroup,
    TaskTelemetry,
    TelemetryComparison,
    TelemetryFilter,
    TelemetryValidationError,
)


class TelemetryService:
    def __init__(self, repository: TelemetryRepository):
        self._repository = repository

    @staticmethod
    def _bounds(limit: int, offset: int):
        if (
            type(limit) is not int
            or not 1 <= limit <= 1000
            or type(offset) is not int
            or offset < 0
        ):
            raise TelemetryValidationError("Invalid telemetry query bounds")

    @staticmethod
    def _filters(values) -> TelemetryFilter:
        try:
            return TelemetryFilter.model_validate(values)
        except ValidationError:
            raise TelemetryValidationError("Invalid telemetry filters") from None

    def get_for_task(self, task_id: UUID | str) -> TaskTelemetry:
        try:
            identity = task_id if isinstance(task_id, UUID) else UUID(task_id)
        except (ValueError, TypeError, AttributeError):
            raise TelemetryValidationError("Invalid Task ID") from None
        return self._repository.get_for_task(identity)

    def list_telemetry(
        self, *, limit: int = 100, offset: int = 0, **filters
    ) -> list[TaskTelemetry]:
        self._bounds(limit, offset)
        return self._repository.list(self._filters(filters), limit=limit, offset=offset)

    def compare(
        self, *, group_by: ComparisonGroup, limit: int = 100, offset: int = 0, **filters
    ) -> list[TelemetryComparison]:
        self._bounds(limit, offset)
        if group_by not in {"worker_id", "model", "provider"}:
            raise TelemetryValidationError("Invalid telemetry comparison group")
        return self._repository.compare(
            group_by, self._filters(filters), limit=limit, offset=offset
        )
