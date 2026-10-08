"""Operator diagnostics: synthetic inference, truthful evidence and bounded storage."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC

import httpx
import pytest
from sqlalchemy import func, select

from agentforge.application.service import Application
from agentforge.cli import main
from agentforge.core.inference import (
    GenerationChunk,
    GenerationResult,
    GenerationTiming,
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
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.diagnostics import DiagnosticsRepository
from agentforge.db.models import WorkerDiagnosticRecord
from agentforge.providers.ollama import OllamaProvider
from agentforge.providers.openai_compatible import OpenAICompatibleProvider
from agentforge.web.app import create_app
from agentforge.workers.config import ProviderConnection, WorkersConfig
from agentforge.workers.diagnostic_service import (
    WorkerDiagnosticsService,
    endpoint_class,
)
from agentforge.workers.diagnostics import (
    DiagnosticBusy,
    DiagnosticsUnavailable,
    DiagnosticWorkerNotFound,
)

PRIVATE = "SECRET-REPOSITORY-AND-PROVIDER-BODY"


def configuration(**updates):
    return WorkersConfig(
        workers=[
            Worker(
                id="worker",
                provider="ollama",
                endpoint="http://127.0.0.1/private",
                model="configured",
                deployment_label="machine",
                context_window=32768,
                supports_tools=True,
                supports_streaming=True,
                options={"stop": PRIVATE, "num_predict": -1},
                **updates,
            )
        ]
    )


class ScriptedProvider:
    def __init__(self, *, result=None, error=None, chunks=None, health=None):
        self.result = result or GenerationResult(
            content="agentforge", model="backend-alias"
        )
        self.error = error
        self.chunks = (
            chunks
            if chunks is not None
            else [
                GenerationChunk(content="agentforge", model="backend-alias"),
                GenerationChunk(content="", model="backend-alias", done=True),
            ]
        )
        self.health_result = health or WorkerHealth(
            backend_available=True, model_available=True
        )
        self.requests = []
        self.health_calls = 0
        self.stream_closed = False
        self.closed = False

    async def generate(self, worker, request):
        self.requests.append((worker, request))
        if self.error:
            raise self.error
        return self.result

    async def health(self, worker):
        self.health_calls += 1
        if self.error:
            raise self.error
        return self.health_result

    @asynccontextmanager
    async def stream(self, worker, request):
        self.requests.append((worker, request))

        async def chunks():
            for value in self.chunks:
                if isinstance(value, Exception):
                    raise value
                yield value

        try:
            yield chunks()
        finally:
            self.stream_closed = True

    async def aclose(self):
        self.closed = True


def service(config=None, provider=None, database=None):
    config = config or configuration()
    repository = (
        DiagnosticsRepository(create_session_factory(database[0])) if database else None
    )
    return WorkerDiagnosticsService(
        config, {"ollama": provider or ScriptedProvider()}, repository
    )


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost/private", "local"),
        ("http://sub.localhost", "local"),
        ("http://127.9.1.2", "local"),
        ("http://[::1]", "local"),
        ("http://[::ffff:127.0.0.1]", "local"),
        ("http://10.0.0.1", "private"),
        ("http://172.16.0.2", "private"),
        ("http://192.168.50.12", "private"),
        ("http://169.254.1.1", "private"),
        ("http://[fd00::1]", "private"),
        ("http://[fe80::1]", "private"),
        ("https://8.8.8.8/v1", "remote"),
        ("https://[2606:4700::1111]", "remote"),
        ("https://model.example.com/private/v1", "unknown"),
        ("http://ai395.local", "unknown"),
        ("http://0.0.0.0", "unknown"),
        ("http://192.0.2.1", "unknown"),
        ("http://224.0.0.1", "unknown"),
    ],
)
def test_conservative_endpoint_classification_without_dns(url, expected):
    assert endpoint_class(url) == expected


def test_listing_and_configuration_are_passive(database):
    provider = ScriptedProvider(error=AssertionError("must not contact Provider"))
    diagnostics = service(provider=provider, database=database)
    configured = diagnostics.get("worker")
    assert configured.configuration.supports_tools is True
    assert configured.configuration.context_window == 32768
    for kind in ("health", "generation", "tools", "streaming"):
        assert getattr(configured, kind).latest is None
    assert diagnostics.list().workers == (configured,)
    assert provider.health_calls == 0 and provider.requests == []
    with pytest.raises(DiagnosticWorkerNotFound):
        diagnostics.get("absent")
    for bounds in ({"limit": 101}, {"limit": True}, {"offset": -1}):
        with pytest.raises(ValueError):
            diagnostics.list(**bounds)


@pytest.mark.parametrize(
    "result,code,backend,model,reachable",
    [
        ({"models": [{"name": "configured:latest"}]}, None, True, True, True),
        ({"models": []}, "model_unavailable", True, False, True),
        (httpx.Response(503, text=PRIVATE), "backend_unavailable", False, None, True),
        (httpx.Response(401, text=PRIVATE), "rejected", False, None, True),
        (httpx.Response(200, text=PRIVATE), "invalid_response", False, None, True),
        (httpx.ConnectError(PRIVATE), "backend_unavailable", False, None, False),
        (httpx.ReadTimeout(PRIVATE), "timeout", False, None, None),
    ],
)
def test_ollama_explicit_health(result, code, backend, model, reachable):
    requests = []

    def respond(request):
        requests.append(request)
        if isinstance(result, Exception):
            raise result
        return (
            result
            if isinstance(result, httpx.Response)
            else httpx.Response(200, json=result)
        )

    provider = OllamaProvider(transport=httpx.MockTransport(respond))
    observed = asyncio.run(service(provider=provider).check("worker"))
    assert len(requests) == 1 and requests[0].method == "GET"
    assert observed.status == ("available" if code is None else "failed")
    assert observed.error_code == code
    assert observed.backend_available is backend
    assert observed.model_available is model
    assert observed.backend_reachable is reachable
    assert observed.generation_success is None and observed.token_usage is None
    assert PRIVATE not in observed.model_dump_json()


def test_compatible_health_not_probed_and_probe_is_synthetic(database, monkeypatch):
    monkeypatch.setenv("TEST_DIAGNOSTIC_TOKEN", PRIVATE)
    connection = ProviderConnection(
        id="cloud",
        type="openai_compatible",
        base_url="https://model.example.com/private/v1",
        api_key_env="TEST_DIAGNOSTIC_TOKEN",
    )
    worker = Worker(
        id="cloud-worker",
        provider="openai_compatible",
        provider_connection="cloud",
        model="configured",
        supports_tools=True,
        options={"stop": PRIVATE, "max_tokens": 90000},
    )
    config = WorkersConfig(providers=[connection], workers=[worker])
    requests = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["max_tokens"] == 32 and body["temperature"] == 0
        assert PRIVATE not in request.content.decode()
        assert body["messages"] == [
            {"role": "user", "content": "Reply with the word agentforge."}
        ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": PRIVATE},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    provider = OpenAICompatibleProvider(
        connection, transport=httpx.MockTransport(respond)
    )
    diagnostics = WorkerDiagnosticsService(
        config,
        {"cloud": provider},
        DiagnosticsRepository(create_session_factory(database[0])),
    )

    async def run():
        try:
            health = await diagnostics.check(worker.id)
            assert health.status == "not_probed" and not requests
            assert health.request_duration_seconds is None
            assert (
                health.backend_available
                is health.backend_reachable
                is health.model_available
                is None
            )
            observed = await diagnostics.probe(worker.id)
            assert observed.generation_success and observed.backend_reachable
            assert observed.configuration.endpoint_class == "unknown"
            assert (
                observed.model_available is None
            )  # Generation is not /models discovery.
            assert observed.token_usage.output_tokens == 2
            assert (
                observed.generation_timing
                is observed.tokens_per_second
                is observed.ttft_seconds
                is None
            )
            serialized = diagnostics.get(worker.id).model_dump_json()
            for private in (
                PRIVATE,
                "TEST_DIAGNOSTIC_TOKEN",
                "model.example.com",
                "private/v1",
                "backend-alias",
            ):
                assert private not in serialized
        finally:
            await provider.aclose()
        assert provider._http.is_closed and not provider._active

    asyncio.run(run())
    with database[0].connect() as connection:
        raw = str(connection.execute(select(WorkerDiagnosticRecord.latest)).all())
        assert PRIVATE not in raw and "private/v1" not in raw


@pytest.mark.parametrize(
    "usage,timing,throughput",
    [
        (None, None, None),
        (TokenUsage(output_tokens=10**400), GenerationTiming(output_seconds=1.0), None),
        (TokenUsage(output_tokens=100), GenerationTiming(output_seconds=1e-308), None),
        (TokenUsage(input_tokens=5), GenerationTiming(output_seconds=2.0), None),
        (TokenUsage(output_tokens=12), None, None),
        (TokenUsage(output_tokens=12), GenerationTiming(output_seconds=0.0), None),
        (TokenUsage(output_tokens=0), GenerationTiming(output_seconds=2.0), 0.0),
        (
            TokenUsage(input_tokens=5, output_tokens=12, total_tokens=17),
            GenerationTiming(output_seconds=2.0, total_seconds=90.0),
            6.0,
        ),
    ],
)
def test_generation_metrics_use_only_normalized_observations(usage, timing, throughput):
    provider = ScriptedProvider(
        result=GenerationResult(
            content="anything",
            reasoning=PRIVATE,
            model="backend-alias",
            token_usage=usage,
            generation_timing=timing,
            usage={"completion_tokens": 999999},
            timing={"eval_duration": 1},
        )
    )
    observed = asyncio.run(service(provider=provider).probe("worker"))
    assert observed.status == "successful"
    assert observed.token_usage == usage and observed.generation_timing == timing
    assert observed.tokens_per_second == throughput
    assert observed.ttft_seconds is None
    assert observed.request_duration_seconds >= 0
    assert "ttft_seconds" in observed.unavailable_metrics
    assert ("tokens_per_second" in observed.unavailable_metrics) == (throughput is None)
    assert PRIVATE not in observed.model_dump_json()
    projected = observed.model_dump(mode="json")
    assert "usage" not in projected and "timing" not in projected
    target, request = provider.requests[0]
    assert target.options == {} and request.max_output_tokens == 32
    assert observed.configuration.model == "configured"  # Never backend alias.


@pytest.mark.parametrize(
    "error,code",
    [
        (BackendUnavailable(PRIVATE), "backend_unavailable"),
        (ProviderTimeout(PRIVATE), "timeout"),
        (TimeoutError(PRIVATE), "timeout"),
        (InvalidProviderResponse(PRIVATE), "invalid_response"),
        (ProviderRejected(PRIVATE), "rejected"),
        (ProviderError(PRIVATE), "diagnostic_failed"),
        (RuntimeError(PRIVATE), "diagnostic_failed"),
    ],
)
def test_safe_failure_projection(error, code):
    result = asyncio.run(
        service(provider=ScriptedProvider(error=error)).probe("worker")
    )
    assert result.status == "failed" and result.error_code == code
    assert result.generation_success is False and result.model_available is None
    assert result.token_usage is result.tokens_per_second is None
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize(
    "result",
    [
        "bad",
        GenerationResult(content="", model="model"),
        GenerationResult(content="x" * 16385, model="model"),
        GenerationResult(content="ok", model="model", reasoning="x" * 16385),
    ],
)
def test_malformed_or_excessive_generation(result):
    observed = asyncio.run(
        service(provider=ScriptedProvider(result=result)).probe("worker")
    )
    assert observed.status == "failed" and observed.error_code == "invalid_response"


@pytest.mark.parametrize(
    "calls,success",
    [
        (
            [
                ToolCall(
                    id="call", name="diagnostic_echo", arguments={"value": "agentforge"}
                )
            ],
            True,
        ),
        ([], False),
        ([ToolCall(id="call", name="shell", arguments={"value": "agentforge"})], False),
        (
            [ToolCall(id="call", name="diagnostic_echo", arguments={"value": PRIVATE})],
            False,
        ),
        (
            [
                ToolCall(
                    id="call",
                    name="diagnostic_echo",
                    arguments={"value": "agentforge", "extra": True},
                )
            ],
            False,
        ),
        (
            [
                ToolCall(
                    id="one", name="diagnostic_echo", arguments={"value": "agentforge"}
                ),
                ToolCall(
                    id="two", name="diagnostic_echo", arguments={"value": "agentforge"}
                ),
            ],
            False,
        ),
    ],
)
def test_native_tool_probe_strictly_validates_without_dispatch(calls, success):
    provider = ScriptedProvider(
        result=GenerationResult(content="", model="model", tool_calls=calls)
    )
    observed = asyncio.run(service(provider=provider).probe("worker", kind="tools"))
    assert observed.tool_call_success is success
    assert observed.status == ("successful" if success else "failed")
    assert observed.error_code == (None if success else "tool_call_failed")
    assert len(provider.requests) == 1
    target, request = provider.requests[0]
    assert request.max_output_tokens == 64
    assert len(request.tools) == 1 and request.tools[0].name == "diagnostic_echo"
    assert PRIVATE not in observed.model_dump_json()


@pytest.mark.parametrize("capability", [False, None])
def test_unsupported_tools_are_not_contacted(capability):
    config = configuration()
    config.workers[0] = config.workers[0].model_copy(
        update={"supports_tools": capability}
    )
    provider = ScriptedProvider()
    observed = asyncio.run(service(config, provider).probe("worker", kind="tools"))
    assert observed.status == "not_applicable" and observed.tool_call_success is None
    assert observed.request_duration_seconds is None and not provider.requests


def test_streaming_measures_first_visible_content_and_terminal_usage(monkeypatch):
    clock = iter([10.0, 11.25, 13.0])
    monkeypatch.setattr(
        "agentforge.workers.diagnostic_service.monotonic", lambda: next(clock)
    )
    provider = ScriptedProvider(
        chunks=[
            GenerationChunk(content="", model="m", reasoning=PRIVATE),
            GenerationChunk(
                content="", model="m", token_usage=TokenUsage(output_tokens=999)
            ),
            GenerationChunk(content="a", model="m"),
            GenerationChunk(content="b", model="m"),
            GenerationChunk(
                content="",
                model="m",
                done=True,
                token_usage=TokenUsage(output_tokens=6),
                generation_timing=GenerationTiming(output_seconds=2.0),
            ),
        ]
    )
    observed = asyncio.run(service(provider=provider).probe("worker", kind="streaming"))
    assert observed.streaming_success and observed.generation_success
    assert observed.ttft_seconds == 1.25 and observed.request_duration_seconds == 3.0
    assert observed.token_usage.output_tokens == 6 and observed.tokens_per_second == 3.0
    assert provider.stream_closed
    assert PRIVATE not in observed.model_dump_json()


@pytest.mark.parametrize(
    "chunks,code",
    [
        ([], "stream_failed"),
        ([GenerationChunk(content="ok", model="m")], "stream_failed"),
        (
            [GenerationChunk(content="", reasoning=PRIVATE, model="m", done=True)],
            "stream_failed",
        ),
        (
            [
                GenerationChunk(content="ok", model="m"),
                InvalidProviderResponse(PRIVATE),
            ],
            "invalid_response",
        ),
        ([GenerationChunk(content="x" * 16385, model="m")], "invalid_response"),
        ([GenerationChunk(content="", model="m")] * 257, "invalid_response"),
        (["malformed"], "invalid_response"),
    ],
)
def test_stream_failures_and_output_bounds_close_stream(chunks, code):
    provider = ScriptedProvider(chunks=chunks)
    observed = asyncio.run(service(provider=provider).probe("worker", kind="streaming"))
    assert observed.status == "failed" and observed.error_code == code
    assert observed.streaming_success is observed.generation_success is False
    assert provider.stream_closed and observed.tokens_per_second is None


def test_streaming_skipped_when_not_configured():
    config = configuration()
    config.workers[0] = config.workers[0].model_copy(
        update={"supports_streaming": False}
    )
    provider = ScriptedProvider()
    observed = asyncio.run(service(config, provider).probe("worker", kind="streaming"))
    assert observed.status == "not_applicable" and observed.streaming_success is None
    assert not provider.requests


@pytest.mark.parametrize("kind", ["generation", "streaming", "health"])
def test_timeout_cancellation_and_duplicate_actions(kind, database):
    async def run():
        entered = asyncio.Event()
        released = asyncio.Event()

        class Blocking(ScriptedProvider):
            async def generate(self, *_):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    released.set()

            async def health(self, *_):
                return await self.generate()

            @asynccontextmanager
            async def stream(self, *_):
                async def chunks():
                    await self.generate()
                    yield None

                try:
                    yield chunks()
                finally:
                    self.stream_closed = True

        config = configuration(timeout_seconds=0.01)
        provider = Blocking()
        diagnostics = service(config, provider, database)

        def operation():
            return (
                diagnostics.check("worker")
                if kind == "health"
                else diagnostics.probe("worker", kind=kind)
            )

        first = asyncio.create_task(operation())
        await entered.wait()
        with pytest.raises(DiagnosticBusy):
            await operation()
        observed = await first
        assert observed.error_code == "timeout" and released.is_set()
        assert (
            diagnostics.get("worker").__getattribute__(kind).last_failure_at
            == observed.checked_at
        )
        # External cancellation is propagated; no completion checkpoint is invented.
        entered.clear()
        released.clear()
        config.workers[0] = config.workers[0].model_copy(update={"timeout_seconds": 30})
        diagnostics = service(config, provider, database)
        running = asyncio.create_task(operation())
        await entered.wait()
        await diagnostics.close()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert released.is_set() and not diagnostics._active
        with pytest.raises(DiagnosticsUnavailable):
            await operation()
        if kind == "streaming":
            assert provider.stream_closed

    asyncio.run(run())


def test_checkpoint_restart_latest_success_failure_invalidation_and_bounded_upsert(
    database,
):
    provider = ScriptedProvider()
    diagnostics = service(provider=provider, database=database)

    async def run():
        success = await diagnostics.probe("worker")
        provider.error = ProviderTimeout(PRIVATE)
        failure = await diagnostics.probe("worker")
        for _ in range(8):
            await diagnostics.probe("worker")
        return success, failure

    success, failure = asyncio.run(run())
    restarted = service(provider=ScriptedProvider(), database=database)
    history = restarted.get("worker").generation
    assert history.last_success_at == success.checked_at
    assert history.last_failure_at >= failure.checked_at
    assert history.latest.status == "failed"
    assert history.last_success_at.tzinfo == UTC
    # Late writers cannot replace a newer completion.
    repository = DiagnosticsRepository(create_session_factory(database[0]))
    repository.save(success, restarted._fingerprints["worker"])
    assert restarted.get("worker").generation == history
    config = configuration()
    config.workers[0] = config.workers[0].model_copy(
        update={"endpoint": configuration().workers[0].endpoint, "model": "replacement"}
    )
    changed = service(config, ScriptedProvider(), database)
    assert changed.get("worker").generation.previous_configuration
    assert changed.get("worker").generation.latest is None
    with database[0].connect() as connection:
        stored = connection.execute(select(WorkerDiagnosticRecord)).one()
        assert stored.configuration_fingerprint == restarted._fingerprints["worker"]
        assert (
            stored.latest["checked_at"]
            == history.latest.model_dump(mode="json")["checked_at"]
        )
    fresh = asyncio.run(changed.probe("worker"))
    assert changed.get("worker").generation.last_failure_at is None
    assert changed.get("worker").generation.last_success_at == fresh.checked_at
    # A late completion for the old configuration cannot displace its replacement.
    replacement = changed.get("worker").generation
    repository.save(failure, restarted._fingerprints["worker"])
    assert changed.get("worker").generation == replacement
    assert restarted.get("worker").generation.previous_configuration
    assert restarted.get("worker").generation.latest is None
    config.workers[0] = config.workers[0].model_copy(update={"id": "new-id"})
    asyncio.run(service(config, ScriptedProvider(), database).probe("new-id"))
    with database[0].connect() as connection:
        assert (
            connection.scalar(select(func.count()).select_from(WorkerDiagnosticRecord))
            == 2
        )
        assert set(connection.scalars(select(WorkerDiagnosticRecord.worker_id))) == {
            "worker",
            "new-id",
        }


def test_narrow_configuration_preserves_unrelated_checkpoints_and_probe_kinds(database):
    config = configuration()
    worker = config.workers[0]
    config.workers = [
        worker.model_copy(update={"id": identity})
        for identity in ("worker-a", "worker-b")
    ]
    full = service(config, ScriptedProvider(), database)
    asyncio.run(full.probe("worker-a"))
    asyncio.run(full.check("worker-a"))
    asyncio.run(full.probe("worker-b"))
    asyncio.run(full.check("worker-b"))
    saved_b = full.get("worker-b")
    saved_a_health = full.get("worker-a").health

    def snapshot():
        with database[0].connect() as connection:
            return {
                (row.worker_id, row.probe_kind): dict(row._mapping)
                for row in connection.execute(select(WorkerDiagnosticRecord))
            }

    before = snapshot()
    narrow = service(
        config.model_copy(update={"workers": [config.workers[0]]}),
        ScriptedProvider(),
        database,
    )
    assert [item.configuration.worker_id for item in narrow.list().workers] == [
        "worker-a"
    ]
    with pytest.raises(DiagnosticWorkerNotFound):
        narrow.get("worker-b")
    assert narrow.get("worker-a").health == saved_a_health
    assert snapshot() == before  # Passive reads never mutate even dormant rows.

    for _ in range(3):
        asyncio.run(narrow.probe("worker-a"))
    after = snapshot()
    assert set(after) == set(before)  # One checkpoint per Worker/probe kind.
    for key in before:
        if key != ("worker-a", "generation"):
            assert after[key] == before[key]
    assert (
        after["worker-a", "generation"]["checked_at"]
        > before["worker-a", "generation"]["checked_at"]
    )
    assert len(narrow.list().workers) == 1
    assert service(config, ScriptedProvider(), database).get("worker-b") == saved_b
    assert snapshot() == after


def test_cli_json_health_probe_passive_read_and_exit_codes(
    database, tmp_path, monkeypatch, capsys
):
    config = tmp_path / "workers.toml"
    config.write_text(
        '[[workers]]\nid="worker"\nprovider="ollama"\nendpoint="http://localhost"\nmodel="configured"\nsupports_tools=true\n'
    )
    url = str(database[1])
    provider = ScriptedProvider()
    monkeypatch.setattr(
        "agentforge.cli.create_providers", lambda _: {"ollama": provider}
    )

    def command(operation, *arguments, expected=0):
        assert (
            main(["worker", operation, *arguments, "--workers", str(config)])
            == expected
        )
        output = capsys.readouterr()
        assert PRIVATE not in output.out + output.err
        return json.loads(output.out) if output.out else output.err

    command("list")
    command("config-check")
    assert not provider.requests and provider.health_calls == 0
    command("check", "worker", "--database-url", url)
    assert provider.health_calls == 1 and provider.closed
    observed = command("probe", "worker", "--database-url", url)["observation"]
    assert observed["probe_kind"] == "generation" and observed["status"] == "successful"
    assert observed["token_usage"] is observed["tokens_per_second"] is None
    provider.error = RuntimeError(PRIVATE)
    command("probe", "worker", "--database-url", url, expected=7)
    count = len(provider.requests)
    last = command("diagnostics", "worker", "--database-url", url)
    assert last["generation"]["latest"]["error_code"] == "diagnostic_failed"
    assert (
        last["generation"]["last_success_at"] and last["generation"]["last_failure_at"]
    )
    command("diagnostics", "--all", "--database-url", url)
    assert len(provider.requests) == count
    command("diagnostics", "--database-url", url, expected=2)
    command("diagnostics", "worker", "--all", "--database-url", url, expected=2)
    command("probe", "missing", "--database-url", url, expected=5)
    command("probe", "worker", expected=2)
    command("probe", "worker", "--database-url", url, "--prompt", PRIVATE, expected=2)
    command("probe", "worker", "--database-url", url, "--kind", "shell", expected=2)
    command(
        "probe",
        "worker",
        "--database-url",
        "sqlite:///" + str(tmp_path / "absent.db"),
        expected=4,
    )


def test_dashboard_separates_partial_observations_and_never_probes(database):
    async def run():
        provider = ScriptedProvider(
            result=GenerationResult(
                content=PRIVATE, model="alias", token_usage=TokenUsage(output_tokens=0)
            )
        )
        app = Application(
            create_database_engine(database[1]),
            configuration(),
            providers={"ollama": provider},
        )
        await app.worker_diagnostics.probe("worker")
        requests = len(provider.requests)
        web = create_app(lambda: app)
        async with web.router.lifespan_context(web):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=web), base_url="http://localhost"
            ) as client:
                body = (await client.get("/workers")).text
                assert "Configuration" in body and "Last observed diagnostics" in body
                assert "32768" in body and "successful" in body
                assert "not checked" in body and "unknown" in body and "—" in body
                assert "— / 0" in body
                assert (
                    PRIVATE not in body
                    and "/private" not in body
                    and "backend-alias" not in body
                )
                assert (await client.post("/workers/probe")).status_code in {403, 404}
        assert len(provider.requests) == requests and provider.health_calls == 0
        with database[0].connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM tasks").scalar() == 0
            )

    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("kind", ["generation", "tools", "streaming"])
def test_real_adapters_run_small_diagnostics(protocol, kind):
    connection = ProviderConnection(
        id="named", type=protocol, base_url="http://localhost/v1"
    )
    worker = Worker(
        id="worker",
        provider=protocol,
        provider_connection="named",
        model="configured",
        supports_tools=True,
        supports_streaming=True,
        options={"stop": PRIVATE},
    )
    config = WorkersConfig(providers=[connection], workers=[worker])
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        assert PRIVATE not in request.content.decode()
        assert body["model"] == "configured" and body["stream"] == (kind == "streaming")
        call = {
            "function": {
                "name": "diagnostic_echo",
                "arguments": {"value": "agentforge"},
            }
        }
        if protocol == "ollama":
            assert body["options"]["num_predict"] == (64 if kind == "tools" else 32)
            result = {
                "model": "configured",
                "message": {"content": "" if kind == "tools" else "agentforge"},
                "done": True,
                "prompt_eval_count": 5,
                "eval_count": 2,
                "eval_duration": 500_000_000,
            }
            if kind == "tools":
                result["message"]["tool_calls"] = [call]
            return httpx.Response(200, content=json.dumps(result) + "\n")
        assert body["max_tokens"] == (64 if kind == "tools" else 32)
        if kind == "tools":
            call.update(id="native-id", type="function")
            call["function"]["arguments"] = json.dumps(call["function"]["arguments"])
        if kind == "streaming":
            frames = [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "agentforge"},
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
            ]
            return httpx.Response(
                200,
                content="".join(
                    "data: " + json.dumps(frame) + "\n\n" for frame in frames
                )
                + "data: [DONE]\n\n",
            )
        message = {
            "role": "assistant",
            "content": "" if kind == "tools" else "agentforge",
        }
        if kind == "tools":
            message["tool_calls"] = [call]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls" if kind == "tools" else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    transport = httpx.MockTransport(respond)
    provider = (
        OllamaProvider(connection, transport=transport)
        if protocol == "ollama"
        else OpenAICompatibleProvider(connection, transport=transport)
    )

    async def run():
        try:
            observation = await WorkerDiagnosticsService(
                config, {"named": provider}
            ).probe("worker", kind=kind)
            assert observation.status == "successful" and len(seen) == 1
            assert observation.configuration.supports_streaming
            assert observation.streaming_success is (
                True if kind == "streaming" else None
            )
            assert observation.tool_call_success is (True if kind == "tools" else None)
            assert observation.token_usage.output_tokens == 2
            assert observation.tokens_per_second == (
                4.0 if protocol == "ollama" else None
            )
            assert (observation.ttft_seconds is not None) == (kind == "streaming")
        finally:
            if hasattr(provider, "aclose"):
                await provider.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["ollama", "openai_compatible"])
def test_real_stream_cancellation_closes_http_response(protocol):
    async def run():
        entered = asyncio.Event()
        closed = asyncio.Event()

        class HangingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                closed.set()

        connection = ProviderConnection(
            id="named", type=protocol, base_url="http://localhost"
        )
        config = WorkersConfig(
            providers=[connection],
            workers=[
                Worker(
                    id="worker",
                    provider=protocol,
                    provider_connection="named",
                    model="model",
                    supports_streaming=True,
                )
            ],
        )
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, stream=HangingStream())
        )
        provider = (
            OllamaProvider(connection, transport=transport)
            if protocol == "ollama"
            else OpenAICompatibleProvider(connection, transport=transport)
        )
        diagnostics = WorkerDiagnosticsService(config, {"named": provider})
        pending = asyncio.create_task(diagnostics.probe("worker", kind="streaming"))
        await asyncio.wait_for(entered.wait(), 1)
        await diagnostics.close()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert closed.is_set()
        if hasattr(provider, "aclose"):
            await provider.aclose()
            assert not provider._active and provider._http.is_closed

    asyncio.run(run())
