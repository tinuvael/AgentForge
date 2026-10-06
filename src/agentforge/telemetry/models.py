"""Metadata-only observations and validated, transport-independent query values."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.tasks.models import TaskReason


class TelemetryError(Exception):
    """Fixed safe diagnostics, never database/backend exception messages."""


class TelemetryNotFound(TelemetryError):
    pass


class TelemetryUnavailable(TelemetryError):
    pass


class TelemetryValidationError(TelemetryError):
    pass


class TaskTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    task_id: UUID
    project_id: UUID
    agent_id: str
    worker_id: str
    provider: str | None = None
    model: str | None = None
    state: Literal["completed", "failed", "cancelled"]
    reason: TaskReason
    error_category: TaskReason | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime
    queue_duration_seconds: float | None = Field(default=None, ge=0)
    execution_duration_seconds: float | None = Field(default=None, ge=0)
    total_duration_seconds: float | None = Field(default=None, ge=0)
    model_call_count: int | None = Field(default=None, ge=0)
    model_request_duration_seconds: float | None = Field(default=None, ge=0)
    backend_total_duration_seconds: float | None = Field(default=None, ge=0)
    model_load_duration_seconds: float | None = Field(default=None, ge=0)
    prompt_evaluation_duration_seconds: float | None = Field(default=None, ge=0)
    generation_duration_seconds: float | None = Field(default=None, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    observed_prompt_tokens: int | None = Field(default=None, ge=0)
    observed_completion_tokens: int | None = Field(default=None, ge=0)
    prompt_observed_turns: int = Field(default=0, ge=0)
    completion_observed_turns: int = Field(default=0, ge=0)
    token_usage_complete: bool = False
    ttft_seconds: float | None = Field(default=None, ge=0)
    tokens_per_second: float | None = Field(default=None, ge=0)
    tool_call_count: int | None = Field(default=None, ge=0)
    total_tool_duration_seconds: float | None = Field(default=None, ge=0)
    tool_output_bytes: int | None = Field(default=None, ge=0)


class TelemetryFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: UUID | None = None
    agent_id: str | None = None
    worker_id: str | None = None
    provider: str | None = None
    model: str | None = None
    state: Literal["completed", "failed", "cancelled"] | None = None
    created_from: datetime | None = None
    created_before: datetime | None = None

    @model_validator(mode="after")
    def valid_times(self):
        for value in (self.created_from, self.created_before):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Time filters require timezone-aware timestamps")
        if (
            self.created_from is not None
            and self.created_before is not None
            and self.created_from >= self.created_before
        ):
            raise ValueError("Invalid telemetry time range")
        return self


ComparisonGroup = Literal["worker_id", "model", "provider"]


class TelemetryComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    group: ComparisonGroup
    value: str | None
    execution_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    success_rate: float
    runtime_observation_count: int
    average_execution_duration_seconds: float | None
    observed_prompt_tokens: int | None
    observed_completion_tokens: int | None
    token_complete_execution_count: int
    throughput_execution_count: int
    tokens_per_second: float | None
