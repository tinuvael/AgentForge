"""Typed operator observations. Configuration is never a measured capability."""

from datetime import UTC, datetime
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from agentforge.core.inference import GenerationTiming, TokenUsage

GenerationProbeKind = Literal["generation", "tools", "streaming"]
ProbeKind = Literal["health", "generation", "tools", "streaming"]
FailureCode = Literal[
    "backend_unavailable",
    "model_unavailable",
    "timeout",
    "invalid_response",
    "rejected",
    "tool_call_failed",
    "stream_failed",
    "diagnostic_failed",
]
EndpointClass = Literal["local", "private", "remote", "unknown"]


class DiagnosticsError(Exception):
    """Fixed safe service errors, without underlying exception text."""


class DiagnosticsUnavailable(DiagnosticsError):
    pass


class DiagnosticBusy(DiagnosticsError):
    pass


class DiagnosticWorkerNotFound(DiagnosticsError):
    pass


class DiagnosticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class DiagnosticConfiguration(DiagnosticModel):
    worker_id: str
    provider: str
    model: str
    deployment_label: str | None
    endpoint_class: EndpointClass
    context_window: int | None
    supports_tools: bool | None
    supports_streaming: bool


class DiagnosticObservation(DiagnosticModel):
    configuration: DiagnosticConfiguration
    probe_kind: ProbeKind
    probe_version: Literal[1] = 1
    # Completion time in UTC, including failed/skipped explicit operations.
    checked_at: datetime
    status: Literal["available", "not_probed", "successful", "failed", "not_applicable"]
    backend_available: bool | None = None
    backend_reachable: bool | None = None
    model_available: bool | None = None
    generation_success: bool | None = None
    tool_call_success: bool | None = None
    streaming_success: bool | None = None
    request_duration_seconds: float | None = Field(default=None, ge=0)
    # Request start to first nonempty visible content delta, not a tokenizer event.
    ttft_seconds: float | None = Field(default=None, ge=0)
    token_usage: TokenUsage | None = None
    generation_timing: GenerationTiming | None = None
    error_code: FailureCode | None = None

    @field_validator("checked_at")
    @classmethod
    def aware_time(cls, value):
        if value.utcoffset() is None:
            raise ValueError("Diagnostic timestamp requires timezone")
        return value.astimezone(UTC)

    @computed_field
    @property
    def tokens_per_second(self) -> float | None:
        # Same denominator as Task telemetry: backend output generation only.
        if (
            self.token_usage is not None
            and self.token_usage.output_tokens is not None
            and self.generation_timing is not None
            and self.generation_timing.output_seconds is not None
            and self.generation_timing.output_seconds > 0
        ):
            try:
                throughput = (
                    self.token_usage.output_tokens
                    / self.generation_timing.output_seconds
                )
            except OverflowError:
                return None
            return throughput if isfinite(throughput) else None
        return None

    @computed_field
    @property
    def unavailable_metrics(self) -> tuple[str, ...]:
        values = {
            "request_duration_seconds": self.request_duration_seconds,
            "ttft_seconds": self.ttft_seconds,
            "input_tokens": self.token_usage.input_tokens if self.token_usage else None,
            "output_tokens": self.token_usage.output_tokens
            if self.token_usage
            else None,
            "total_tokens": self.token_usage.total_tokens if self.token_usage else None,
            "generation_duration_seconds": self.generation_timing.output_seconds
            if self.generation_timing
            else None,
            "tokens_per_second": self.tokens_per_second,
        }
        return tuple(name for name, value in values.items() if value is None)


class DiagnosticHistory(DiagnosticModel):
    latest: DiagnosticObservation | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    # Earlier target/configuration observations are suppressed, not current evidence.
    previous_configuration: bool = False


class WorkerDiagnostics(DiagnosticModel):
    configuration: DiagnosticConfiguration
    health: DiagnosticHistory = Field(default_factory=DiagnosticHistory)
    generation: DiagnosticHistory = Field(default_factory=DiagnosticHistory)
    tools: DiagnosticHistory = Field(default_factory=DiagnosticHistory)
    streaming: DiagnosticHistory = Field(default_factory=DiagnosticHistory)


class DiagnosticsPage(DiagnosticModel):
    workers: tuple[WorkerDiagnostics, ...]
    next_offset: int | None
