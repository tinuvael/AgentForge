"""Ollama HTTP chat adapter with bounded execution and owned stream lifetimes."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
)

from agentforge.core.inference import (
    GenerationChunk,
    GenerationRequest,
    GenerationResult,
    GenerationTiming,
    Message,
    TokenUsage,
    ToolCall,
)
from agentforge.core.provider_errors import (
    BackendUnavailable,
    InvalidProviderResponse,
    ProviderError,
    ProviderRejected,
    ProviderTimeout,
)
from agentforge.core.worker import Worker, WorkerHealth
from agentforge.workers.config import ProviderConnection


class _OllamaFunction(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, JsonValue]


class _OllamaToolCall(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=200)
    function: _OllamaFunction


class _OllamaMessage(BaseModel):
    content: str
    thinking: StrictStr | None = Field(default=None, repr=False)
    tool_calls: list[_OllamaToolCall] = Field(default_factory=list)


class _ChatResponse(BaseModel):
    model: str = Field(min_length=1)
    message: _OllamaMessage
    done: StrictBool
    done_reason: str | None = None
    prompt_eval_count: StrictInt | None = Field(default=None, ge=0)
    eval_count: StrictInt | None = Field(default=None, ge=0)
    total_duration: StrictInt | None = Field(default=None, ge=0)
    load_duration: StrictInt | None = Field(default=None, ge=0)
    prompt_eval_duration: StrictInt | None = Field(default=None, ge=0)
    eval_duration: StrictInt | None = Field(default=None, ge=0)


class _ModelTag(BaseModel):
    name: str = Field(min_length=1)


class _TagsResponse(BaseModel):
    models: list[_ModelTag]


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        connection: ProviderConnection | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Optional transport supports offline tests, without sharing client ownership.
        self._transport = transport
        self._connection = connection
        if connection is not None and connection.type != self.name:
            raise ProviderRejected("Mismatched Provider connection")

    def _validate_worker(self, worker: Worker) -> None:
        if worker.provider != self.name:
            raise ProviderRejected("Worker provider does not match Ollama")
        if self._connection is not None:
            if worker.provider_connection != self._connection.id:
                raise ProviderRejected("Mismatched Worker/Provider connection")
        elif worker.endpoint is None or worker.provider_connection is not None:
            raise ProviderRejected("Worker requires a Provider connection")

    def _client(self, worker: Worker, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=str(
                self._connection.base_url if self._connection else worker.endpoint
            ).rstrip("/")
            + "/",
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            transport=self._transport,
        )

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        if response.status_code in {502, 503, 504}:
            raise BackendUnavailable("Ollama backend unavailable")
        raise ProviderRejected(
            "Ollama rejected the request", status_code=response.status_code
        )

    @staticmethod
    def _payload(
        worker: Worker, request: GenerationRequest, *, stream: bool
    ) -> dict[str, JsonValue]:
        messages = request.messages
        if request.system is not None:
            messages = [Message(role="system", content=request.system), *messages]
        options = {**worker.options, **request.options}
        if worker.context_window is not None:
            options["num_ctx"] = worker.context_window
        if request.temperature is not None:
            options["temperature"] = request.temperature
        payload = {
            "model": worker.model,
            "messages": [OllamaProvider._message(message) for message in messages],
            "stream": stream,
            "options": options,
        }
        if request.tools:
            payload["tools"] = [
                {"type": "function", "function": tool.model_dump()}
                for tool in request.tools
            ]
        return payload

    @staticmethod
    def _message(message: Message) -> dict[str, JsonValue]:
        value = {"role": message.role, "content": message.content}
        if message.reasoning is not None:
            value["thinking"] = message.reasoning
        if message.tool_calls:
            value["tool_calls"] = [
                {"function": {"name": call.name, "arguments": call.arguments}}
                for call in message.tool_calls
            ]
        if message.role == "tool":
            # Native Ollama correlates by tool_name, not OpenAI tool_call_id.
            value["tool_name"] = message.tool_name
        return value

    @staticmethod
    def _parse_chat(data: object) -> GenerationChunk:
        if isinstance(data, dict) and "error" in data:
            raise ProviderRejected("Ollama reported a generation error")
        try:
            parsed = _ChatResponse.model_validate(data)
        except ValidationError:
            raise InvalidProviderResponse("Invalid Ollama chat response") from None
        usage = {
            key: value
            for key in ("prompt_eval_count", "eval_count")
            if (value := getattr(parsed, key)) is not None
        }
        timing = {
            key: value
            for key in (
                "total_duration",
                "load_duration",
                "prompt_eval_duration",
                "eval_duration",
            )
            if (value := getattr(parsed, key)) is not None
        }
        return GenerationChunk(
            content=parsed.message.content,
            reasoning=parsed.message.thinking,
            model=parsed.model,
            done=parsed.done,
            finish_reason=parsed.done_reason,
            usage=usage or None,
            timing=timing or None,
            generation_timing=GenerationTiming(
                **{
                    normalized: getattr(parsed, raw) / 1_000_000_000
                    for normalized, raw in (
                        ("total_seconds", "total_duration"),
                        ("load_seconds", "load_duration"),
                        ("prompt_seconds", "prompt_eval_duration"),
                        ("output_seconds", "eval_duration"),
                    )
                    if getattr(parsed, raw) is not None
                }
            )
            if timing
            else None,
            token_usage=TokenUsage(
                input_tokens=parsed.prompt_eval_count, output_tokens=parsed.eval_count
            )
            if usage
            else None,
            tool_calls=[
                ToolCall(
                    id=call.id or f"ollama-{uuid4().hex}",
                    name=call.function.name,
                    arguments=call.function.arguments,
                )
                for call in parsed.message.tool_calls
            ],
        )

    @staticmethod
    def _json(data: str | bytes) -> object:
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise InvalidProviderResponse("Invalid Ollama JSON response") from None

    async def health(self, worker: Worker) -> WorkerHealth:
        """Probe the backend and whether the configured model is installed."""
        self._validate_worker(worker)
        timeout = min(5.0, worker.timeout_seconds)
        try:
            async with (
                asyncio.timeout(timeout),
                self._client(worker, timeout) as client,
            ):
                response = await client.get("api/tags")
                self._check_status(response)
                try:
                    tags = _TagsResponse.model_validate(self._json(response.content))
                except ValidationError:
                    raise InvalidProviderResponse("Invalid Ollama model list") from None
                model = (
                    worker.model
                    if ":" in worker.model.rsplit("/", 1)[-1]
                    else worker.model + ":latest"
                )
                return WorkerHealth(
                    backend_available=True,
                    model_available=any(
                        tag.name in {worker.model, model} for tag in tags.models
                    ),
                )
        except (TimeoutError, httpx.TimeoutException):
            return WorkerHealth(
                backend_available=False, error_code=ProviderTimeout.code
            )
        except httpx.RequestError:
            return WorkerHealth(
                backend_available=False, error_code=BackendUnavailable.code
            )
        except ProviderError as error:
            return WorkerHealth(backend_available=False, error_code=error.code)

    async def generate(
        self, worker: Worker, request: GenerationRequest
    ) -> GenerationResult:
        self._validate_worker(worker)
        timeout = request.timeout_seconds or worker.timeout_seconds
        try:
            async with (
                asyncio.timeout(timeout),
                self._client(worker, timeout) as client,
            ):
                response = await client.post(
                    "api/chat", json=self._payload(worker, request, stream=False)
                )
                self._check_status(response)
                chunk = self._parse_chat(self._json(response.content))
                if not chunk.done:
                    raise InvalidProviderResponse("Incomplete Ollama chat response")
                return GenerationResult(**chunk.model_dump(exclude={"done"}))
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderTimeout("Ollama generation timed out") from None
        except httpx.RequestError:
            raise BackendUnavailable("Could not communicate with Ollama") from None

    async def _chunks(self, response: httpx.Response) -> AsyncIterator[GenerationChunk]:
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            chunk = self._parse_chat(self._json(line))
            yield chunk
            if chunk.done:
                return
        raise InvalidProviderResponse("Ollama stream ended without a terminal chunk")

    @asynccontextmanager
    async def stream(
        self, worker: Worker, request: GenerationRequest
    ) -> AsyncIterator[AsyncIterator[GenerationChunk]]:
        """Use `async with`; exiting closes HTTP even after an early iteration stop."""
        self._validate_worker(worker)
        if not worker.supports_streaming:
            raise ProviderRejected("Worker does not advertise streaming support")
        timeout = request.timeout_seconds or worker.timeout_seconds
        try:
            async with (
                asyncio.timeout(timeout),
                self._client(worker, timeout) as client,
            ):
                async with client.stream(
                    "POST", "api/chat", json=self._payload(worker, request, stream=True)
                ) as response:
                    self._check_status(response)
                    chunks = self._chunks(response)
                    try:
                        yield chunks
                    finally:
                        await chunks.aclose()
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderTimeout("Ollama stream timed out") from None
        except httpx.RequestError:
            raise BackendUnavailable("Could not communicate with Ollama") from None
