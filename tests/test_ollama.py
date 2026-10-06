"""Exercise HTTP normalization, errors and stream ownership entirely offline."""

import asyncio
import json

import httpx
import pytest

from agentforge.core.inference import GenerationRequest, Message, Provider
from agentforge.core.provider_errors import (
    BackendUnavailable,
    InvalidProviderResponse,
    ProviderRejected,
    ProviderTimeout,
)
from agentforge.core.worker import Worker
from agentforge.providers.ollama import OllamaProvider


@pytest.fixture
def worker():
    return Worker(
        id="test-worker",
        provider="ollama",
        model="test-model:tiny",
        endpoint="http://backend.invalid:1234/proxy/",
        context_window=2048,
        supports_streaming=True,
        options={"temperature": 0.1, "seed": 7},
    )


@pytest.fixture
def generation_request():
    return GenerationRequest(messages=[Message(role="user", content="hello")])


def chat_response(**changes):
    return {
        "model": "test-model:tiny",
        "message": {"role": "assistant", "content": "hello back"},
        "done": True,
        "done_reason": "stop",
        **changes,
    }


def provider_for(handler) -> Provider:
    return OllamaProvider(transport=httpx.MockTransport(handler))


def test_generate_normalizes_response_and_configured_request(
    worker, generation_request
):
    def handler(http_request):
        assert str(http_request.url) == "http://backend.invalid:1234/proxy/api/chat"
        assert http_request.method == "POST"
        assert http_request.extensions["timeout"]["read"] == 120.0
        payload = json.loads(http_request.content)
        assert payload == {
            "model": worker.model,
            "messages": [
                {"role": "system", "content": "be concise"},
                {"role": "user", "content": "hello"},
            ],
            "stream": False,
            "options": {"temperature": 0.4, "num_ctx": 2048, "seed": 9},
        }
        return httpx.Response(
            200,
            json=chat_response(
                prompt_eval_count=10,
                eval_count=4,
                total_duration=12345,
                eval_duration=6789,
            ),
        )

    generation_request = generation_request.model_copy(
        update={"system": "be concise", "temperature": 0.4, "options": {"seed": 9}}
    )
    result = asyncio.run(provider_for(handler).generate(worker, generation_request))
    assert result.content == "hello back"
    assert result.model == worker.model
    assert result.finish_reason == "stop"
    assert result.usage == {"prompt_eval_count": 10, "eval_count": 4}
    assert result.timing == {"total_duration": 12345, "eval_duration": 6789}


def test_one_provider_serves_multiple_explicit_targets(worker, generation_request):
    seen = []

    def handler(http_request):
        payload = json.loads(http_request.content)
        seen.append((http_request.url.host, payload))
        return httpx.Response(200, json=chat_response(model=payload["model"]))

    provider = provider_for(handler)
    other = Worker(
        id="another-worker",
        provider="ollama",
        model="other-model",
        endpoint="http://other.invalid",
    )

    async def run():
        await provider.generate(worker, generation_request)
        await provider.generate(other, generation_request)

    asyncio.run(run())
    assert [host for host, _ in seen] == ["backend.invalid", "other.invalid"]
    assert [payload["model"] for _, payload in seen] == [worker.model, other.model]
    assert seen[0][1]["options"]["temperature"] == 0.1
    assert "num_ctx" not in seen[1][1]["options"]


@pytest.mark.parametrize(
    "model,installed", [("test-model:tiny", True), ("missing", False)]
)
def test_health_distinguishes_backend_and_model_availability(worker, model, installed):
    def handler(http_request):
        assert http_request.url.path == "/proxy/api/tags"
        assert http_request.extensions["timeout"]["read"] == 5.0
        return httpx.Response(200, json={"models": [{"name": "test-model:tiny"}]})

    worker = worker.model_copy(update={"model": model})
    health = asyncio.run(provider_for(handler).health(worker))
    assert health.backend_available
    assert health.model_available is installed
    assert health.available is installed
    assert health.error_code is None


@pytest.mark.parametrize("model", ["small", "registry.invalid:5000/team/small"])
def test_health_accepts_default_latest_tag(worker, model):
    provider = provider_for(
        lambda _: httpx.Response(200, json={"models": [{"name": model + ":latest"}]})
    )
    health = asyncio.run(provider.health(worker.model_copy(update={"model": model})))
    assert health.available


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(503), "backend_unavailable"),
        (httpx.Response(401), "rejected"),
        (httpx.Response(200, json={"models": "wrong"}), "invalid_response"),
        (httpx.Response(200, content=b"bad json"), "invalid_response"),
    ],
)
def test_health_failure(worker, response, code):
    health = asyncio.run(provider_for(lambda _: response).health(worker))
    assert not health.backend_available
    assert health.model_available is None
    assert not health.available
    assert health.error_code == code


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"model": "test", "done": True, "message": {"content": 123}},
        chat_response(done="true"),
        chat_response(done=False),
        chat_response(eval_count=-1),
        chat_response(total_duration="secret"),
    ],
)
def test_malformed_response(worker, generation_request, data):
    provider = provider_for(lambda _: httpx.Response(200, json=data))
    with pytest.raises(InvalidProviderResponse):
        asyncio.run(provider.generate(worker, generation_request))


def test_invalid_json_is_safe(worker, generation_request):
    provider = provider_for(
        lambda _: httpx.Response(200, content=b"private-backend-body")
    )
    with pytest.raises(InvalidProviderResponse) as caught:
        asyncio.run(provider.generate(worker, generation_request))
    assert "private-backend-body" not in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 404, 500])
def test_http_rejection_is_safe(worker, generation_request, status):
    provider = provider_for(
        lambda _: httpx.Response(status, json={"error": "sensitive backend details"})
    )
    with pytest.raises(ProviderRejected) as caught:
        asyncio.run(provider.generate(worker, generation_request))
    assert caught.value.status_code == status
    assert "sensitive" not in str(caught.value)


def test_error_envelope_is_rejection(worker, generation_request):
    provider = provider_for(lambda _: httpx.Response(200, json={"error": "secret"}))
    with pytest.raises(ProviderRejected, match="generation error"):
        asyncio.run(provider.generate(worker, generation_request))


@pytest.mark.parametrize("status", [502, 503, 504])
def test_unavailable_http_status(worker, generation_request, status):
    provider = provider_for(lambda _: httpx.Response(status, content=b"private"))
    with pytest.raises(BackendUnavailable):
        asyncio.run(provider.generate(worker, generation_request))


@pytest.mark.parametrize(
    "exception,expected,code",
    [
        (httpx.ConnectError, BackendUnavailable, "backend_unavailable"),
        (httpx.ReadError, BackendUnavailable, "backend_unavailable"),
        (httpx.ReadTimeout, ProviderTimeout, "timeout"),
        (httpx.ConnectTimeout, ProviderTimeout, "timeout"),
    ],
)
def test_http_transport_errors(worker, generation_request, exception, expected, code):
    def handler(http_request):
        raise exception("sensitive URL or body", request=http_request)

    provider = provider_for(handler)
    with pytest.raises(expected) as caught:
        asyncio.run(provider.generate(worker, generation_request))
    assert "sensitive" not in str(caught.value)
    health = asyncio.run(provider.health(worker))
    assert health.error_code == code
    assert not health.available


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, records=(), *, block=False, error=None):
        self.records = records
        self.block = block
        self.error = error
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for record in self.records:
            yield record
        if self.error:
            raise self.error
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def record(**changes):
    return (json.dumps(chat_response(**changes)) + "\n").encode()


def test_stream_normalizes_deltas_and_final_metadata(worker, generation_request):
    stream = TrackedStream(
        [
            b"\n",
            record(message={"content": "hello "}, done=False, done_reason=None),
            record(message={"content": "world"}, done=False, done_reason=None),
            record(message={"content": ""}, eval_count=2, total_duration=123),
        ]
    )

    def handler(http_request):
        assert json.loads(http_request.content)["stream"] is True
        return httpx.Response(200, stream=stream)

    async def run():
        async with provider_for(handler).stream(worker, generation_request) as chunks:
            return [chunk async for chunk in chunks]

    chunks = asyncio.run(run())
    assert "".join(chunk.content for chunk in chunks) == "hello world"
    assert [chunk.done for chunk in chunks] == [False, False, True]
    assert chunks[-1].model == worker.model
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage == {"eval_count": 2}
    assert chunks[-1].timing == {"total_duration": 123}
    assert stream.closed


@pytest.mark.parametrize(
    "records,expected",
    [
        ([b"not-json\n"], InvalidProviderResponse),
        ([b"{}\n"], InvalidProviderResponse),
        ([b'{"error": "private"}\n'], ProviderRejected),
        ([record(done=False)], InvalidProviderResponse),
        ([], InvalidProviderResponse),
    ],
)
def test_stream_errors_close_resources(worker, generation_request, records, expected):
    stream = TrackedStream(records)
    provider = provider_for(lambda _: httpx.Response(200, stream=stream))

    async def run():
        async with provider.stream(worker, generation_request) as chunks:
            return [chunk async for chunk in chunks]

    with pytest.raises(expected):
        asyncio.run(run())
    assert stream.closed


@pytest.mark.parametrize("action", ["break", "consumer_error", "no_iteration"])
def test_stream_context_closes_on_early_exit(worker, generation_request, action):
    stream = TrackedStream([record(done=False)], block=True)

    class TrackedTransport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True

    transport = TrackedTransport(lambda _: httpx.Response(200, stream=stream))
    provider = OllamaProvider(transport=transport)

    async def run():
        async with provider.stream(worker, generation_request) as chunks:
            if action == "no_iteration":
                return
            async for _ in chunks:
                if action == "consumer_error":
                    raise RuntimeError("caller failed")
                break

    if action == "consumer_error":
        with pytest.raises(RuntimeError, match="caller failed"):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert stream.closed
    assert transport.closed


def test_stream_cancellation_propagates_and_closes(worker, generation_request):
    async def run():
        stream = TrackedStream(block=True)
        provider = provider_for(lambda _: httpx.Response(200, stream=stream))

        async def consume():
            async with provider.stream(worker, generation_request) as chunks:
                async for _ in chunks:
                    pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(stream.waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed

    asyncio.run(run())


def test_generate_cancellation_closes_response(worker, generation_request):
    async def run():
        stream = TrackedStream(block=True)
        provider = provider_for(lambda _: httpx.Response(200, stream=stream))
        task = asyncio.create_task(provider.generate(worker, generation_request))
        await asyncio.wait_for(stream.waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["generate", "stream", "health"])
def test_total_deadline_and_cleanup(worker, generation_request, operation):
    stream = TrackedStream(block=True)
    provider = provider_for(lambda _: httpx.Response(200, stream=stream))
    worker = worker.model_copy(update={"timeout_seconds": 0.01})

    async def run():
        if operation == "stream":
            async with provider.stream(worker, generation_request) as chunks:
                async for _ in chunks:
                    pass
        elif operation == "health":
            health = await provider.health(worker)
            assert health.error_code == "timeout"
        else:
            await provider.generate(worker, generation_request)

    if operation == "health":
        asyncio.run(run())
    else:
        with pytest.raises(ProviderTimeout):
            asyncio.run(run())
    assert stream.closed


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("private"), httpx.ReadError("private")]
)
def test_stream_transport_error_closes(worker, generation_request, error):
    stream = TrackedStream(error=error)
    provider = provider_for(lambda _: httpx.Response(200, stream=stream))

    async def run():
        async with provider.stream(worker, generation_request) as chunks:
            return [chunk async for chunk in chunks]

    expected = (
        ProviderTimeout if isinstance(error, httpx.ReadTimeout) else BackendUnavailable
    )
    with pytest.raises(expected):
        asyncio.run(run())
    assert stream.closed


def test_stream_http_rejection_closes(worker, generation_request):
    stream = TrackedStream([b"secret"])
    provider = provider_for(lambda _: httpx.Response(404, stream=stream))

    async def run():
        async with provider.stream(worker, generation_request):
            pytest.fail("rejected stream must not reach caller")

    with pytest.raises(ProviderRejected):
        asyncio.run(run())
    assert stream.closed


def test_request_timeout_overrides_worker_default(worker, generation_request):
    def handler(http_request):
        assert http_request.extensions["timeout"]["read"] == 3.0
        assert http_request.extensions["timeout"]["connect"] == 3.0
        return httpx.Response(200, json=chat_response())

    generation_request = generation_request.model_copy(update={"timeout_seconds": 3.0})
    asyncio.run(provider_for(handler).generate(worker, generation_request))


def test_provider_mismatch_and_disabled_stream_rejected_before_http(
    worker, generation_request
):
    def handler(_):
        pytest.fail("invalid target must be rejected before HTTP")

    provider = provider_for(handler)

    async def run():
        wrong = worker.model_copy(update={"provider": "future-provider"})
        with pytest.raises(ProviderRejected):
            await provider.generate(wrong, generation_request)
        with pytest.raises(ProviderRejected):
            await provider.health(wrong)
        for target in (wrong, worker.model_copy(update={"supports_streaming": False})):
            with pytest.raises(ProviderRejected):
                async with provider.stream(target, generation_request):
                    pass

    asyncio.run(run())


def test_native_tool_protocol_and_normalized_ids_round_trip(worker):
    from agentforge.core.inference import ToolCall, ToolDefinition

    calls = [
        ToolCall(id="first", name="read_file", arguments={"path": "one.py"}),
        ToolCall(id="second", name="read_file", arguments={"path": "two.py"}),
    ]
    request = GenerationRequest(
        messages=[
            Message(role="user", content="Inspect"),
            Message(role="assistant", content="", tool_calls=calls),
            Message(
                role="tool",
                content='{"ok":true}',
                tool_call_id="first",
                tool_name="read_file",
            ),
            Message(
                role="tool",
                content='{"ok":false}',
                tool_call_id="second",
                tool_name="read_file",
            ),
        ],
        tools=[
            ToolDefinition(
                name="read_file",
                description="Read source",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            )
        ],
    )

    def handler(http_request):
        payload = json.loads(http_request.content)
        assert payload["tools"] == [
            {"type": "function", "function": request.tools[0].model_dump()}
        ]
        assert payload["messages"][1] == {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "one.py"}}},
                {"function": {"name": "read_file", "arguments": {"path": "two.py"}}},
            ],
        }
        assert payload["messages"][2] == {
            "role": "tool",
            "content": '{"ok":true}',
            "tool_name": "read_file",
        }
        assert payload["messages"][3]["tool_name"] == "read_file"
        return httpx.Response(
            200,
            json=chat_response(
                message={
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "backend-id",
                            "function": {
                                "name": "read_file",
                                "arguments": {"path": "three.py"},
                            },
                        },
                        {
                            "function": {
                                "name": "search_code",
                                "arguments": {"query": "helper"},
                            }
                        },
                    ],
                },
                prompt_eval_count=19,
                eval_count=5,
            ),
        )

    result = asyncio.run(provider_for(handler).generate(worker, request))
    assert result.content == ""
    assert result.tool_calls[0].id == "backend-id"
    assert result.tool_calls[0].arguments == {"path": "three.py"}
    assert result.tool_calls[1].id.startswith("ollama-")
    assert result.tool_calls[1].name == "search_code"
    assert result.token_usage.input_tokens == 19
    assert result.token_usage.output_tokens == 5


@pytest.mark.parametrize(
    "tool_calls",
    [
        [{"function": {"name": "read_file", "arguments": "path=secret"}}],
        [{"function": {"name": "", "arguments": {}}}],
        [{"function": {"name": "read_file", "arguments": []}}],
        [{"id": "", "function": {"name": "read_file", "arguments": {}}}],
    ],
)
def test_malformed_native_tool_calls_are_safe_provider_failures(
    worker, generation_request, tool_calls
):
    provider = provider_for(
        lambda _: httpx.Response(
            200, json=chat_response(message={"content": "", "tool_calls": tool_calls})
        )
    )
    with pytest.raises(InvalidProviderResponse) as caught:
        asyncio.run(provider.generate(worker, generation_request))
    assert "secret" not in str(caught.value)


def test_missing_tool_ids_are_unique_even_for_repeated_tools(
    worker, generation_request
):
    provider = provider_for(
        lambda _: httpx.Response(
            200,
            json=chat_response(
                message={
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_file",
                                "arguments": {"path": "one.py"},
                            }
                        }
                    ]
                    * 2,
                }
            ),
        )
    )

    async def run():
        first = await provider.generate(worker, generation_request)
        second = await provider.generate(worker, generation_request)
        return first.tool_calls + second.tool_calls

    calls = asyncio.run(run())
    assert len({call.id for call in calls}) == 4


def test_stream_still_normalizes_structured_tools(worker, generation_request):
    data = chat_response(
        message={
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "one.py"}}}
            ],
        }
    )
    stream = TrackedStream([json.dumps(data).encode() + b"\n"])
    provider = provider_for(lambda _: httpx.Response(200, stream=stream))

    async def run():
        async with provider.stream(worker, generation_request) as chunks:
            return [chunk async for chunk in chunks]

    chunks = asyncio.run(run())
    assert chunks[0].tool_calls[0].name == "read_file" and chunks[0].done
    assert stream.closed


def test_normalized_usage_partial_and_missing_counts(worker, generation_request):
    provider = provider_for(
        lambda _: httpx.Response(200, json=chat_response(eval_count=4))
    )
    result = asyncio.run(provider.generate(worker, generation_request))
    assert (
        result.token_usage.input_tokens is None
        and result.token_usage.output_tokens == 4
    )
    provider = provider_for(lambda _: httpx.Response(200, json=chat_response()))
    assert (
        asyncio.run(provider.generate(worker, generation_request)).token_usage is None
    )


@pytest.mark.parametrize("thinking", ["I need to inspect the file", "", None])
def test_thinking_is_opaque_normalized_state_not_assistant_content(worker, thinking):
    data = chat_response(
        message={
            "thinking": thinking,
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "source.py"}}}
            ],
        }
    )
    provider = provider_for(lambda _: httpx.Response(200, json=data))
    result = asyncio.run(
        provider.generate(
            worker,
            GenerationRequest(messages=[Message(role="user", content="Inspect")]),
        )
    )
    assert result.reasoning == thinking
    assert result.content == ""
    assert result.tool_calls[0].name == "read_file"


def test_absent_thinking_stays_unknown_and_absent_on_wire(worker, generation_request):
    def handler(http_request):
        payload = json.loads(http_request.content)
        assert all(
            "thinking" not in message and "reasoning" not in message
            for message in payload["messages"]
        )
        return httpx.Response(200, json=chat_response())

    result = asyncio.run(provider_for(handler).generate(worker, generation_request))
    assert result.reasoning is None and result.content == "hello back"


@pytest.mark.parametrize(
    "thinking", [42, True, ["PRIVATE THINKING"], {"text": "PRIVATE THINKING"}]
)
def test_malformed_thinking_is_rejected_without_leaking_state(
    worker, generation_request, thinking
):
    provider = provider_for(
        lambda _: httpx.Response(
            200, json=chat_response(message={"content": "", "thinking": thinking})
        )
    )
    with pytest.raises(InvalidProviderResponse) as caught:
        asyncio.run(provider.generate(worker, generation_request))
    assert str(caught.value) == "Invalid Ollama chat response"


def test_stream_normalizes_thinking_deltas_separately_and_closes(
    worker, generation_request
):
    records = [
        chat_response(done=False, message={"content": "", "thinking": "opaque first "}),
        chat_response(done=False, message={"content": "", "thinking": "opaque second"}),
        chat_response(done=False, message={"content": "answer"}),
        chat_response(message={"content": ""}),
    ]
    stream = TrackedStream([json.dumps(record).encode() + b"\n" for record in records])
    provider = provider_for(lambda _: httpx.Response(200, stream=stream))

    async def run():
        async with provider.stream(worker, generation_request) as chunks:
            return [chunk async for chunk in chunks]

    chunks = asyncio.run(run())
    assert [chunk.reasoning for chunk in chunks] == [
        "opaque first ",
        "opaque second",
        None,
        None,
    ]
    assert "".join(chunk.content for chunk in chunks) == "answer"
    assert chunks[-1].done and stream.closed
