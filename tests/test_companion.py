"""Offline Companion projections, safe HTTP observation and shared MCP lifecycle."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agentforge.agents.models import TraceEvent
from agentforge.application.companion import progress_label
from agentforge.mcp.server import create_server
from agentforge.tasks.observation import QUEUE_LIMIT, TimelineEvent
from agentforge.web.app import create_app
from agentforge.web.combined import CompanionConfig
from tests.test_mcp import PRIVATE, run
from tests.test_mcp import setup as setup_fixture
from tests.test_web import LiveConnection, client_for, csrf, stored

setup = setup_fixture


def test_standalone_lan_companion_has_neutral_hosting_label(setup):
    async def execute():
        async with client_for(setup.app, allowed_hosts=("192.168.50.2",)) as (
            client,
            _,
        ):
            response = await client.get("/companion", headers={"Host": "192.168.50.2"})
            assert response.status_code == 200
            assert "LOOPBACK" not in response.text
            assert '<span class="local">OPERATOR</span>' in response.text

    run(execute())


def test_active_identity_elapsed_and_terminal_detail(setup):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, _):
            identity = setup.app.delegate_task(**setup.binding).task_id
            await setup.provider.entered.wait()
            body = (await client.get("/companion")).text
            for expected in (
                "Registered",
                "repo_explorer",
                "home-i5",
                "configured-model",
                "running",
                "Cancel Task",
                "Running model turn",
            ):
                assert expected in body
            assert (
                "No persisted metrics"
                in (await client.get(f"/companion/tasks/{identity}")).text
            )
            detail = setup.app.companion.detail(identity)
            assert (
                setup.app.companion.detail(
                    identity, now=detail.task.started_at + timedelta(seconds=23)
                ).elapsed_seconds
                == 23
            )
            assert detail.tool_calls == 0
            setup.provider.release.set()
            await setup.app.tasks.wait_task(identity)
            body = (await client.get(f"/companion/tasks/{identity}")).text
            assert "Completed" in body and "Evidence found." in body
            assert "partial / incomplete" in body and "Total tokens</dt><dd>—" in body
            assert "Cancel Task" not in body
            first = setup.app.companion.detail(identity).elapsed_seconds
            assert (
                setup.app.companion.detail(
                    identity, now=detail.task.started_at + timedelta(days=1)
                ).elapsed_seconds
                == first
            )

    run(execute())


def test_safe_live_resync_terminal_disconnect(setup):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, web):
            identity = setup.app.delegate_task(**setup.binding).task_id
            await setup.provider.entered.wait()
            observer = setup.app.tasks.observer
            path = f"/companion/tasks/{identity}"
            async with LiveConnection(web, path + "/events") as live:
                await live.expect("resync")
                observer.record(
                    identity,
                    TraceEvent(
                        step=2,
                        kind="tool_request",
                        tool_name="read_file",
                        arguments={"path": PRIVATE},
                        tool_call_id=PRIVATE,
                    ),
                )
                await live.expect("refresh")
                body = (await client.get(path + "/fragment")).text
                assert "Reading repository with read_file" in body
                assert "Tools 1" in body and "Step 2" in body
                assert PRIVATE not in body
                for _ in range(QUEUE_LIMIT + 2):
                    observer.notify(identity)
                await live.expect("resync")
                setup.provider.release.set()
                await setup.app.tasks.wait_task(identity)
                await live.expect("terminal")
            assert observer.subscriber_count == 0
            response = await client.get(path + "/events")
            assert response.text == "event: terminal\ndata: {}\n\n"
            setup.provider.release.clear()
            identity = setup.app.delegate_task(**setup.binding).task_id
            async with LiveConnection(
                web, f"/companion/tasks/{identity}/events"
            ) as live:
                await live.expect("resync")
            assert observer.subscriber_count == 0
            assert setup.app.get_task(task_id=identity).state in {"queued", "running"}

    run(execute())


@pytest.mark.parametrize("state", ["queued", "running"])
def test_cancel_csrf_and_cooperative_semantics(setup, state):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, _):
            if state == "queued":
                setup.app.delegate_task(**setup.binding)
                await setup.provider.entered.wait()
            identity = (
                stored(setup)
                if state == "queued"
                else setup.app.delegate_task(**setup.binding).task_id
            )
            if state == "running":
                await setup.provider.entered.wait()
            path = f"/companion/tasks/{identity}"
            page = await client.get(path)
            assert (await client.get(path + "/cancel")).status_code == 405
            assert (
                await client.post(path + "/cancel", data={"confirm": "yes"})
            ).status_code == 403
            data = {"csrf": csrf(page), "confirm": "yes"}
            assert (
                await client.post(
                    path + "/cancel",
                    data=data,
                    headers={"origin": "https://evil.invalid"},
                )
            ).status_code == 403
            assert (await client.post(path + "/cancel", data=data)).status_code == 303
            task = setup.app.get_task(task_id=identity)
            assert task.state == ("cancelled" if state == "queued" else "running")
            body = (await client.get(path)).text
            assert "Cancellation requested" in body
            assert "cooperative" in body and "Remote inference may continue" in body

    run(execute())


def test_escaping_security_and_bounded_queries(setup):
    async def execute():
        async with client_for(setup.app) as (client, _):
            identity = stored(setup, "completed")
            for path in ("/companion", f"/companion/tasks/{identity}"):
                response = await client.get(path)
                assert response.status_code == 200
                assert (
                    "frame-ancestors 'none'"
                    in response.headers["content-security-policy"]
                )
                for private in (
                    PRIVATE,
                    str(setup.root),
                    "192.168.50.12",
                    "inference.example",
                    "api_key",
                ):
                    assert private not in response.text
                assert (
                    await client.get(path, headers={"host": "evil.invalid"})
                ).status_code == 400
            assert (
                "&lt;script&gt;"
                in (await client.get(f"/companion/tasks/{identity}")).text
            )
            assert (await client.get("/companion?limit=101")).status_code == 422
            assert (await client.get("/companion?limit=1&limit=2")).status_code == 422
            for asset in ("companion.css", "companion.js"):
                assert (await client.get("/static/" + asset)).status_code == 200

    run(execute())


def test_mcp_and_http_share_one_owner(setup, monkeypatch):
    async def execute():
        started, closed = (
            AsyncMock(wraps=setup.app.start),
            AsyncMock(wraps=setup.app.close),
        )
        monkeypatch.setattr(setup.app, "start", started)
        monkeypatch.setattr(setup.app, "close", closed)
        borrowed = []

        @asynccontextmanager
        async def http(app, config):
            assert app is setup.app and config == CompanionConfig()
            web = create_app(shared_application=app)
            async with web.router.lifespan_context(web):
                borrowed.append(web)
                yield web
            assert closed.await_count == 1

        monkeypatch.setattr("agentforge.mcp.server.companion_http", http)
        factory_calls = []

        def factory():
            factory_calls.append(True)
            return setup.app

        server = create_server(factory, companion=CompanionConfig())
        setup.provider.release.clear()
        async with create_connected_server_and_client_session(server) as mcp:
            result = await mcp.call_tool("delegate_task", setup.binding)
            from uuid import UUID

            identity = UUID(result.structuredContent["task_id"])
            await setup.provider.entered.wait()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=borrowed[0]),
                base_url="http://127.0.0.1",
            ) as client:
                assert str(identity) in (await client.get("/companion")).text
                async with LiveConnection(
                    borrowed[0], f"/companion/tasks/{identity}/events"
                ) as live:
                    await live.expect("resync")
                    assert (
                        await mcp.call_tool("watch_task", {"task_id": str(identity)})
                    ).structuredContent["state"] == "running"
                    duplicate = setup.build()
                    try:
                        with pytest.raises(RuntimeError, match="already owns"):
                            await duplicate.start()
                    finally:
                        await duplicate.close()
                    await mcp.call_tool("cancel_task", {"task_id": str(identity)})
                    await live.expect("refresh")
        assert len(factory_calls) == started.await_count == closed.await_count == 1
        assert setup.app.tasks.observer.subscriber_count == 0
        assert not setup.app.tasks.available

    run(execute())


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "localhost", "192.168.1.1", "*"])
def test_combined_requires_loopback(host):
    with pytest.raises(ValueError):
        CompanionConfig(host=host)


def test_loopback_defaults_and_borrowed_lifecycle(setup):
    from agentforge.cli import parser

    args = parser().parse_args(
        [
            "mcp",
            "--database-url",
            "sqlite:///unused",
            "--workers",
            "unused",
            "--companion",
        ]
    )
    assert args.companion_host == "127.0.0.1" and args.companion_port == 8765
    assert CompanionConfig(host="::1").host == "::1"
    with pytest.raises(ValueError):
        create_app(lambda: setup.app, shared_application=setup.app)

    async def execute():
        web = create_app(shared_application=setup.app)
        with pytest.raises(Exception, match="service_unavailable"):
            async with web.router.lifespan_context(web):
                pass
        await setup.app.close()

    run(execute())


@pytest.mark.parametrize(
    "kind,success,label",
    [
        ("validation_started", None, "Running validation"),
        ("validation_completed", True, "Validation passed"),
        ("validation_completed", False, "Validation failed"),
        ("workspace_provisioned", True, "Coding workspace created"),
    ],
)
def test_factual_progress_labels(kind, success, label):
    assert (
        progress_label(TimelineEvent(1, kind, None, success, None, None, None)) == label
    )


def test_real_combined_stdio_http_shutdown(database, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    workers = tmp_path / "workers.toml"
    workers.write_text(
        '[[workers]]\nid="offline"\nprovider="ollama"\nmodel="configured"\nendpoint="http://192.0.2.1:11434"\n'
    )
    smoke = Path(__file__).resolve().parents[1] / "scripts/verify_companion.py"
    result = subprocess.run(
        [
            sys.executable,
            str(smoke),
            "--database-url",
            str(database[1]),
            "--workers",
            str(workers),
        ],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stderr
    assert "valid stdio, concurrent HTTP" in result.stdout


def test_council_participants_terminal_counts_and_links(setup):
    from agentforge.core.inference import GenerationResult
    from agentforge.core.provider_errors import ProviderTimeout
    from tests.test_councils import arguments, finish

    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, web):
            council = setup.app.delegate_council(
                **arguments(setup, workers=("ai395", "home-i5", "cloud"))
            )
            await setup.provider.entered.wait()
            path = f"/companion/councils/{council.council_id}"
            async with LiveConnection(web, path + "/events") as live:
                await live.expect("resync")
                body = (await client.get(path)).text
                for p in council.participants:
                    assert (
                        p.worker_id in body and f"/companion/tasks/{p.task_id}" in body
                    )
                assert "Registered" in body and "repo_explorer" in body
                assert "configured-model" in body and "active" in body
                setup.app.cancel_task(task_id=council.participants[-1].task_id)
                setup.provider.turns = [
                    GenerationResult(content="Answer", model="fake"),
                    ProviderTimeout(PRIVATE),
                ]
                setup.provider.release.set()
                await finish(setup.app, council)
                await live.expect("terminal")
            body = (await client.get(path + "/fragment")).text
            assert "1 completed · 1 failed · 1 cancelled" in body
            assert "terminal" in body and PRIVATE not in body
            assert "external Director" in body
            assert setup.app.tasks.observer.subscriber_count == 0

    run(execute())


def test_live_truncation_does_not_invent_tool_total(setup):
    from agentforge.tasks.observation import TRACE_LIMIT

    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, _):
            identity = setup.app.delegate_task(**setup.binding).task_id
            await setup.provider.entered.wait()
            for _ in range(TRACE_LIMIT + 1):
                setup.app.tasks.observer.record(
                    identity,
                    TraceEvent(step=2, kind="tool_request", tool_name="read_file"),
                )
            item = setup.app.companion.detail(identity)
            assert item.progress.truncated and item.tool_calls is None
            body = (await client.get(f"/companion/tasks/{identity}")).text
            assert "Latest 100 events" in body and "Tools —" in body

    run(execute())


def test_companion_shutdown_and_pre_iteration_cleanup(setup, monkeypatch):
    from starlette.responses import StreamingResponse

    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, web):
            identity = setup.app.delegate_task(**setup.binding).task_id
            await setup.provider.entered.wait()
            path = f"/companion/tasks/{identity}/events"
            async with LiveConnection(web, path) as live:
                await live.expect("resync")
                await setup.app.close()
                await live.expect("shutdown")
            assert setup.app.tasks.observer.subscriber_count == 0
        # Cancellation before body iteration must release watch_task reservation.
        app = setup.build()
        web = create_app(shared_application=app)
        await app.start()
        try:
            identity = app.delegate_task(**setup.binding).task_id
            route = next(
                r
                for r in web.routes
                if getattr(r, "path", "") == "/companion/tasks/{task_id}/events"
            )
            response = await route.endpoint(None, identity)
            assert app.tasks.observer.subscriber_count == 1

            async def disconnected(*_):
                raise asyncio.CancelledError

            monkeypatch.setattr(StreamingResponse, "__call__", disconnected)
            with pytest.raises(asyncio.CancelledError):
                await response({}, None, None)
            assert app.tasks.observer.subscriber_count == 0
        finally:
            await app.close()

    run(execute())


@pytest.mark.parametrize("complete", [True, False])
def test_observed_telemetry_complete_and_partial(setup, complete):
    from agentforge.core.inference import GenerationResult, GenerationTiming, TokenUsage

    async def execute():
        result = GenerationResult(
            content="Public answer",
            model="fake",
            token_usage=TokenUsage(input_tokens=20, output_tokens=10),
            generation_timing=GenerationTiming(output_seconds=2.0),
        )
        if not complete:
            from agentforge.core.inference import ToolCall

            result = result.model_copy(
                update={
                    "content": "",
                    "tool_calls": [
                        ToolCall(
                            id="read", name="read_file", arguments={"path": "source.py"}
                        )
                    ],
                }
            )
            setup.provider.turns = [
                result,
                GenerationResult(content="Public answer", model="fake"),
            ]
        else:
            setup.provider.turns = [result]
        async with client_for(setup.app) as (client, _):
            task = setup.app.delegate_task(**setup.binding)
            await setup.app.tasks.wait_task(task.task_id)
            body = (await client.get(f"/companion/tasks/{task.task_id}")).text
            assert "Observed prompt tokens</dt><dd>20" in body
            assert "Observed completion tokens</dt><dd>10" in body
            assert "TTFT</dt><dd>—" in body
            if complete:
                assert "Total tokens</dt><dd>30" in body
                assert "Tokens/s</dt><dd>5.0" in body
            else:
                assert "Total tokens</dt><dd>—" in body
                assert "Tokens/s</dt><dd>—" in body
                assert "partial / incomplete" in body
            assert PRIVATE not in body

    run(execute())


def test_companion_startup_failure_closes_owned_resources(setup, monkeypatch):
    async def execute():
        close = AsyncMock(wraps=setup.app.close)
        dispose = []
        original = setup.app.database.dispose

        def disposed():
            dispose.append(True)
            original()

        monkeypatch.setattr(setup.app.database, "dispose", disposed)
        monkeypatch.setattr(setup.app, "close", close)

        @asynccontextmanager
        async def unavailable(*_):
            raise OSError(PRIVATE)
            yield

        monkeypatch.setattr("agentforge.mcp.server.companion_http", unavailable)
        server = create_server(lambda: setup.app, companion=CompanionConfig())
        with pytest.raises(OSError):
            async with server.lifespan(server):
                pass
        assert close.await_count == 1 and len(dispose) == 1
        assert not setup.app.tasks.available

    run(execute())


def test_unknown_outcome_labels():
    for kind in ("tool_result", "validation_completed"):
        label = progress_label(TimelineEvent(1, kind, None, None, None, None, None))
        assert "unknown" in label and "failed" not in label


def test_htmx_cancel_redirect_preserves_strict_origin_boundary(setup):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, _):
            identity = setup.app.delegate_task(**setup.binding).task_id
            await setup.provider.entered.wait()
            path = f"/companion/tasks/{identity}"
            page = await client.get(path)
            assert f'hx-post="{path}/cancel"' in page.text
            fields = {"csrf": csrf(page), "confirm": "yes"}
            assert (
                await client.post(
                    path + "/cancel",
                    data=fields,
                    headers={"Origin": "null", "HX-Request": "true"},
                )
            ).status_code == 403
            response = await client.post(
                path + "/cancel",
                data=fields,
                headers={"Origin": "http://127.0.0.1", "HX-Request": "true"},
            )
            assert response.status_code == 200
            assert response.headers["HX-Redirect"] == path
            assert setup.app.get_task(task_id=identity).cancellation_requested_at

    run(execute())


def test_active_pages_use_per_state_counts(setup):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, _):
            running = setup.app.delegate_task(**setup.binding).task_id
            await setup.provider.entered.wait()
            queued = setup.app.delegate_task(**setup.binding).task_id
            response = await client.get("/companion?limit=1")
            assert str(running) in response.text and str(queued) in response.text
            assert "More active work" not in response.text
            second = setup.app.delegate_task(**setup.binding).task_id
            assert "More active work" in (await client.get("/companion?limit=1")).text
            response = await client.get("/companion?limit=1&offset=1")
            assert (
                "Previous" in response.text and "More active work" not in response.text
            )
            assert f'data-task-id="{running}"' not in response.text
            assert (
                sum(
                    f'data-task-id="{identity}"' in response.text
                    for identity in (queued, second)
                )
                == 1
            )

    run(execute())
