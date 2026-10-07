"""Offline HTTP and real ASGI-stream tests, migrated SQLite and scripted Providers."""

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, update

from agentforge.agents.models import ExecutionResult, TraceEvent
from agentforge.application.service import Application
from agentforge.core.inference import (
    GenerationResult,
    GenerationTiming,
    TokenUsage,
    ToolCall,
)
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.models import IndexStateRecord, TaskRecord
from agentforge.db.tasks import TaskRepository
from agentforge.tasks.models import TaskStorageError
from agentforge.web.app import create_app
from agentforge.web.server import main
from agentforge.workers.config import WorkersConfig
from tests.test_mcp import PRIVATE, run
from tests.test_mcp import setup as setup_fixture

setup = setup_fixture


@asynccontextmanager
async def client_for(app, **options):
    web = create_app(lambda: app, **options)
    async with web.router.lifespan_context(web):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web), base_url="http://127.0.0.1"
        ) as client:
            yield client, web


def stored(
    setup, state="queued", *, request="Task text", trace=(), error="provider_error"
):
    repository = TaskRepository(create_session_factory(setup.app.database))
    binding = setup.binding | {"project_id": setup.project.id}
    identity = repository.add(
        project_id=binding["project_id"],
        agent_id=binding["agent_id"],
        worker_id=binding["worker_id"],
        request=request,
        provider="fake",
        model="history-model",
    ).task_id
    if state == "queued":
        return identity
    if state == "cancelled":
        repository.cancel(identity)
        return identity
    repository.claim(identity)
    if state == "running":
        return identity
    if state == "failed":
        repository.finish(identity, error_code=error)
    else:
        repository.finish(
            identity,
            result=ExecutionResult(
                project_id=setup.project.id,
                agent_id=binding["agent_id"],
                worker_id=binding["worker_id"],
                state="completed",
                reason="completed",
                final_answer="Final <script>alert('answer')</script>",
                steps=1,
                tool_call_count=0,
                tool_output_bytes=0,
                usage=(),
                trace=trace,
            ),
        )
    return identity


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


class LiveConnection:
    """Actual ASGI streaming/disconnect without a socket or browser service."""

    def __init__(self, web, path):
        self.web, self.path = web, path
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.messages = []

    async def __aenter__(self):
        await self.incoming.put(
            {"type": "http.request", "body": b"", "more_body": False}
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 80),
        }

        async def send(message):
            self.messages.append(message)
            await self.outgoing.put(message)

        self.task = asyncio.create_task(self.web(scope, self.incoming.get, send))
        response = await asyncio.wait_for(self.outgoing.get(), 2)
        assert response["type"] == "http.response.start"
        assert response["status"] == 200
        return self

    async def expect(self, kind):
        while True:
            message = await asyncio.wait_for(self.outgoing.get(), 2)
            if f"event: {kind}\n".encode() in message.get("body", b""):
                return message["body"]

    async def __aexit__(self, *_):
        await self.incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(self.task, 2)


def test_empty_overview_and_lists(database):
    async def execute():
        app = Application(
            create_database_engine(database[1]), WorkersConfig(workers=[]), providers={}
        )
        async with client_for(app) as (client, _):
            for path, expected in (
                ("/", "No terminal telemetry recorded."),
                ("/workers", "No Workers configured."),
                ("/projects", "No Projects registered."),
                ("/tasks", "No Tasks in this view."),
            ):
                response = await client.get(path)
                assert response.status_code == 200
                assert expected in response.text
                assert response.headers["cache-control"] == "no-store"
                assert (
                    "frame-ancestors 'none'"
                    in response.headers["content-security-policy"]
                )

    run(execute())


@pytest.mark.parametrize("count", [1, 4])
def test_worker_configuration_without_health_or_secrets(setup, count):
    async def execute():
        await setup.app.close()
        app = setup.build()
        if count == 1:
            app = Application(
                create_database_engine(setup.database[1]),
                WorkersConfig(workers=setup.workers.workers[:1]),
                providers={"fake": setup.provider},
            )
        async with client_for(app) as (client, _):
            overview = await client.get("/")
            assert f"<strong>{count}</strong>" in overview.text
            response = await client.get("/workers")
            assert response.status_code == 200
            assert response.text.count("configured / unknown") == count
            assert "32768" in response.text and "yes" in response.text
            for private in (
                PRIVATE,
                "private-path",
                "192.168.50.12",
                "api_key",
                "Recommended",
                "best Worker",
                ">online<",
            ):
                assert private not in response.text

    run(execute())


def test_worker_unknown_capabilities_and_escaping(setup):
    async def execute():
        await setup.app.close()
        worker = setup.workers.workers[0].model_copy(
            update={
                "model": "<script>MODEL</script>",
                "deployment_label": "<b>LAN</b>",
                "context_window": None,
                "supports_tools": None,
                "supports_streaming": False,
            }
        )
        app = Application(
            create_database_engine(setup.database[1]),
            WorkersConfig(workers=[worker]),
            providers={"fake": setup.provider},
        )
        async with client_for(app) as (client, _):
            body = (await client.get("/workers")).text
            assert "&lt;script&gt;MODEL&lt;/script&gt;" in body
            assert "&lt;b&gt;LAN&lt;/b&gt;" in body
            assert "<td>unknown</td>" in body and "<td>no</td>" in body
            assert "<td>—</td>" in body

    run(execute())


def test_projects_cached_metadata_no_io_and_escaping(setup, monkeypatch):
    async def execute():
        other = setup.registry.register_project(
            "<script>PROJECT</script>", setup.root.parent
        )
        now = datetime.now(UTC)
        with create_session_factory(setup.app.database)() as session:
            session.add(
                IndexStateRecord(
                    project_id=other.id, indexed_at=now, observed_head="a" * 40
                )
            )
            session.commit()

        def forbidden(*_args, **_kwargs):
            pytest.fail(
                "List rendering must not inspect source, Git, or index contents"
            )

        monkeypatch.setattr(setup.app.projects, "open_root", forbidden)
        monkeypatch.setattr(setup.app.index, "get_index_status", forbidden)
        # Registered roots can disappear; this page never claims live availability.
        (setup.root / "source.py").unlink()
        setup.root.rmdir()
        async with client_for(setup.app) as (client, _):
            response = await client.get("/projects")
            assert response.status_code == 200
            assert (
                "Registered" in response.text and str(setup.project.id) in response.text
            )
            assert "&lt;script&gt;PROJECT&lt;/script&gt;" in response.text
            assert "a" * 40 in response.text and "not probed / —" in response.text
            assert PRIVATE not in response.text

    run(execute())


def test_overview_counts_and_recent_bounds(setup):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, _):
            running = setup.app.delegate_task(**setup.binding)
            await setup.provider.entered.wait()
            identities = [
                stored(setup, state)
                for state in ["queued", "completed", "failed", "cancelled"] * 4
            ]
            response = await client.get("/")
            assert response.status_code == 200
            for state, count in [
                ("queued", 4),
                ("running", 1),
                ("completed", 4),
                ("failed", 4),
                ("cancelled", 4),
            ]:
                assert f"<span>{state}</span><strong>{count}</strong>" in response.text
            assert response.text.count('href="/tasks/') == 10
            assert str(identities[-1]) in response.text
            assert str(running.task_id) not in response.text
            setup.provider.release.set()

    run(execute())


def test_history_pagination_filters_order_and_compact_query(setup):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, _):
            setup.app.delegate_task(**setup.binding)
            await setup.provider.entered.wait()
            ids = [stored(setup, "completed") for _ in range(7)]
            with create_session_factory(setup.app.database)() as session:
                session.execute(
                    update(TaskRecord)
                    .where(TaskRecord.task_id.in_(ids))
                    .values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
                )
                session.commit()
            queries = []

            def capture(_connection, _cursor, statement, *_):
                queries.append(statement)

            event.listen(setup.app.database, "before_cursor_execute", capture)
            try:
                page = await client.get(
                    "/tasks", params={"state": "completed", "limit": 3}
                )
            finally:
                event.remove(setup.app.database, "before_cursor_execute", capture)
            assert page.status_code == 200
            positions = [
                page.text.index(str(identity))
                for identity in sorted(ids, reverse=True)[:3]
            ]
            assert positions == sorted(positions)
            assert page.text.count('href="/tasks/') == 3
            assert "Next page" in page.text
            assert len(queries) == 1
            assert (
                "execution_result" not in queries[0]
                and "tasks.request" not in queries[0]
            )
            fragment = await client.get(
                "/tasks?state=completed&limit=3&offset=3",
                headers={"HX-Request": "true"},
            )
            assert (
                "<!doctype" not in fragment.text and 'id="task-table"' in fragment.text
            )
            assert "Previous page" in fragment.text
            for field, value in [
                ("project_id", str(setup.project.id)),
                ("agent_id", "repo_explorer"),
                ("worker_id", "home-i5"),
            ]:
                body = (
                    await client.get(
                        "/tasks", params={field: value, "state": "completed"}
                    )
                ).text
                assert body.count('href="/tasks/') == 7
                empty = (await client.get("/tasks", params={field: str(uuid4())})).text
                assert "No Tasks in this view." in empty
            setup.provider.release.set()

    run(execute())


@pytest.mark.parametrize("path", ["/tasks", "/workers", "/projects"])
@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=no",
        "offset=-1",
        "offset=1000001",
        "limit=2&limit=3",
        "unexpected=PRIVATE",
    ],
)
def test_list_invalid_bounds_safe(setup, path, query):
    async def execute():
        async with client_for(setup.app) as (client, _):
            response = await client.get(f"{path}?{query}")
            assert response.status_code == 422
            assert "invalid_arguments" in response.text and PRIVATE not in response.text

    run(execute())


@pytest.mark.parametrize(
    "query",
    [
        "state=unknown",
        "project_id=invalid",
        "agent_id=" + "a" * 101,
        "worker_id=" + "a" * 101,
    ],
)
def test_history_invalid_filters(setup, query):
    async def execute():
        async with client_for(setup.app) as (client, _):
            assert (await client.get("/tasks?" + query)).status_code == 422

    run(execute())


@pytest.mark.parametrize(
    "state", ["queued", "running", "completed", "failed", "cancelled"]
)
def test_task_detail_states_and_privacy(setup, state):
    async def execute():
        trace = (
            TraceEvent(
                step=1,
                kind="tool_request",
                tool_name="read_file",
                tool_call_id=PRIVATE,
                arguments={PRIVATE: PRIVATE},
            ),
            TraceEvent(
                step=1,
                kind="tool_result",
                tool_name="read_file",
                success=False,
                error_code=PRIVATE,
            ),
        )
        async with client_for(setup.app) as (client, _):
            setup.provider.release.clear()
            active = setup.app.delegate_task(**setup.binding)
            await setup.provider.entered.wait()
            identity = (
                active.task_id
                if state == "running"
                else stored(setup, state, request="<script>TASK</script>", trace=trace)
            )
            response = await client.get(f"/tasks/{identity}")
            assert response.status_code == 200
            assert f'data-state="{state}"' in response.text
            assert "provider_error" in response.text if state == "failed" else True
            if state != "running":
                assert "&lt;script&gt;TASK&lt;/script&gt;" in response.text
            if state == "completed":
                assert (
                    "&lt;script&gt;alert(&#39;answer&#39;)&lt;/script&gt;"
                    in response.text
                )
                assert "internal_error" in response.text
                assert "No final answer." not in response.text
            else:
                assert "No final answer." in response.text
            assert PRIVATE not in response.text
            assert ('name="confirm"' in response.text) == (
                state in {"queued", "running"}
            )
            setup.provider.release.set()

    run(execute())


def test_telemetry_known_null_partial_and_no_reasoning(setup):
    async def execute():
        setup.provider.turns = [
            GenerationResult(
                content="",
                model="fake",
                reasoning=PRIVATE,
                usage={"secret": PRIVATE},
                timing={"body": PRIVATE},
                tool_calls=[
                    ToolCall(
                        id=PRIVATE, name="read_file", arguments={"path": "source.py"}
                    )
                ],
                token_usage=TokenUsage(input_tokens=20, output_tokens=10),
                generation_timing=GenerationTiming(output_seconds=2.0),
            ),
            GenerationResult(content="Public final", model="fake", reasoning=PRIVATE),
        ]
        async with client_for(setup.app) as (client, _):
            identity = setup.app.delegate_task(**setup.binding).task_id
            await setup.app.tasks.wait_task(identity)
            body = (await client.get(f"/tasks/{identity}")).text
            assert "Public final" in body and PRIVATE not in body
            assert "partial / incomplete" in body
            assert "20 / 1 of 2" in body and "10 / 1 of 2" in body
            assert "Prompt tokens</dt><dd>—</dd>" in body
            assert "Total tokens</dt><dd>—</dd>" in body
            assert "Observed throughput (tokens/s)</dt><dd>—</dd>" in body
            assert "TTFT (only explicitly observed)</dt><dd>—</dd>" in body
            assert "Tool attempts</dt><dd>1</dd>" in body
            assert "Model turns</dt><dd>2</dd>" in body
            assert "read_file" in body and "tool_result" in body

    run(execute())


def test_telemetry_complete_values_and_aggregate_coverage(setup):
    async def execute():
        setup.provider.turns = [
            GenerationResult(
                content="Result",
                model="fake",
                token_usage=TokenUsage(input_tokens=20, output_tokens=10),
                generation_timing=GenerationTiming(output_seconds=2.0),
            )
        ]
        async with client_for(setup.app) as (client, _):
            identity = setup.app.delegate_task(**setup.binding).task_id
            await setup.app.tasks.wait_task(identity)
            body = (await client.get(f"/tasks/{identity}")).text
            assert "Token accounting</dt><dd>complete</dd>" in body
            for label, value in [
                ("Prompt tokens", 20),
                ("Completion tokens", 10),
                ("Total tokens", 30),
                ("Observed throughput (tokens/s)", "5.0"),
            ]:
                assert f"{label}</dt><dd>{value}</dd>" in body
            assert "Model generation duration</dt><dd>2.000 s</dd>" in body
            overview = (await client.get("/")).text
            assert "1 / 1 Tasks" in overview and "5.0 tokens/s" in overview
            assert "may be partial" in overview

    run(execute())


def test_detail_deregistered_project_and_long_bounded_trace(setup):
    async def execute():
        async with client_for(setup.app) as (client, _):
            identity = stored(
                setup,
                "completed",
                trace=tuple(
                    TraceEvent(step=n, kind="model_request") for n in range(150)
                ),
            )
            setup.app.projects.remove_project(setup.project.id)
            body = (await client.get(f"/tasks/{identity}/fragment")).text
            assert str(setup.project.id) in body and "latest 100" in body
            assert body.count("<td>model_request</td>") == 100
            assert "Registered" not in body

    run(execute())


@pytest.mark.parametrize(
    "state", ["queued", "running", "completed", "failed", "cancelled"]
)
def test_cancellation_existing_semantics_and_confirmation(setup, state):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, _):
            active = setup.app.delegate_task(**setup.binding)
            await setup.provider.entered.wait()
            identity = active.task_id if state == "running" else stored(setup, state)
            # GET never mutates, including terminal Tasks.
            assert (await client.get(f"/tasks/{identity}/cancel")).status_code == 405
            assert setup.app.tasks.get_task(identity).state == state
            if state in {"queued", "running"}:
                page = await client.get(f"/tasks/{identity}")
                token = csrf(page)
            else:
                await client.get("/")
                token = client.cookies.get("agentforge_csrf")
            data = {"csrf": token, "confirm": "yes"}
            response = await client.post(
                f"/tasks/{identity}/cancel", data=data, headers={"HX-Request": "true"}
            )
            assert response.status_code == 200
            task = setup.app.tasks.get_task(identity)
            if state == "queued":
                assert task.state == "cancelled" and task.started_at is None
            elif state == "running":
                assert (
                    task.state == "running"
                    and task.cancellation_requested_at is not None
                )
                assert "cancellation requested" in response.text
            else:
                assert task.state == state
            setup.provider.release.set()
            ended = await setup.app.tasks.wait_task(active.task_id)
            assert ended.state == ("cancelled" if state == "running" else "completed")
            assert all(
                request.messages[-1].content != "Task text"
                for request in setup.provider.requests
            )

    run(execute())


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "wrong",
        "unsigned_cookie",
        "no_confirm",
        "origin",
        "fetch_site",
        "unicode",
        "duplicate",
        "big",
        "json",
    ],
)
def test_cancellation_csrf_rejection(setup, problem):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, _):
            active = setup.app.delegate_task(**setup.binding)
            await setup.provider.entered.wait()
            identity = stored(setup)
            token = csrf(await client.get(f"/tasks/{identity}"))
            data, headers = {"csrf": token, "confirm": "yes"}, {}
            content = None
            if problem == "missing":
                data.pop("csrf")
            if problem == "wrong":
                data["csrf"] = "wrong"
            if problem == "unsigned_cookie":
                client.cookies.clear()
                client.cookies.set("agentforge_csrf", "a" * 64 + "." + "b" * 64)
                data["csrf"] = client.cookies.get("agentforge_csrf")
            if problem == "no_confirm":
                data.pop("confirm")
            if problem == "origin":
                headers["Origin"] = "https://hostile.invalid"
            if problem == "fetch_site":
                headers["Sec-Fetch-Site"] = "cross-site"
            if problem == "unicode":
                data["csrf"] = "é" * 129
            if problem == "duplicate":
                content = f"csrf={token}&csrf={token}&confirm=yes"
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            if problem == "big":
                data["extra"] = "a" * 5000
            if problem == "json":
                content = "{}"
                headers["Content-Type"] = "application/json"
            response = await client.post(
                f"/tasks/{identity}/cancel",
                data=data if content is None else None,
                content=content,
                headers=headers,
            )
            assert response.status_code == 403
            assert setup.app.tasks.get_task(identity).state == "queued"
            setup.provider.release.set()
            await setup.app.tasks.wait_task(active.task_id)

    run(execute())


def test_post_redirect_missing_task_and_csrf_cookie_flags(setup):
    async def execute():
        async with client_for(setup.app) as (client, _):
            response = await client.get("/")
            cookie = response.headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=strict" in cookie
            token = client.cookies.get("agentforge_csrf")
            assert (
                await client.post(
                    f"/tasks/{uuid4()}/cancel", data={"csrf": token, "confirm": "yes"}
                )
            ).status_code == 404
            completed = stored(setup, "completed")
            response = await client.post(
                f"/tasks/{completed}/cancel", data={"csrf": token, "confirm": "yes"}
            )
            assert (
                response.status_code == 303
                and response.headers["location"] == f"/tasks/{completed}"
            )

    run(execute())


@pytest.mark.parametrize("suffix", ["", "/fragment", "/events"])
@pytest.mark.parametrize("identity,code", [("malformed", 422), (str(uuid4()), 404)])
def test_invalid_or_missing_task_ids(setup, suffix, identity, code):
    async def execute():
        async with client_for(setup.app) as (client, _):
            response = await client.get(f"/tasks/{identity}{suffix}")
            assert (
                response.status_code == code
                and "traceback" not in response.text.lower()
            )

    run(execute())


def test_safe_errors_and_absent_dangerous_routes(setup, monkeypatch, caplog):
    async def execute():
        async with client_for(setup.app) as (client, _):
            for path in [
                "/files/etc/passwd",
                "/shell",
                "/provider",
                "/docs",
                "/openapi.json",
            ]:
                assert (await client.get(path)).status_code == 404
            assert (
                await client.get("/", headers={"Host": "hostile.invalid"})
            ).status_code == 400

            def explode(*_args, **_kwargs):
                raise RuntimeError(PRIVATE)

            monkeypatch.setattr(setup.app, "status", explode)
            response = await client.get("/")
            assert response.status_code == 500
            assert "internal_error" in response.text and PRIVATE not in response.text
            assert PRIVATE not in caplog.text

            def storage(*_args, **_kwargs):
                raise TaskStorageError(PRIVATE)

            monkeypatch.setattr(setup.app.tasks, "history", storage)
            response = await client.get("/tasks", headers={"HX-Request": "true"})
            assert (
                response.status_code == 503 and "storage_unavailable" in response.text
            )
            assert response.headers["HX-Retarget"] == "#notice"
            assert PRIVATE not in response.text

    run(execute())


def test_sse_live_trace_terminal_multitab_and_disconnect(setup, monkeypatch):
    async def execute():
        original_generate = setup.provider.generate
        second_started = asyncio.Event()
        turns = 0

        async def generate(worker, request):
            nonlocal turns
            turns += 1
            if turns == 2:
                setup.provider.release.clear()
                second_started.set()
            return await original_generate(worker, request)

        monkeypatch.setattr(setup.provider, "generate", generate)
        setup.provider.release.clear()
        setup.provider.turns = [
            GenerationResult(
                content="",
                model="fake",
                reasoning=PRIVATE,
                tool_calls=[
                    ToolCall(
                        id=PRIVATE, name="read_file", arguments={"path": "source.py"}
                    )
                ],
            ),
            GenerationResult(content="Final", model="fake", reasoning=PRIVATE),
        ]
        async with client_for(setup.app) as (client, web):
            identity = setup.app.delegate_task(**setup.binding).task_id
            # Connect while queued before yielding to execution.
            with setup.app.tasks.observer.subscribe(identity) as direct:
                await setup.provider.entered.wait()
                assert "refresh" in [direct.get_nowait() for _ in range(direct.qsize())]
            path = f"/tasks/{identity}/events"
            async with (
                LiveConnection(web, path) as first,
                LiveConnection(web, path) as second,
            ):
                assert await first.expect("resync") == b"event: resync\ndata: {}\n\n"
                await second.expect("resync")
                assert setup.app.tasks.observer.subscriber_count == 2
                running = (await client.get(f"/tasks/{identity}/fragment")).text
                assert "model_request" in running
                assert PRIVATE not in running
                setup.provider.release.set()
                await second_started.wait()
                await first.expect("refresh")
                await second.expect("refresh")
                progress = (await client.get(f"/tasks/{identity}/fragment")).text
                assert 'data-state="running"' in progress
                assert "model_response" in progress and "tool_result" in progress
                assert "tool_request" in progress and PRIVATE not in progress
                setup.provider.release.set()
                await first.expect("terminal")
                await second.expect("terminal")
            assert setup.app.tasks.observer.subscriber_count == 0
            body = (await client.get(f"/tasks/{identity}/fragment")).text
            assert (
                "tool_request" in body
                and "tool_result" in body
                and "termination" in body
            )
            assert PRIVATE not in body
            wire = b"".join(m.get("body", b"") for m in first.messages)
            assert PRIVATE.encode() not in wire and b"arguments" not in wire
            # A disconnected observer leaves a different running Task untouched.
            setup.provider.release.clear()
            setup.provider.entered.clear()
            other = setup.app.delegate_task(**setup.binding).task_id
            await setup.provider.entered.wait()
            async with LiveConnection(web, f"/tasks/{other}/events") as stream:
                await stream.expect("resync")
            assert setup.app.tasks.observer.subscriber_count == 0
            assert setup.app.tasks.get_task(other).state == "running"
            setup.provider.release.set()
            assert (await setup.app.tasks.wait_task(other)).state == "completed"

    run(execute())


def test_sse_shutdown_terminal_reconnection_and_cleanup(setup):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, web):
            completed = stored(setup, "completed")
            response = await client.get(
                f"/tasks/{completed}/events", headers={"Last-Event-ID": "123"}
            )
            assert response.text == "event: terminal\ndata: {}\n\n"
            assert response.headers["content-type"].startswith("text/event-stream")
            identity = setup.app.delegate_task(**setup.binding).task_id
            await setup.provider.entered.wait()
            async with LiveConnection(web, f"/tasks/{identity}/events") as live:
                await live.expect("resync")
                await setup.app.close()
                await live.expect("shutdown")
            assert setup.app.tasks.observer.subscriber_count == 0
            assert setup.provider.cleaned == 1
            assert setup.app.tasks.get_task(identity).state == "failed"

    run(execute())


def test_web_lifecycle_single_owner_and_restart_recovery(setup, monkeypatch):
    async def execute():
        calls = {"start": 0, "close": 0}
        original_start, original_close = setup.app.tasks.start, setup.app.tasks.close

        async def start():
            calls["start"] += 1
            await original_start()

        async def close():
            calls["close"] += 1
            await original_close()

        monkeypatch.setattr(setup.app.tasks, "start", start)
        monkeypatch.setattr(setup.app.tasks, "close", close)
        stale = stored(setup, "running")
        async with client_for(setup.app) as (client, _):
            for path in ["/", "/tasks", "/projects", "/workers"] * 2:
                assert (await client.get(path)).status_code == 200
            recovered = setup.app.tasks.get_task(stale)
            assert (
                recovered.state == "failed"
                and recovered.reason == "execution_interrupted"
            )
            duplicate = setup.build()
            try:
                with pytest.raises(RuntimeError, match="already owns"):
                    await duplicate.start()
            finally:
                await duplicate.close()
        assert calls == {"start": 1, "close": 1}
        restarted = setup.build()
        async with client_for(restarted) as (client, _):
            body = (await client.get(f"/tasks/{stale}")).text
            assert (
                "execution_interrupted" in body
                and "Execution runtime</dt><dd>—</dd>" in body
            )

    run(execute())


def test_static_assets_offline_and_packaged(setup):
    async def execute():
        async with client_for(setup.app) as (client, _):
            for path in ["htmx.min.js", "dashboard.js", "dashboard.css"]:
                assert (await client.get("/static/" + path)).status_code == 200
            body = (await client.get("/")).text
            assert 'src="https://' not in body and 'href="https://' not in body
            assert "allowEval" in body and "false" in body

    run(execute())


@pytest.mark.parametrize(
    "host,warning",
    [
        (None, False),
        ("127.0.0.1", False),
        ("::1", False),
        ("192.168.1.2", True),
        ("0.0.0.0", True),
    ],
)
def test_cli_safe_defaults_and_lan_warning(monkeypatch, caplog, host, warning):
    captured = {}

    def serve(web, **kwargs):
        captured.update(kwargs)
        assert web is not None

    monkeypatch.setattr("agentforge.web.server.uvicorn.run", serve)
    monkeypatch.setattr("agentforge.web.server.logging.basicConfig", lambda **_: None)
    args = ["web", "--database-url", "sqlite:///unused.db", "--workers", "unused.toml"]
    if host:
        args.extend(["--host", host])
    monkeypatch.setattr("sys.argv", args)
    assert main() == 0
    assert captured["host"] == (host or "127.0.0.1")
    assert captured["port"] == 8765 and captured["workers"] == 1
    assert ("no authentication layer" in caplog.text) == warning


def test_worker_and_project_pagination_bounds(setup):
    async def execute():
        setup.registry.register_project("Another", setup.root.parent)
        async with client_for(setup.app) as (client, _):
            workers = await client.get("/workers?limit=1")
            assert workers.text.count("configured / unknown") == 1
            assert "Next page" in workers.text
            next_workers = await client.get("/workers?limit=1&offset=1")
            assert "Previous page" in next_workers.text
            assert next_workers.text.count("configured / unknown") == 1
            projects = await client.get("/projects?limit=1")
            assert projects.text.count("not probed / —") == 1
            assert "Next page" in projects.text
            next_projects = await client.get("/projects?limit=1&offset=1")
            assert "Previous page" in next_projects.text
            assert next_projects.text.count("not probed / —") == 1
            assert (await client.get("/static/../../README.md")).status_code == 404

    run(execute())


def test_sse_capacity_rejection_does_not_register(setup):
    from contextlib import ExitStack

    from agentforge.tasks.observation import SUBSCRIBER_LIMIT

    async def execute():
        async with client_for(setup.app) as (client, _):
            identity = stored(setup, "completed")
            with ExitStack() as stack:
                for _ in range(SUBSCRIBER_LIMIT):
                    stack.enter_context(setup.app.tasks.observer.subscribe(identity))
                response = await client.get(f"/tasks/{identity}/events")
                assert response.status_code == 503
                assert "service_unavailable" in response.text
                assert setup.app.tasks.observer.subscriber_count == SUBSCRIBER_LIMIT
            assert setup.app.tasks.observer.subscriber_count == 0

    run(execute())


def test_cancellation_missing_cookie_and_malformed_uuid(setup):
    async def execute():
        async with client_for(setup.app) as (client, _):
            identity = stored(setup, "completed")
            token = client.cookies.get("agentforge_csrf")
            client.cookies.clear()
            response = await client.post(
                f"/tasks/{identity}/cancel",
                data={"csrf": token or "", "confirm": "yes"},
            )
            assert response.status_code == 403
            assert (
                await client.post("/tasks/malformed/cancel", data={"confirm": "yes"})
            ).status_code == 422

    run(execute())


def test_secure_cookie_and_https_origin(setup):
    async def execute():
        async with client_for(setup.app) as (client, _):
            client.base_url = httpx.URL("https://127.0.0.1")
            response = await client.get("/")
            assert "Secure" in response.headers["set-cookie"]
            identity = stored(setup, "completed")
            response = await client.post(
                f"/tasks/{identity}/cancel",
                data={"csrf": client.cookies.get("agentforge_csrf"), "confirm": "yes"},
                headers={"Origin": "https://127.0.0.1"},
            )
            assert response.status_code == 303

    run(execute())


def test_long_task_text_and_result_are_visibly_bounded(setup):
    async def execute():
        async with client_for(setup.app) as (client, _):
            identity = stored(setup, "completed", request="r" * 40_000)
            with create_session_factory(setup.app.database)() as session:
                row = session.get(TaskRecord, identity)
                row.execution_result = row.execution_result | {
                    "final_answer": "a" * 80_000
                }
                session.commit()
            body = (await client.get(f"/tasks/{identity}")).text
            assert "r" * 32_768 in body and "r" * 32_769 not in body
            assert "a" * 65_536 in body and "a" * 65_537 not in body
            assert "truncated for display" in body

    run(execute())


def test_pre_iteration_disconnect_releases_sse_reservation():
    from agentforge.tasks.observation import TaskObserver
    from agentforge.web.app import EventResponse

    async def execute():
        observer = TaskObserver()
        subscription = observer.subscribe(uuid4())
        queue = subscription.__enter__()
        entered = False

        async def content():
            nonlocal entered
            entered = True
            yield f"event: {await queue.get()}\ndata: {{}}\n\n"

        response = EventResponse(
            content(), subscription, media_type="text/event-stream"
        )

        async def disconnected(_self, _scope, _receive, _send):
            raise asyncio.CancelledError

        # Simulate cancellation at the response layer before body iteration begins.
        from unittest.mock import patch

        with patch("starlette.responses.StreamingResponse.__call__", disconnected):
            with pytest.raises(asyncio.CancelledError):
                await response({}, None, None)
        assert not entered and observer.subscriber_count == 0

    run(execute())


def test_htmx_history_restore_returns_full_page(setup):
    async def execute():
        async with client_for(setup.app) as (client, _):
            response = await client.get(
                "/tasks",
                headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
            )
            assert "<!doctype html>" in response.text
            assert 'hx-history="false"' in response.text

    run(execute())


def test_sse_contract_rejects_unknown_internal_notice(setup):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (_, web):
            identity = setup.app.delegate_task(**setup.binding).task_id
            await setup.provider.entered.wait()
            async with LiveConnection(web, f"/tasks/{identity}/events") as live:
                await live.expect("resync")
                setup.app.tasks.observer.notify(identity, PRIVATE)
                body = await live.expect("resync")
                assert body == b"event: resync\ndata: {}\n\n"
            setup.provider.release.set()
            await setup.app.tasks.wait_task(identity)

    run(execute())
