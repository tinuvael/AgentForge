"""Small normalized inference boundary used by future execution callers."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentforge.core.worker import Worker, WorkerHealth


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    messages: list[Message] = Field(min_length=1)
    system: str | None = None
    temperature: float | None = Field(default=None, ge=0)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    model: str
    finish_reason: str | None = None
    usage: dict[str, JsonValue] | None = None
    timing: dict[str, JsonValue] | None = None


class GenerationChunk(GenerationResult):
    """Content is a delta; terminal chunks carry available final metadata."""

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
