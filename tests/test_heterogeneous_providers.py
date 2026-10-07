"""Real adapters, runtime/storage/telemetry, MCP and HTML; all inference mocked."""

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import select

from agentforge.application.service import Application
from agentforge.db.database import create_session_factory
from agentforge.db.models import CouncilRecord, TaskRecord, TaskTelemetryRecord
from agentforge.mcp.server import create_server
from agentforge.providers.factory import PROVIDER_TYPES
from agentforge.providers.ollama import OllamaProvider
from agentforge.providers.openai_compatible import OpenAICompatibleProvider
from agentforge.workers.config import load_workers
from tests.test_mcp import call, run
from tests.test_openai_compatible import BytesStream, completion
from tests.test_web import client_for

SECRET = "PRIVATE_TEST_BEARER_VALUE"
REASONING = "PRIVATE_INTEGRATION_REASONING"


@pytest.fixture
def setup(database, registry, tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "source.py").write_text("def evidence():\n    pass\n")
    project = registry.register_project("Evidence", root)
    monkeypatch.setenv("AGENTFORGE_TEST_TOKEN", SECRET)
    path = tmp_path / "workers.toml"
    path.write_text("""[[providers]]
id = "local-connection"
type = "ollama"
base_url = "http://ollama.invalid:11434"

[[providers]]
id = "remote-connection"
type = "openai_compatible"
base_url = "https://gateway.invalid/v1"
api_key_env = "AGENTFORGE_TEST_TOKEN"

[[workers]]
id = "local"
provider = "local-connection"
model = "ollama-configured"
supports_tools = true
context_window = 32768

[[workers]]
id = "remote"
provider = "remote-connection"
model = "compatible-configured"
supports_tools = true
supports_streaming = true
context_window = 32768
""")
    requests = {"local": [], "remote": []}
    state = {"fail_remote": False, "block_remote": False}
    streams = []

    def handler(identity):
        def handle(req):
            payload = json.loads(req.content)
            requests[identity].append(payload)
            expected = (
                "ollama-configured" if identity == "local" else "compatible-configured"
            )
            assert payload["model"] == expected
            if identity == "remote":
                assert req.headers["authorization"] == "Bearer " + SECRET
                if state["fail_remote"]:
                    return httpx.Response(401, json={"error": {"message": SECRET}})
                if state["block_remote"]:
                    stream = BytesStream(
                        [json.dumps(completion()).encode()], block=True
                    )
                    streams.append(stream)
                    return httpx.Response(200, stream=stream)
                assert REASONING not in req.content.decode()
            messages = payload["messages"]
            if messages[-1]["role"] != "tool":
                function = {
                    "name": "read_file",
                    "arguments": {"path": "source.py", "start_line": 1, "end_line": 2},
                }
                if identity == "local":
                    return httpx.Response(
                        200,
                        json={
                            "model": "observation-only",
                            "done": True,
                            "message": {
                                "content": "",
                                "thinking": REASONING,
                                "tool_calls": [
                                    {"id": "local-call", "function": function}
                                ],
                            },
                            "prompt_eval_count": 10,
                            "eval_count": 3,
                            "eval_duration": 1000000000,
                        },
                    )
                function["arguments"] = json.dumps(function["arguments"])
                # Exercise a multiple-tool batch and exact Provider-issued IDs.
                return httpx.Response(
                    200,
                    json=completion(
                        content=None,
                        calls=[
                            {
                                "id": "remote-first",
                                "type": "function",
                                "function": function,
                            },
                            {
                                "id": "remote-second",
                                "type": "function",
                                "function": function,
                            },
                        ],
                        reasoning_content=REASONING,
                        usage={
                            "prompt_tokens": 10,
                            "completion_tokens": 3,
                            "total_tokens": 13,
                        },
                    ),
                )
            tool_messages = [m for m in messages if m["role"] == "tool"]
            for message in tool_messages:
                body = json.loads(message["content"])
                assert (
                    body["ok"]
                    and body["result"]["content"] == "def evidence():\n    pass\n"
                )
            if identity == "local":
                assert messages[-1]["tool_name"] == "read_file"
                assert messages[-2]["thinking"] == REASONING
                return httpx.Response(
                    200,
                    json={
                        "model": "observation-only",
                        "done": True,
                        "done_reason": "stop",
                        "message": {
                            "content": "Evidence source.py:1-2.",
                            "thinking": REASONING,
                        },
                        "prompt_eval_count": 20,
                        "eval_count": 4,
                        "eval_duration": 1000000000,
                    },
                )
            assert [m["tool_call_id"] for m in tool_messages] == [
                "remote-first",
                "remote-second",
            ]
            assert [c["id"] for c in messages[-3]["tool_calls"]] == [
                "remote-first",
                "remote-second",
            ]
            return httpx.Response(
                200,
                json=completion(
                    content="Evidence source.py:1-2.",
                    reasoning_content=REASONING,
                    usage={
                        "prompt_tokens": 20,
                        "completion_tokens": 4,
                        "total_tokens": 24,
                    },
                ),
            )

        return handle

    monkeypatch.setitem(
        PROVIDER_TYPES,
        "ollama",
        lambda c: OllamaProvider(c, transport=httpx.MockTransport(handler("local"))),
    )
    monkeypatch.setitem(
        PROVIDER_TYPES,
        "openai_compatible",
        lambda c: OpenAICompatibleProvider(
            c, transport=httpx.MockTransport(handler("remote"))
        ),
    )
    app = Application.from_config(
        database_url=str(database[1]), workers_path=str(path), concurrency=2
    )
    return app, project, requests, state, streams, path


def assert_private_absent(value):
    text = str(value)
    assert all(
        private not in text
        for private in (
            SECRET,
            REASONING,
            "gateway.invalid",
            "ollama.invalid",
            "AGENTFORGE_TEST_TOKEN",
        )
    )


def assert_task(app, task, provider, model, calls):
    assert task.state == "completed" and task.final_answer == "Evidence source.py:1-2."
    assert task.provider == provider and task.model == model
    assert task.execution_result.tool_call_count == calls
    metric = app.telemetry.get_for_task(task.task_id)
    assert metric.provider == provider and metric.model == model
    assert (
        metric.prompt_tokens == 30
        and metric.completion_tokens == 7
        and metric.total_tokens == 37
    )
    assert metric.model_call_count == 2 and metric.token_usage_complete
    assert metric.model_request_duration_seconds is not None
    assert metric.execution_duration_seconds is not None and metric.ttft_seconds is None
    if provider == "openai_compatible":
        assert (
            metric.generation_duration_seconds is None
            and metric.tokens_per_second is None
        )
    assert_private_absent(task.model_dump_json())
    assert_private_absent(metric.model_dump_json())


def assert_database_private_absent(app):
    with create_session_factory(app.database)() as session:
        for table in (TaskRecord, TaskTelemetryRecord, CouncilRecord):
            for row in session.scalars(select(table)):
                assert_private_absent(
                    {
                        column.name: getattr(row, column.name)
                        for column in table.__table__.columns
                    }
                )


@pytest.mark.parametrize(
    "worker_id,provider,model,calls",
    [
        ("local", "ollama", "ollama-configured", 1),
        ("remote", "openai_compatible", "compatible-configured", 2),
    ],
)
def test_same_repo_explorer_flow_task_engine_and_telemetry(
    setup, worker_id, provider, model, calls
):
    app, project, requests, _, _, _ = setup

    async def execute():
        await app.start()
        try:
            submitted = app.delegate_task(
                project_id=project.id,
                agent_id="repo_explorer",
                worker_id=worker_id,
                task="Read evidence",
            )
            task = await app.tasks.wait_task(submitted.task_id)
            assert_task(app, task, provider, model, calls)
            assert (
                len(requests[worker_id]) == 2
                and len(requests["remote" if worker_id == "local" else "local"]) == 0
            )
            assert_database_private_absent(app)
        finally:
            await app.close()
        remote = app._owned_providers["remote-connection"]
        assert remote._closed and (remote._http is None or remote._http.is_closed)

    run(execute())


@pytest.mark.parametrize("fail_remote", [False, True])
def test_heterogeneous_council_independent_ordered_no_fallback(setup, fail_remote):
    app, project, requests, state, _, _ = setup
    state["fail_remote"] = fail_remote

    async def execute():
        await app.start()
        try:
            council = app.delegate_council(
                project_id=project.id,
                agent_id="repo_explorer",
                worker_ids=("remote", "local"),
                task="Read evidence",
            )
            tasks = [await app.tasks.wait_task(p.task_id) for p in council.participants]
            snapshot = app.get_council(council_id=council.council_id)
            assert snapshot.terminal
            assert [p.worker_id for p in snapshot.participants] == ["remote", "local"]
            assert [p.provider for p in snapshot.participants] == [
                "openai_compatible",
                "ollama",
            ]
            assert_task(app, tasks[1], "ollama", "ollama-configured", 1)
            if fail_remote:
                assert (
                    tasks[0].state == "failed"
                    and tasks[0].error_code == "provider_error"
                )
                assert tasks[0].final_answer is None
                assert (
                    app.telemetry.get_for_task(tasks[0].task_id).prompt_tokens is None
                )
            else:
                assert_task(
                    app, tasks[0], "openai_compatible", "compatible-configured", 2
                )
            assert len(requests["local"]) == 2 and len(requests["remote"]) == (
                1 if fail_remote else 2
            )
            assert_private_absent(snapshot.model_dump_json())
            assert_database_private_absent(app)
        finally:
            await app.close()

    run(execute())


def test_mcp_heterogeneous_discovery_task_and_council_worker_only(setup):
    app, project, _, _, _, _ = setup

    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: app)
        ) as client:
            workers = await call(client, "list_workers")
            assert [w["provider"] for w in workers["workers"]] == [
                "ollama",
                "openai_compatible",
            ]
            assert_private_absent(workers)
            common = {
                "project_id": str(project.id),
                "agent_id": "repo_explorer",
                "task": "Read evidence",
            }
            delegated = await call(
                client, "delegate_task", common | {"worker_id": "remote"}
            )
            await app.tasks.wait_task(UUID(delegated["task_id"]))
            done = await call(client, "get_task", {"task_id": delegated["task_id"]})
            assert done["state"] == "completed"
            council = await call(
                client, "delegate_council", common | {"worker_ids": ["local", "remote"]}
            )
            for participant in council["participants"]:
                await app.tasks.wait_task(UUID(participant["task_id"]))
            done_council = await call(
                client, "get_council", {"council_id": council["council_id"]}
            )
            assert done_council["participant_counts"]["completed"] == 2
            assert_private_absent([delegated, done, council, done_council])
            assert_database_private_absent(app)

    run(execute())


@pytest.mark.parametrize("fail_remote", [False, True])
def test_dashboard_workers_task_and_council_are_generic(setup, fail_remote):
    app, project, _, state, _, _ = setup
    state["fail_remote"] = fail_remote

    async def execute():
        async with client_for(app) as (client, _):
            page = (await client.get("/workers")).text
            assert (
                "local" in page
                and "remote" in page
                and "ollama" in page
                and "openai_compatible" in page
            )
            council = app.delegate_council(
                project_id=project.id,
                agent_id="repo_explorer",
                worker_ids=("remote", "local"),
                task="Read evidence",
            )
            for p in council.participants:
                await app.tasks.wait_task(p.task_id)
            pages = [page, (await client.get(f"/councils/{council.council_id}")).text]
            pages += [
                (await client.get(f"/tasks/{p.task_id}")).text
                for p in council.participants
            ]
            assert (
                "compatible-configured" in pages[1] and "ollama-configured" in pages[1]
            )
            assert pages[1].index("Participant 1 · remote") < pages[1].index(
                "Participant 2 · local"
            )
            if fail_remote:
                assert "provider_error" in pages[1]
            assert_private_absent(pages)

    run(execute())


@pytest.mark.parametrize("shutdown", [False, True])
def test_task_cancellation_and_shutdown_close_active_http(setup, shutdown):
    app, project, _, state, streams, _ = setup
    state["block_remote"] = True

    async def execute():
        await app.start()
        submitted = app.delegate_task(
            project_id=project.id,
            agent_id="repo_explorer",
            worker_id="remote",
            task="Read evidence",
        )
        try:
            while not streams:
                await asyncio.sleep(0)
            await streams[0].entered.wait()
            if shutdown:
                await app.close()
                task = app.tasks.get_task(submitted.task_id)
                assert task.error_code == "executor_cancelled"
            else:
                app.cancel_task(task_id=submitted.task_id)
                # Existing cancellation is cooperative at runtime boundaries.
                streams[0].release.set()
                task = await app.tasks.wait_task(submitted.task_id)
                assert task.state == "cancelled" and task.final_answer is None
            assert streams[0].closed
            assert app.telemetry.get_for_task(submitted.task_id).model_call_count == 1
            assert_database_private_absent(app)
        finally:
            await app.close()
        assert app._owned_providers["remote-connection"]._http.is_closed

    run(execute())


def test_startup_failure_closes_already_created_http_resources(setup, monkeypatch):
    app, _, _, _, _, _ = setup

    async def execute():
        client = app._owned_providers["remote-connection"]._client()

        async def fail():
            raise RuntimeError("safe-startup-failure")

        monkeypatch.setattr(app.tasks, "start", fail)
        with pytest.raises(RuntimeError):
            await app.start()
        assert app._closed and client.is_closed
        await app.close()

    run(execute())


def test_partial_composition_failure_never_opens_http_clients(setup, monkeypatch):
    app, _, _, _, _, path = setup
    config = load_workers(path)
    constructed = []
    original = PROVIDER_TYPES["ollama"]

    def first(connection):
        instance = original(connection)
        constructed.append(instance)
        return instance

    def fail(connection):
        raise ValueError("safe-construction-failure")

    monkeypatch.setitem(PROVIDER_TYPES, "ollama", first)
    monkeypatch.setitem(PROVIDER_TYPES, "openai_compatible", fail)
    from agentforge.providers.factory import create_providers

    with pytest.raises(ValueError):
        create_providers(config)
    assert len(constructed) == 1 and not hasattr(constructed[0], "_http")
    run(app.close())


def test_two_compatible_connections_dispatch_by_reference_not_type(setup, monkeypatch):
    app, project, _, _, _, path = setup
    from agentforge.db.database import create_database_engine

    path.write_text(
        path.read_text()
        + """\n[[providers]]
id="other-compatible"
type="openai_compatible"
base_url="http://other-gateway.invalid/custom/"

[[workers]]
id="other-worker"
provider="other-compatible"
model="different-model"
"""
    )
    requests = []

    def build(connection):
        def handle(req):
            requests.append(
                (connection.id, str(req.url), json.loads(req.content)["model"])
            )
            return httpx.Response(200, json=completion())

        return OpenAICompatibleProvider(
            connection, transport=httpx.MockTransport(handle)
        )

    monkeypatch.setitem(PROVIDER_TYPES, "openai_compatible", build)
    second = Application(
        create_database_engine(str(app.database.url)), load_workers(path), concurrency=2
    )

    async def execute():
        await app.close()
        await second.start()
        try:
            submissions = [
                second.delegate_task(
                    project_id=project.id,
                    agent_id="repo_explorer",
                    worker_id=worker,
                    task="Inspect",
                )
                for worker in ("remote", "other-worker")
            ]
            for task in submissions:
                assert (await second.tasks.wait_task(task.task_id)).state == "completed"
            assert requests == [
                (
                    "remote-connection",
                    "https://gateway.invalid/v1/chat/completions",
                    "compatible-configured",
                ),
                (
                    "other-compatible",
                    "http://other-gateway.invalid/custom/chat/completions",
                    "different-model",
                ),
            ]
            assert (
                second._owned_providers["remote-connection"]._http
                is not second._owned_providers["other-compatible"]._http
            )
        finally:
            await second.close()

    run(execute())


@pytest.mark.parametrize(
    "usage,expected",
    [
        (None, (None, None, None)),
        ({}, (None, None, None)),
        ({"total_tokens": 9}, (None, None, 9)),
        ({"prompt_tokens": 10}, (10, None, None)),
        ({"prompt_tokens": 10, "completion_tokens": 2}, (10, 2, 12)),
        (
            {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 15},
            (10, 2, 15),
        ),
    ],
)
def test_compatible_reported_usage_durable_coverage(
    setup, monkeypatch, usage, expected
):
    app, project, _, _, _, _ = setup
    # Extra backend-specific usage fields must not become public observations.
    wire_usage = usage | {"private_extra": SECRET} if usage is not None else None
    monkeypatch.setattr(
        app._owned_providers["remote-connection"]._transport,
        "handler",
        lambda _: httpx.Response(200, json=completion(usage=wire_usage)),
    )

    async def execute():
        await app.start()
        try:
            task = app.delegate_task(
                project_id=project.id,
                agent_id="repo_explorer",
                worker_id="remote",
                task="Inspect",
            )
            finished = await app.tasks.wait_task(task.task_id)
            assert finished.state == "completed"
            metric = app.telemetry.get_for_task(task.task_id)
            assert (
                metric.prompt_tokens,
                metric.completion_tokens,
                metric.total_tokens,
            ) == expected
            assert metric.token_usage_complete == (
                expected[0] is not None and expected[1] is not None
            )
            assert (
                metric.generation_duration_seconds is None
                and metric.tokens_per_second is None
            )
            assert_database_private_absent(app)
        finally:
            await app.close()

    run(execute())
