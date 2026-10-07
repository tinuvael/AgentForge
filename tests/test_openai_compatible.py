"""Bounded HTTP and SSE tests under the unchanged socket/DNS guard."""

import asyncio
import json

import httpx
import pytest

from agentforge.core.inference import (
    GenerationRequest,
    Message,
    ToolCall,
    ToolDefinition,
)
from agentforge.core.provider_errors import (
    BackendUnavailable,
    InvalidProviderResponse,
    ProviderRejected,
    ProviderTimeout,
)
from agentforge.core.worker import Worker
from agentforge.providers import openai_compatible as protocol
from agentforge.providers.openai_compatible import OpenAICompatibleProvider
from agentforge.workers.config import ProviderConnection

SECRET = "PRIVATE_TEST_BEARER_VALUE"
REASONING = "PRIVATE_MODEL_REASONING"


def run(coroutine):
    async def bounded():
        async with asyncio.timeout(5):
            return await coroutine

    return asyncio.run(bounded())


@pytest.fixture
def worker():
    return Worker(
        id="remote-worker",
        provider="openai_compatible",
        provider_connection="remote",
        model="configured-model",
        supports_tools=True,
        supports_streaming=True,
    )


@pytest.fixture
def generation_request():
    return GenerationRequest(messages=[Message(role="user", content="hello")])


def provider(
    handler, *, base="https://gateway.invalid/v1", auth=False, stream_usage=False
):
    return OpenAICompatibleProvider(
        ProviderConnection(
            id="remote",
            type="openai_compatible",
            base_url=base,
            api_key_env="AGENTFORGE_TEST_TOKEN" if auth else None,
            stream_usage=stream_usage,
        ),
        transport=httpx.MockTransport(handler),
    )


def completion(*, content="answer", calls=None, usage=None, **message):
    return {
        "model": "response-observation",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if calls else "stop",
                "message": {
                    "role": "assistant",
                    "content": content,
                    **message,
                    **({"tool_calls": calls} if calls else {}),
                },
            }
        ],
        **({"usage": usage} if usage is not None else {}),
    }


def call(identity="issued-id", name="read_file", arguments='{"path":"source.py"}'):
    return {
        "id": identity,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


async def generate(p, worker, generation_request):
    try:
        return await p.generate(worker, generation_request)
    finally:
        await p.aclose()


@pytest.mark.parametrize(
    "base,path",
    [
        ("https://gateway.invalid/v1", "/v1/chat/completions"),
        ("https://gateway.invalid/v1/", "/v1/chat/completions"),
        ("http://ai395.local:8000/api", "/api/chat/completions"),
        ("http://lan.invalid:8000", "/chat/completions"),
    ],
)
def test_text_url_model_and_optional_auth(
    worker, generation_request, monkeypatch, caplog, base, path
):
    monkeypatch.setenv("AGENTFORGE_TEST_TOKEN", SECRET)
    requests = []

    def handler(req):
        requests.append(req)
        assert req.url.path == path
        assert req.headers["authorization"] == "Bearer " + SECRET
        assert json.loads(req.content)["model"] == "configured-model"
        return httpx.Response(200, json=completion())

    p = provider(handler, base=base, auth=True)
    result = run(generate(p, worker, generation_request))
    assert result.content == "answer" and result.finish_reason == "stop"
    assert result.reasoning is None and result.token_usage is None
    assert result.model == "response-observation" and worker.model == "configured-model"
    assert result.generation_timing is None and result.timing is None
    assert len(requests) == 1 and p._http.is_closed
    assert SECRET not in repr(p) + caplog.text + result.model_dump_json()


def test_messages_tools_and_private_reasoning_serialization(worker):
    issued = ToolCall(
        id="provider-issued", name="read_file", arguments={"path": "source.py"}
    )
    generation_request = GenerationRequest(
        system="extra system",
        messages=[
            Message(role="system", content="system"),
            Message(role="user", content="user"),
            Message(role="assistant", content="assistant"),
            Message(
                role="assistant", content="", reasoning=REASONING, tool_calls=[issued]
            ),
            Message(
                role="tool",
                content="tool output",
                tool_call_id=issued.id,
                tool_name=issued.name,
            ),
        ],
        tools=[
            ToolDefinition(
                name="read_file", description="read", parameters={"type": "object"}
            )
        ],
    )

    def handler(req):
        payload = json.loads(req.content)
        assert [m["role"] for m in payload["messages"]] == [
            "system",
            "system",
            "user",
            "assistant",
            "assistant",
            "tool",
        ]
        assert payload["messages"][-1] == {
            "role": "tool",
            "content": "tool output",
            "tool_call_id": "provider-issued",
        }
        assert payload["messages"][-2]["tool_calls"] == [
            call("provider-issued", arguments=json.dumps({"path": "source.py"}))
        ]
        assert REASONING not in req.content.decode()
        assert payload["tools"][0]["function"]["name"] == "read_file"
        assert "authorization" not in req.headers
        return httpx.Response(
            200,
            json=completion(
                content=None,
                calls=[call("one"), call("two")],
                reasoning_content=REASONING,
            ),
        )

    result = run(generate(provider(handler), worker, generation_request))
    assert [c.id for c in result.tool_calls] == ["one", "two"]
    assert result.content == "" and result.reasoning == REASONING
    assert REASONING not in repr(result)


@pytest.mark.parametrize(
    "usage,counts",
    [
        (
            {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            (10, 4, 14),
        ),
        ({"completion_tokens": 0}, (None, 0, None)),
        ({"total_tokens": 9}, (None, None, 9)),
        ({}, None),
        (None, None),
    ],
)
def test_usage_only_reported(worker, generation_request, usage, counts):
    result = run(
        generate(
            provider(lambda _: httpx.Response(200, json=completion(usage=usage))),
            worker,
            generation_request,
        )
    )
    assert (
        tuple(result.token_usage.model_dump().values()) if result.token_usage else None
    ) == counts


@pytest.mark.parametrize(
    "data",
    [
        {},
        [],
        {"choices": []},
        completion(calls=[call(arguments="not json")]),
        completion(calls=[call(arguments="[]")]),
        completion(calls=[call(), call()]),
        completion(calls=[call(arguments='{"value":NaN}')]),
        completion(usage={"prompt_tokens": -1}),
        completion(usage={"prompt_tokens": True}),
        completion(usage={"completion_tokens": "5"}),
        completion(content=23),
        completion(reasoning_content={"private": SECRET}),
        {
            "choices": [
                {"message": {"role": "tool", "content": "bad"}, "finish_reason": "stop"}
            ]
        },
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "bad"},
                    "finish_reason": None,
                }
            ]
        },
    ],
)
def test_malformed_shapes_safe(worker, generation_request, data):
    with pytest.raises(InvalidProviderResponse) as caught:
        run(
            generate(
                provider(lambda _: httpx.Response(200, json=data)),
                worker,
                generation_request,
            )
        )
    assert SECRET not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("body", [b"not json", b"\xff", b"[", b'{"choices":NaN}'])
def test_malformed_json(worker, generation_request, body):
    with pytest.raises(InvalidProviderResponse):
        run(
            generate(
                provider(lambda _: httpx.Response(200, content=body)),
                worker,
                generation_request,
            )
        )


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, pieces, *, block=False, error=None):
        self.pieces = pieces
        self.block = block
        self.error = error
        self.closed = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.reads = 0

    async def __aiter__(self):
        self.entered.set()
        if self.block:
            await self.release.wait()
        for piece in self.pieces:
            self.reads += 1
            yield piece
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 429, 500, 503])
def test_status_never_reads_error_or_follows_redirect(
    worker, generation_request, monkeypatch, status
):
    monkeypatch.setenv("AGENTFORGE_TEST_TOKEN", SECRET)
    body = BytesStream([SECRET.encode() * 10000])
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(
            status, stream=body, headers={"Location": "https://other.invalid/stolen"}
        )

    with pytest.raises(
        BackendUnavailable if status >= 500 else ProviderRejected
    ) as caught:
        run(generate(provider(handler, auth=True), worker, generation_request))
    assert len(requests) == 1 and body.closed and body.reads == 0
    assert SECRET not in str(caught.value) + repr(caught.value)


def test_structured_error_not_leaked(worker, generation_request):
    with pytest.raises(ProviderRejected) as caught:
        run(
            generate(
                provider(
                    lambda _: httpx.Response(200, json={"error": {"message": SECRET}})
                ),
                worker,
                generation_request,
            )
        )
    assert SECRET not in str(caught.value) + repr(caught.value)


def test_response_size_bound(worker, generation_request, monkeypatch):
    monkeypatch.setattr(protocol, "MAX_RESPONSE_BYTES", 4096)
    body = BytesStream([b"x" * 4096] * 100)
    with pytest.raises(InvalidProviderResponse):
        run(
            generate(
                provider(lambda _: httpx.Response(200, stream=body)),
                worker,
                generation_request,
            )
        )
    assert body.closed and body.reads <= 2


@pytest.mark.parametrize(
    "failure,error",
    [
        (httpx.ConnectTimeout(SECRET), ProviderTimeout),
        (httpx.ReadTimeout(SECRET), ProviderTimeout),
        (httpx.ConnectError(SECRET), BackendUnavailable),
        (httpx.ReadError(SECRET), BackendUnavailable),
    ],
)
def test_transport_failure_is_safe_and_single_attempt(
    worker, generation_request, failure, error
):
    requests = []

    def handler(req):
        requests.append(req)
        raise failure

    with pytest.raises(error) as caught:
        run(generate(provider(handler), worker, generation_request))
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert len(requests) == 1


def event(delta=None, *, finish=None, usage=None):
    value = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return ("data: " + json.dumps(value) + "\n\n").encode()


async def collect(p, worker, generation_request):
    try:
        async with p.stream(worker, generation_request) as chunks:
            return [c async for c in chunks]
    finally:
        await p.aclose()


@pytest.mark.parametrize(
    "usage", [None, {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}]
)
def test_stream_text_keepalive_done_and_usage(worker, generation_request, usage):
    data = (
        b": ping\r\n\r\n"
        + event({"role": "assistant"})
        + event({"content": "one"})
        + event({"content": "two", "reasoning_content": REASONING})
        + event(finish="stop")
    )
    if usage is not None:
        data += (
            "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
        ).encode()
    data += b"data: [DONE]\n\n"
    body = BytesStream([data[i : i + 7] for i in range(0, len(data), 7)])
    p = provider(lambda _: httpx.Response(200, stream=body), stream_usage=True)
    chunks = run(collect(p, worker, generation_request))
    assert [c.content for c in chunks] == ["one", "two", ""]
    assert chunks[1].reasoning == REASONING and chunks[-1].done
    assert chunks[-1].finish_reason == "stop"
    assert (chunks[-1].usage if usage else chunks[-1].token_usage) == usage
    assert body.closed and p._http.is_closed and not p._active


def test_stream_multiple_fragmented_tool_calls(worker, generation_request):
    deltas = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "first-",
                    "type": "function",
                    "function": {"name": "read_", "arguments": '{"path":'},
                },
                {
                    "index": 1,
                    "id": "second",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"other.py"}',
                    },
                },
            ]
        },
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "id",
                    "function": {"name": "file", "arguments": '"source.py"}'},
                }
            ]
        },
    ]
    body = BytesStream(
        [*(event(d) for d in deltas), event(finish="tool_calls"), b"data: [DONE]\n\n"]
    )
    chunks = run(
        collect(
            provider(lambda _: httpx.Response(200, stream=body)),
            worker,
            generation_request,
        )
    )
    assert len(chunks) == 1 and chunks[0].done
    assert [c.id for c in chunks[0].tool_calls] == ["first-id", "second"]
    assert [c.arguments for c in chunks[0].tool_calls] == [
        {"path": "source.py"},
        {"path": "other.py"},
    ]
    assert body.closed


@pytest.mark.parametrize(
    "body",
    [
        b"data: invalid\n\n",
        b"data: []\n\n",
        b"data: [DONE]\n\n",
        event({"content": 123}),
        event({"role": "tool"}),
        event({"tool_calls": [{"index": True}]}),
        event({"tool_calls": [{"index": -1}]}),
        event({"tool_calls": [{"index": 200}]}),
        event({"tool_calls": [{"index": 0, "id": 3}]}),
        event({"tool_calls": [{"index": 0, "function": []}]}),
        event(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "one",
                        "function": {"name": "read_file", "arguments": "[]"},
                    }
                ]
            },
            finish="tool_calls",
        )
        + b"data: [DONE]\n\n",
        event(
            {
                "tool_calls": [
                    {
                        "index": 1,
                        "id": "one",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ]
            },
            finish="tool_calls",
        )
        + b"data: [DONE]\n\n",
        event({"content": "x"}, finish="stop"),
        event(finish="stop") + event({"content": "late"}),
        b'data: {"choices":[]}\n\n',
        b'data: {"error":{"message":"PRIVATE_TEST_BEARER_VALUE"}}\n\n',
        b"data: \xff\n\n",
        b"data: [DONE]",
    ],
)
def test_malformed_stream_fails_closed(worker, generation_request, body):
    stream = BytesStream([body])
    with pytest.raises((InvalidProviderResponse, ProviderRejected)) as caught:
        run(
            collect(
                provider(lambda _: httpx.Response(200, stream=stream)),
                worker,
                generation_request,
            )
        )
    assert stream.closed and SECRET not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b"data: " + b"x" * 4096,
        b":" + b"x" * 4096 + b"\n\n",
        b"data: " + b"x" * 2000 + b"\ndata: " + b"x" * 2000 + b"\n\n",
    ],
)
def test_stream_event_bound(worker, generation_request, monkeypatch, body):
    monkeypatch.setattr(protocol, "MAX_EVENT_BYTES", 1024)
    stream = BytesStream([body])
    with pytest.raises(InvalidProviderResponse):
        run(
            collect(
                provider(lambda _: httpx.Response(200, stream=stream)),
                worker,
                generation_request,
            )
        )
    assert stream.closed


def test_stream_total_bound(worker, generation_request, monkeypatch):
    monkeypatch.setattr(protocol, "MAX_STREAM_BYTES", 4096)
    stream = BytesStream([b":keepalive\n\n" * 100] * 100)
    with pytest.raises(InvalidProviderResponse):
        run(
            collect(
                provider(lambda _: httpx.Response(200, stream=stream)),
                worker,
                generation_request,
            )
        )
    assert stream.closed and stream.reads < 10


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("action", ["cancel", "timeout", "close"])
def test_active_request_cleanup(worker, generation_request, streaming, action):
    async def execute():
        body = BytesStream([], block=True)
        p = provider(lambda _: httpx.Response(200, stream=body))
        req = (
            generation_request.model_copy(update={"timeout_seconds": 0.02})
            if action == "timeout"
            else generation_request
        )

        async def execute_request():
            if streaming:
                async with p.stream(worker, req) as chunks:
                    return [c async for c in chunks]
            return await p.generate(worker, req)

        task = asyncio.create_task(execute_request())
        await body.entered.wait()
        if action == "cancel":
            task.cancel()
        elif action == "close":
            await p.aclose()
        with pytest.raises(
            ProviderTimeout if action == "timeout" else asyncio.CancelledError
        ):
            await task
        await p.aclose()
        assert body.closed and p._http.is_closed and not p._active

    run(execute())


def test_stream_disconnect_and_early_stop(worker, generation_request):
    async def execute():
        broken = BytesStream([], error=httpx.ReadError(SECRET))
        p = provider(lambda _: httpx.Response(200, stream=broken))
        with pytest.raises(BackendUnavailable) as caught:
            await collect(p, worker, generation_request)
        assert SECRET not in str(caught.value) and broken.closed
        body = BytesStream([event({"content": "x"}) + b":keepalive\n\n" * 1000])
        p = provider(lambda _: httpx.Response(200, stream=body))
        try:
            async with p.stream(worker, generation_request) as chunks:
                async for chunk in chunks:
                    assert chunk.content == "x"
                    break
            assert body.closed and not p._active
        finally:
            await p.aclose()

    run(execute())


def test_shared_client_and_rejected_payload_overrides(worker, generation_request):
    async def execute():
        p = provider(lambda _: httpx.Response(200, json=completion()))
        await asyncio.gather(
            p.generate(worker, generation_request),
            p.generate(worker, generation_request),
        )
        client = p._http
        await p.generate(worker, generation_request)
        assert p._http is client and not client.is_closed
        for options in (
            {"model": "replacement"},
            {"base_url": "https://other.invalid"},
            {"api_key": SECRET},
            {"n": 2},
        ):
            with pytest.raises(ProviderRejected) as caught:
                await p.generate(
                    worker, generation_request.model_copy(update={"options": options})
                )
            assert SECRET not in str(caught.value)
        await p.aclose()
        with pytest.raises(ProviderRejected):
            await p.generate(worker, generation_request)

    run(execute())


def test_stream_usage_opt_in_and_message_bounds(worker, generation_request):
    async def execute():
        for enabled in (False, True):

            def handler(req, enabled=enabled):
                payload = json.loads(req.content)
                assert payload.get("stream_options") == (
                    {"include_usage": True} if enabled else None
                )
                assert req.extensions["timeout"]["connect"] == 10.0
                assert req.headers["accept-encoding"] == "identity"
                return httpx.Response(
                    200, content=event(finish="stop") + b"data: [DONE]\n\n"
                )

            p = provider(handler, stream_usage=enabled)
            assert (await collect(p, worker, generation_request))[-1].done

    run(execute())


@pytest.mark.parametrize(
    "body",
    [
        b'{"choices":[],"choices":[]}',
        json.dumps(
            completion(calls=[call(arguments='{"path":"one","path":"two"}')])
        ).encode(),
        json.dumps(completion(content=None)).encode(),
        json.dumps(
            completion(function_call={"name": "read_file", "arguments": "{}"})
        ).encode(),
    ],
)
def test_ambiguous_and_unsupported_completions_rejected(
    worker, generation_request, body
):
    with pytest.raises(InvalidProviderResponse):
        run(
            generate(
                provider(lambda _: httpx.Response(200, content=body)),
                worker,
                generation_request,
            )
        )


@pytest.mark.parametrize("streaming", [False, True])
def test_compressed_response_rejected_before_body_read(
    worker, generation_request, streaming
):
    body = BytesStream([b"unread compressed bytes"])
    p = provider(
        lambda _: httpx.Response(200, stream=body, headers={"Content-Encoding": "gzip"})
    )
    with pytest.raises(InvalidProviderResponse):
        run(
            collect(p, worker, generation_request)
            if streaming
            else generate(p, worker, generation_request)
        )
    assert body.closed and body.reads == 0


@pytest.mark.parametrize(
    "delta",
    [
        {"tool_calls": {}},
        {"tool_calls": False},
        {"tool_calls": ""},
        {"function_call": {"name": "read_file"}},
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "same",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
                {
                    "index": 1,
                    "id": "same",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
            ]
        },
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "x" * 201,
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        },
    ],
)
def test_stream_malformed_or_duplicate_calls_rejected(
    worker, generation_request, delta
):
    body = BytesStream([event(delta, finish="tool_calls") + b"data: [DONE]\n\n"])
    with pytest.raises(InvalidProviderResponse):
        run(
            collect(
                provider(lambda _: httpx.Response(200, stream=body)),
                worker,
                generation_request,
            )
        )
    assert body.closed


def test_capability_binding_health_and_closed_state(worker, generation_request):
    async def execute():
        calls = []

        def handler(req):
            calls.append(req)
            return httpx.Response(200, json=completion())

        p = provider(handler)
        assert (await p.health(worker)).error_code == "not_probed"
        assert not calls
        with pytest.raises(ProviderRejected):
            async with p.stream(
                worker.model_copy(update={"supports_streaming": False}),
                generation_request,
            ):
                pass
        for changes in ({"provider": "other"}, {"provider_connection": "other"}):
            with pytest.raises(ProviderRejected):
                await p.generate(worker.model_copy(update=changes), generation_request)
        assert not calls
        # Two Workers on a connection share a client without sharing model identity.
        other = worker.model_copy(update={"id": "another", "model": "another-model"})
        await asyncio.gather(
            p.generate(worker, generation_request),
            p.generate(other, generation_request),
        )
        assert [json.loads(req.content)["model"] for req in calls] == [
            "configured-model",
            "another-model",
        ]
        await p.aclose()

    run(execute())


def test_small_delta_arrives_before_backend_finishes(worker, generation_request):
    class LiveStream(BytesStream):
        async def __aiter__(self):
            yield event({"content": "immediate"})
            self.entered.set()
            await self.release.wait()
            yield event(finish="stop") + b"data: [DONE]\n\n"

    async def execute():
        body = LiveStream([])
        p = provider(lambda _: httpx.Response(200, stream=body))
        try:
            async with p.stream(worker, generation_request) as chunks:
                async with asyncio.timeout(0.5):
                    first = await anext(chunks)
                assert first.content == "immediate" and not body.release.is_set()
                body.release.set()
                remaining = [chunk async for chunk in chunks]
                assert remaining[-1].done
            assert body.closed
        finally:
            await p.aclose()

    run(execute())


def test_multiline_data_event(worker, generation_request):
    body = BytesStream(
        [
            b'data: {"choices":\r\ndata: [{"index":0,'
            b'"delta":{"content":"text"},"finish_reason":"stop"}]}\r\n\r\n'
            b"data: [DONE]\r\n\r\n"
        ]
    )
    chunks = run(
        collect(
            provider(lambda _: httpx.Response(200, stream=body)),
            worker,
            generation_request,
        )
    )
    assert [chunk.content for chunk in chunks] == ["text", ""]
    assert chunks[-1].done and body.closed
