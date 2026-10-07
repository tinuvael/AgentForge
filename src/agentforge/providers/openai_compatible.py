"""Bounded Chat Completions HTTP/SSE adapter, independent of any vendor product."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    StrictStr,
    ValidationError,
)

from agentforge.core.inference import (
    GenerationChunk,
    GenerationRequest,
    GenerationResult,
    Message,
    TokenUsage,
    ToolCall,
)
from agentforge.core.provider_errors import (
    BackendUnavailable,
    InvalidProviderResponse,
    ProviderRejected,
    ProviderTimeout,
)
from agentforge.core.worker import Worker, WorkerHealth
from agentforge.providers.json import parse_json as _json
from agentforge.workers.config import ProviderConnection

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_STREAM_BYTES = 4 * 1024 * 1024
MAX_TOOL_CALLS = 200
_OPTIONS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "seed",
    "stop",
    "presence_penalty",
    "frequency_penalty",
}


class _WireModel(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)


class _Usage(_WireModel):
    prompt_tokens: StrictInt | None = Field(default=None, ge=0)
    completion_tokens: StrictInt | None = Field(default=None, ge=0)
    total_tokens: StrictInt | None = Field(default=None, ge=0)


class _Function(_WireModel):
    name: StrictStr = Field(min_length=1, max_length=100)
    arguments: StrictStr


class _Call(_WireModel):
    id: StrictStr = Field(min_length=1, max_length=200)
    type: Literal["function"]
    function: _Function


class _Message(_WireModel):
    role: Literal["assistant"]
    content: StrictStr | None = None
    reasoning_content: StrictStr | None = Field(default=None, repr=False)
    reasoning: StrictStr | None = Field(default=None, repr=False)
    tool_calls: list[_Call] = Field(default_factory=list, max_length=MAX_TOOL_CALLS)
    function_call: object | None = Field(default=None, repr=False, exclude=True)


class _Choice(_WireModel):
    index: StrictInt = Field(default=0, ge=0, le=0)
    message: _Message
    finish_reason: StrictStr = Field(min_length=1)


class _Response(_WireModel):
    model: StrictStr | None = None
    choices: list[_Choice] = Field(min_length=1, max_length=1)
    usage: _Usage | None = None


def _invalid() -> InvalidProviderResponse:
    return InvalidProviderResponse("Invalid compatible Provider response")


def _usage(value: _Usage | None) -> dict:
    if value is None:
        return {}
    observed = value.model_dump(exclude_none=True)
    return {
        "usage": observed or None,
        "token_usage": TokenUsage(
            input_tokens=value.prompt_tokens,
            output_tokens=value.completion_tokens,
            total_tokens=value.total_tokens,
        )
        if observed
        else None,
    }


def _calls(values: list[_Call]) -> list[ToolCall]:
    calls = []
    for value in values:
        arguments = _json(value.function.arguments)
        if not isinstance(arguments, dict):
            raise _invalid()
        calls.append(
            ToolCall(id=value.id, name=value.function.name, arguments=arguments)
        )
    if len({call.id for call in calls}) != len(calls):
        raise _invalid()
    return calls


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        connection: ProviderConnection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if connection.type != self.name:
            raise ProviderRejected("Mismatched Provider connection")
        self._connection = connection
        token = connection.bearer_token()
        self._token = SecretStr(token) if token else None
        self._transport = transport
        # Lazy creation leaves no HTTP resources to leak on composition failure.
        self._http: httpx.AsyncClient | None = None
        self._closed = False
        self._active: set[asyncio.Task] = set()

    def _client(self) -> httpx.AsyncClient:
        if self._closed:
            raise ProviderRejected("Provider connection is closed")
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=str(self._connection.base_url).rstrip("/") + "/",
                transport=self._transport,
                follow_redirects=False,
            )
        return self._http

    async def aclose(self):
        self._closed = True
        active = self._active - {asyncio.current_task()}
        for task in active:
            task.cancel()
        try:
            await asyncio.gather(*active, return_exceptions=True)
        finally:
            if self._http is not None:
                await self._http.aclose()

    @asynccontextmanager
    async def _response(self, worker, request, *, streaming):
        self._validate_worker(worker)
        timeout = request.timeout_seconds or worker.timeout_seconds
        client = self._client()
        headers = {"Accept-Encoding": "identity"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token.get_secret_value()
        task = asyncio.current_task()
        self._active.add(task)
        try:
            async with asyncio.timeout(timeout):
                async with client.stream(
                    "POST",
                    "chat/completions",
                    json=self._payload(worker, request, streaming),
                    headers=headers,
                    timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                    follow_redirects=False,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        # Never read error bodies, including structured backend errors.
                        if response.status_code >= 500:
                            raise BackendUnavailable("Provider backend unavailable")
                        raise ProviderRejected(
                            "Provider rejected the request",
                            status_code=response.status_code,
                        )
                    if (
                        response.headers.get("content-encoding", "identity")
                        != "identity"
                    ):
                        raise _invalid()
                    yield response
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderTimeout("Provider request timed out") from None
        except httpx.RequestError:
            raise BackendUnavailable("Could not communicate with Provider") from None
        finally:
            self._active.discard(task)

    def _payload(self, worker: Worker, request: GenerationRequest, streaming: bool):
        options = {**worker.options, **request.options}
        if set(options) - _OPTIONS:
            raise ProviderRejected("Unsupported generation options")
        messages = request.messages
        if request.system is not None:
            messages = [Message(role="system", content=request.system), *messages]
        payload = {
            **options,
            "model": worker.model,
            "messages": [self._message(message) for message in messages],
            "stream": streaming,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {"type": "function", "function": tool.model_dump()}
                for tool in request.tools
            ]
        if streaming and self._connection.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        return payload

    @staticmethod
    def _message(message: Message):
        value = {"role": message.role, "content": message.content}
        # Private reasoning remains internal; no standardized continuation field.
        if message.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, ensure_ascii=False, allow_nan=False
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        if message.role == "tool":
            value["tool_call_id"] = message.tool_call_id
        return value

    async def health(self, worker: Worker) -> WorkerHealth:
        # No broadly guaranteed non-inference probe in this implemented subset.
        self._validate_worker(worker)
        return WorkerHealth(backend_available=False, error_code="not_probed")

    def _validate_worker(self, worker: Worker):
        if (
            worker.provider != self.name
            or worker.provider_connection != self._connection.id
        ):
            raise ProviderRejected("Mismatched Worker/Provider connection")

    async def generate(
        self, worker: Worker, request: GenerationRequest
    ) -> GenerationResult:
        async with self._response(worker, request, streaming=False) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=4096):
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise _invalid()
                body.extend(chunk)
            data = _json(bytes(body))
            if isinstance(data, dict) and "error" in data:
                raise ProviderRejected("Provider reported a generation error")
            try:
                parsed = _Response.model_validate(data)
                choice = parsed.choices[0]
                if choice.message.function_call is not None:
                    raise _invalid()
                calls = _calls(choice.message.tool_calls)
                if choice.message.content is None and not calls:
                    raise _invalid()
                if choice.finish_reason == "tool_calls" and not calls:
                    raise _invalid()
                return GenerationResult(
                    content=choice.message.content or "",
                    reasoning=choice.message.reasoning_content
                    or choice.message.reasoning,
                    model=parsed.model or worker.model,
                    finish_reason=choice.finish_reason,
                    tool_calls=calls,
                    **_usage(parsed.usage),
                )
            except ValidationError:
                raise _invalid() from None

    async def _events(self, response: httpx.Response) -> AsyncIterator[str]:
        """SSE data events: bounded byte lines/frames and whole response budget."""
        buffer = bytearray()
        data: list[bytes] = []
        frame_size = total = 0
        # No chunk_size: httpx otherwise buffers small deltas until that size.
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_STREAM_BYTES:
                raise _invalid()
            # Bound parser work windows even if a transport supplies a large chunk.
            for offset in range(0, len(chunk), 4096):
                buffer.extend(chunk[offset : offset + 4096])
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    frame_size += len(line) + 1
                    if frame_size > MAX_EVENT_BYTES:
                        raise _invalid()
                    line = line.rstrip(b"\r")
                    if not line:
                        if data:
                            try:
                                event = b"\n".join(data).decode("utf-8")
                            except UnicodeError:
                                raise _invalid() from None
                            yield event
                        data = []
                        frame_size = 0
                    elif line.startswith(b"data:"):
                        value = line[5:]
                        data.append(value[1:] if value.startswith(b" ") else value)
                    # Ignored SSE fields/comments still count toward byte bounds.
                if frame_size + len(buffer) > MAX_EVENT_BYTES:
                    raise _invalid()
                # Keep large keepalive-only chunks interruptible, without a reader task.
                await asyncio.sleep(0)
        # EOF is not a terminal event. Caller requires a framed [DONE].
        if buffer or data:
            raise _invalid()

    async def _chunks(self, response, worker) -> AsyncIterator[GenerationChunk]:
        assembled: dict[int, dict] = {}
        finish = None
        usage = None
        model = worker.model
        events = self._events(response)
        try:
            async for event in events:
                if event.strip() == "[DONE]":
                    if finish is None:
                        raise _invalid()
                    try:
                        if sorted(assembled) != list(range(len(assembled))):
                            raise _invalid()
                        calls = _calls(
                            [
                                _Call.model_validate(assembled[i])
                                for i in sorted(assembled)
                            ]
                        )
                        if finish == "tool_calls" and not calls:
                            raise _invalid()
                    except ValidationError:
                        raise _invalid() from None
                    yield GenerationChunk(
                        content="",
                        model=model,
                        done=True,
                        finish_reason=finish,
                        tool_calls=calls,
                        **_usage(usage),
                    )
                    return
                data = _json(event)
                if not isinstance(data, dict):
                    raise _invalid()
                if "error" in data:
                    raise ProviderRejected("Provider reported a generation error")
                try:
                    if data.get("usage") is not None:
                        usage = _Usage.model_validate(data["usage"])
                    if data.get("model") is not None:
                        if not isinstance(data["model"], str) or not data["model"]:
                            raise _invalid()
                        model = data["model"]
                    choices = data["choices"]
                    if not isinstance(choices, list) or len(choices) > 1:
                        raise _invalid()
                    if not choices:
                        if data.get("usage") is None:
                            raise _invalid()
                        continue
                    choice = choices[0]
                    if (
                        not isinstance(choice, dict)
                        or type(choice.get("index")) is not int
                        or choice["index"] != 0
                    ):
                        raise _invalid()
                    delta = choice["delta"]
                    if not isinstance(delta, dict) or finish is not None:
                        raise _invalid()
                    if "function_call" in delta:
                        raise _invalid()
                    if delta.get("role", "assistant") != "assistant":
                        raise _invalid()
                    content = delta.get("content")
                    reasoning = delta.get("reasoning_content", delta.get("reasoning"))
                    if any(
                        value is not None and not isinstance(value, str)
                        for value in (content, reasoning)
                    ):
                        raise _invalid()
                    calls = delta.get("tool_calls")
                    if calls is None:
                        calls = []
                    if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS:
                        raise _invalid()
                    for call in calls:
                        self._assemble(assembled, call)
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if not isinstance(reason, str) or not reason:
                            raise _invalid()
                        finish = reason
                    if content or reasoning:
                        yield GenerationChunk(
                            content=content or "", reasoning=reasoning, model=model
                        )
                except (ValidationError, KeyError, TypeError):
                    raise _invalid() from None
            raise _invalid()
        finally:
            await events.aclose()

    @staticmethod
    def _assemble(assembled, call):
        if (
            not isinstance(call, dict)
            or type(call.get("index")) is not int
            or not 0 <= call["index"] < MAX_TOOL_CALLS
        ):
            raise _invalid()
        target = assembled.setdefault(
            call["index"],
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if call.get("type", "function") != "function":
            raise _invalid()
        function = call.get("function", {})
        if not isinstance(function, dict):
            raise _invalid()
        for owner, key, fragment, limit in (
            (target, "id", call.get("id"), 200),
            (target["function"], "name", function.get("name"), 100),
            (
                target["function"],
                "arguments",
                function.get("arguments"),
                MAX_RESPONSE_BYTES,
            ),
        ):
            if fragment is not None:
                if (
                    not isinstance(fragment, str)
                    or len(owner[key]) + len(fragment) > limit
                ):
                    raise _invalid()
                owner[key] += fragment

    @asynccontextmanager
    async def stream(self, worker: Worker, request: GenerationRequest):
        if not worker.supports_streaming:
            raise ProviderRejected("Worker does not advertise streaming support")
        async with self._response(worker, request, streaming=True) as response:
            chunks = self._chunks(response, worker)
            try:
                yield chunks
            finally:
                await chunks.aclose()
