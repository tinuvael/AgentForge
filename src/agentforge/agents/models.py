"""Agent behavior, execution policy and ephemeral results; no Worker routing."""

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentforge.core.inference import GenerationTiming, TokenUsage


class RuntimeLimits(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )

    max_steps: int = Field(default=12, ge=1, le=100)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    max_tool_calls: int = Field(default=24, ge=1, le=200)
    max_tool_result_bytes: int = Field(default=12_000, ge=256, le=65_536)
    max_tool_output_bytes: int = Field(default=48_000, ge=256, le=524_288)
    max_context_tokens: int = Field(default=24_000, ge=1, le=200_000)


class Agent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    description: str
    system_prompt: str = Field(min_length=1)
    allowed_tools: tuple[str, ...] = ()
    limits: RuntimeLimits = Field(default_factory=RuntimeLimits)


class CancellationToken:
    """Thread-safe cooperative signal for future Task cancellation integration.

    Checked at execution boundaries. asyncio task cancellation still propagates.
    """

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


TerminationReason = Literal[
    "completed",
    "cancelled",
    "timeout",
    "provider_timeout",
    "provider_error",
    "invalid_configuration",
    "invalid_response",
    "security_error",
    "tool_not_allowed",
    "tool_error",
    "max_steps",
    "max_tool_calls",
    "tool_result_limit",
    "tool_output_limit",
    "context_limit",
]


class TraceEvent(BaseModel):
    """Bounded metadata only: no source bodies, raw model arguments or diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int
    kind: Literal[
        "model_request", "model_response", "tool_request", "tool_result", "termination"
    ]
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, JsonValue] | None = None
    success: bool | None = None
    error_code: str | None = None
    size_bytes: int | None = None
    duration_seconds: float | None = None
    reason: TerminationReason | None = None


class ModelTurnObservation(BaseModel):
    """One attempted generate call, including failed or locally cancelled calls."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    request_duration_seconds: float = Field(ge=0)
    token_usage: TokenUsage | None = None
    generation_timing: GenerationTiming | None = None


@dataclass
class ExecutionObservations:
    """Ephemeral metadata checkpoint shared with the owning executor.

    Allows executor cancellation to retain observations when run propagates
    CancelledError. No prompts, responses, tools' bodies or Provider objects.
    Trace is the runtime's existing bounded evidence, not a second tool timer.
    """

    model_turns: list[ModelTurnObservation] = field(default_factory=list)
    trace: list[TraceEvent] = field(default_factory=list)
    tool_output_bytes: int = 0
    on_trace: Callable[[TraceEvent], None] | None = field(default=None, repr=False)

    def record_trace(self, event: TraceEvent) -> None:
        self.trace.append(event)
        if self.on_trace is not None:
            try:
                self.on_trace(event)
            except Exception:
                # Observation must never alter execution or its durable result.
                pass


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    final_answer: str | None = None
    agent_id: str
    worker_id: str
    project_id: UUID | str
    state: Literal["completed", "cancelled", "failed"]
    reason: TerminationReason
    steps: int
    tool_call_count: int
    tool_output_bytes: int
    # One observation per successful model turn; missing usage remains None.
    usage: tuple[TokenUsage | None, ...]
    trace: tuple[TraceEvent, ...]
    model_turns: tuple[ModelTurnObservation, ...] = ()
