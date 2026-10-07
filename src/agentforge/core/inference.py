"""Small normalized inference boundary used by runtime and direct callers."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictStr, model_validator

from agentforge.core.worker import Worker, WorkerHealth


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    description: str
    parameters: dict[str, JsonValue]


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, JsonValue]


class TokenUsage(BaseModel):
    """Observed counts only; absence remains unknown, never estimated."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class GenerationTiming(BaseModel):
    """Backend-observed durations in seconds; output excludes prompt evaluation.

    Request/network latency is measured separately by the runtime. None means
    unobserved. These fields never imply client-observed time to first token.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )

    total_seconds: float | None = Field(default=None, ge=0)
    load_seconds: float | None = Field(default=None, ge=0)
    prompt_seconds: float | None = Field(default=None, ge=0)
    output_seconds: float | None = Field(default=None, ge=0)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    # Opaque assistant history for protocol continuity; never a runtime instruction.
    reasoning: StrictStr | None = Field(default=None, repr=False)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None

    @model_validator(mode="after")
    def valid_tool_role(self):
        if self.reasoning is not None and self.role != "assistant":
            raise ValueError("Only assistant messages may carry reasoning state")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("Only assistant messages may request tools")
        if self.role == "tool":
            if not self.tool_call_id or not self.tool_name:
                raise ValueError("Tool results require call ID and tool name")
        elif self.tool_call_id is not None or self.tool_name is not None:
            raise ValueError("Only tool results may carry correlation fields")
        return self


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    messages: list[Message] = Field(min_length=1)
    system: str | None = None
    temperature: float | None = Field(default=None, ge=0)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    tools: list[ToolDefinition] = Field(default_factory=list)


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    reasoning: StrictStr | None = Field(default=None, repr=False)
    model: str
    finish_reason: str | None = None
    usage: dict[str, JsonValue] | None = None
    timing: dict[str, JsonValue] | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    token_usage: TokenUsage | None = None
    generation_timing: GenerationTiming | None = None


class GenerationChunk(GenerationResult):
    """Content/reasoning are deltas; terminal chunks carry available final metadata."""

    done: bool = False


class Provider(Protocol):
    """Protocol mechanics for an explicitly supplied Worker; no selection policy."""

    name: str

    async def health(self, worker: Worker) -> WorkerHealth: ...

    async def generate(
        self, worker: Worker, request: GenerationRequest
    ) -> GenerationResult: ...

    def stream(
        self, worker: Worker, request: GenerationRequest
    ) -> AbstractAsyncContextManager[AsyncIterator[GenerationChunk]]: ...
