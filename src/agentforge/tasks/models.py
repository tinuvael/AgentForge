"""Immutable durable Task snapshots and the centralized lifecycle contract."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agentforge.agents.models import ExecutionResult, TerminationReason

TaskState = Literal["queued", "running", "completed", "failed", "cancelled"]
TaskReason = (
    TerminationReason
    | Literal[
        "execution_interrupted",
        "runtime_error",
        "invalid_runtime_result",
        "executor_cancelled",
    ]
)
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
_TRANSITIONS = {
    "queued": frozenset({"running", "cancelled"}),
    "running": TERMINAL_STATES,
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class TaskError(Exception):
    """Public errors contain fixed safe diagnostics only."""


class TaskNotFound(TaskError):
    pass


class TaskValidationError(TaskError):
    pass


class TaskStorageError(TaskError):
    pass


class InvalidTaskTransition(TaskError):
    pass


def validate_transition(source: TaskState, target: TaskState) -> None:
    if target not in _TRANSITIONS.get(source, ()):
        raise InvalidTaskTransition("Task transition is not permitted")


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    project_id: UUID
    agent_id: str
    worker_id: str
    request: str
    state: TaskState
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancellation_requested_at: datetime | None = None
    reason: TaskReason | None = None
    error_code: TaskReason | None = None
    execution_result: ExecutionResult | None = None
    provider: str | None = None
    model: str | None = None
    telemetry_status: Literal["pending", "recorded", "unavailable"] = "pending"
    queue_duration_seconds: float | None = None

    @property
    def final_answer(self) -> str | None:
        return (
            self.execution_result.final_answer
            if self.state == "completed" and self.execution_result is not None
            else None
        )

    @property
    def failure_diagnostic(self) -> str | None:
        return f"Execution failed: {self.error_code}" if self.error_code else None


class TaskHistoryItem(BaseModel):
    """Compact history query: no requests, answers or execution JSON loaded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    project_id: UUID
    project_name: str | None
    agent_id: str
    worker_id: str
    state: TaskState
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: TaskReason | None
    execution_duration_seconds: float | None
