"""Both adapters share safe HTTP observations; no live services or credentials."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from agentforge.core.inference import GenerationRequest, Message
from agentforge.core.provider_errors import ProviderError
from agentforge.core.worker import Worker
from agentforge.db.diagnostics import DiagnosticsRepository
from agentforge.providers.http import rate_limits
from agentforge.providers.ollama import OllamaProvider
from agentforge.providers.openai_compatible import OpenAICompatibleProvider
from agentforge.workers.config import ProviderConnection, WorkersConfig
from agentforge.workers.diagnostic_service import WorkerDiagnosticsService
from tests.test_ollama import chat_response
from tests.test_openai_compatible import BytesStream, completion, event

PRIVATE = "PRIVATE_UNTRUSTED_HEADER_BODY"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", 0),
        (" 60 ", 60),
        ("86400", 86400),
        ("86401", None),
        ("9" * 10_000, None),
        ("-1", None),
        ("0.5", None),
        ("NaN", None),
        ("∞", None),
        ("1, 2", None),
        (PRIVATE, None),
        ("", None),
        ("Sat, 24 Oct 2026 12:01:00 GMT", 60),
        ("Sat, 24 Oct 2026 11:59:00 GMT", None),
        ("Mon, 26 Oct 2026 12:00:00 GMT", None),
        ("Sat, 24 Oct 2026 12:01:00", None),
    ],
    ids=[
        "zero",
        "seconds",
        "max",
        "over-max",
        "oversized",
        "negative",
        "fractional",
        "nan",
        "unicode",
        "combined",
        "private",
        "empty",
        "date",
        "past-date",
        "distant-date",
        "naive-date",
    ],
)
def test_retry_after_bounded_parsing(value, expected):
    observed = rate_limits(
        httpx.Headers({"Retry-After": value}, encoding="utf-8"),
        now=datetime(2026, 10, 24, 12, tzinfo=UTC),
    )
    assert (observed.retry_after_seconds if observed else None) == expected


def test_missing_duplicate_and_vendor_headers_stay_unknown():
    for headers in (
        {},
        {"X-RateLimit-Remaining": "100", "X-Private": PRIVATE},
        [("Retry-After", "1"), ("Retry-After", "2")],
    ):
        assert rate_limits(httpx.Headers(headers)) is None


def test_expired_date_is_not_rounded_up_to_zero():
    assert (
        rate_limits(
            httpx.Headers({"Retry-After": "Sat, 24 Oct 2026 12:00:00 GMT"}),
            now=datetime(2026, 10, 24, 12, 0, 0, 1, tzinfo=UTC),
        )
        is None
    )


@pytest.fixture(params=["ollama", "openai_compatible"])
def target(request):
    connection = ProviderConnection(
        id="connection", type=request.param, base_url="https://inference.invalid/v1"
    )
    worker = Worker(
        id="worker",
        provider=connection.type,
        provider_connection=connection.id,
        model="configured-model",
        supports_streaming=True,
    )
    return connection, worker


def adapter(connection, handler):
    cls = OllamaProvider if connection.type == "ollama" else OpenAICompatibleProvider
    return cls(connection, transport=httpx.MockTransport(handler))


async def close(provider):
    if isinstance(provider, OpenAICompatibleProvider):
        await provider.aclose()


@pytest.mark.parametrize("operation", ["generate", "stream"])
@pytest.mark.parametrize(
    "status,code",
    [
        (400, "invalid_request"),
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "backend_unavailable"),
        (503, "backend_unavailable"),
    ],
)
def test_safe_status_codes_no_body_read_retry_or_redirect(
    target, operation, status, code
):
    connection, worker = target
    body = BytesStream([PRIVATE.encode()], block=True)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            stream=body,
            headers={"Retry-After": "5", "X-Untrusted": PRIVATE},
        )

    provider = adapter(connection, respond)

    async def execute():
        request = GenerationRequest(messages=[Message(role="user", content="test")])
        try:
            with pytest.raises(ProviderError) as caught:
                if operation == "generate":
                    await provider.generate(worker, request)
                else:
                    async with provider.stream(worker, request) as chunks:
                        _ = [chunk async for chunk in chunks]
            assert caught.value.code == code
            assert caught.value.status_code == status
            assert caught.value.rate_limits.retry_after_seconds == 5
            assert PRIVATE not in str(caught.value) + repr(vars(caught.value))
            assert len(requests) == 1 and body.closed and body.reads == 0
        finally:
            await close(provider)

    asyncio.run(execute())


@pytest.mark.parametrize("operation", ["generate", "stream"])
def test_success_observations_and_no_ambient_credentials(
    target, operation, tmp_path, monkeypatch
):
    connection, worker = target
    netrc = tmp_path / "netrc"
    netrc.write_text("machine inference.invalid login ambient password " + PRIVATE)
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy.invalid")
    requests = []

    def respond(request):
        requests.append(request)
        assert "authorization" not in request.headers
        if operation == "generate":
            data = chat_response() if connection.type == "ollama" else completion()
            return httpx.Response(200, json=data, headers={"Retry-After": "7"})
        data = (
            (json.dumps(chat_response()) + "\n").encode()
            if connection.type == "ollama"
            else event({"content": "answer"}, finish="stop") + b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=data, headers={"Retry-After": "7"})

    provider = adapter(connection, respond)

    async def execute():
        request = GenerationRequest(messages=[Message(role="user", content="test")])
        try:
            if operation == "generate":
                result = await provider.generate(worker, request)
            else:
                async with provider.stream(worker, request) as chunks:
                    result = [chunk async for chunk in chunks][-1]
                assert result.done
            assert result.rate_limits.retry_after_seconds == 7
            assert len(requests) == 1 and PRIVATE not in result.model_dump_json()
        finally:
            await close(provider)

    asyncio.run(execute())


@pytest.mark.parametrize(
    "status,code",
    [
        (200, None),
        (401, "authentication_failed"),
        (429, "rate_limited"),
        (503, "backend_unavailable"),
    ],
)
def test_diagnostic_checkpoint_safe_observations(target, database, status, code):
    from agentforge.db.database import create_session_factory

    connection, worker = target
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            json=chat_response() if connection.type == "ollama" else completion(),
            headers={"Retry-After": "9", "X-Secret": PRIVATE},
        )

    provider = adapter(connection, respond)
    repository = DiagnosticsRepository(create_session_factory(database[0]))
    config = WorkersConfig(providers=[connection], workers=[worker])

    async def execute():
        service = WorkerDiagnosticsService(
            config, {connection.id: provider}, repository
        )
        try:
            observed = await service.probe(worker.id)
            assert observed.error_code == code
            assert observed.rate_limits.retry_after_seconds == 9
            assert observed.status == ("successful" if status == 200 else "failed")
            restarted = WorkerDiagnosticsService(config, {}, repository)
            assert restarted.get(worker.id).generation.latest == observed
            assert PRIVATE not in restarted.get(worker.id).model_dump_json()
            assert len(requests) == 1
        finally:
            await service.close()
            await close(provider)

    asyncio.run(execute())
