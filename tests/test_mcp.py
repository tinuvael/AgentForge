"""Official SDK in-process protocol tests: real migrated storage, fake inference."""

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError
from sqlalchemy import update

from agentforge.agents import REPO_EXPLORER
from agentforge.application.service import Application
from agentforge.core.inference import GenerationResult, ToolCall
from agentforge.core.worker import Worker
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.models import TaskRecord
from agentforge.db.tasks import TaskRepository
from agentforge.mcp.server import create_server, main
from agentforge.projects.errors import ProjectStorageError
from agentforge.tasks.models import TaskStorageError
from agentforge.workers.config import WorkersConfig

PRIVATE = "PRIVATE_SQL_CREDENTIAL_REASONING_SOURCE"
PUBLIC_TOOLS = {
    "get_coding_workspace",
    "get_coding_diff",
    "cleanup_coding_workspace",
    "agentforge_status",
    "describe_capabilities",
    "list_projects",
    "list_workers",
    "list_agents",
    "delegate_task",
    "get_task",
    "cancel_task",
    "delegate_council",
    "get_council",
    "cancel_council",
}


def run(coroutine):
    async def bounded():
        async with asyncio.timeout(10):
            return await coroutine

    return asyncio.run(bounded())


class ScriptedProvider:
    name = "fake"

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.requests = []
        self.workers = []
        self.turns = []
        self.cleaned = 0

    async def health(self, worker):
        pytest.fail("MCP discovery must not probe or choose Workers")

    async def generate(self, worker, request):
        self.workers.append(worker)
        self.requests.append(request)
        self.entered.set()
        try:
            await self.release.wait()
            response = (
                self.turns.pop(0)
                if self.turns
                else GenerationResult(
                    content="Evidence found.",
                    model=worker.model,
                    reasoning=PRIVATE,
                    usage={"private": PRIVATE},
                    timing={"private": PRIVATE},
                )
            )
            if isinstance(response, Exception):
                raise response
            return response
        finally:
            self.cleaned += 1


@pytest.fixture
def setup(database, registry, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "source.py").write_text(f"def {PRIVATE}():\n    pass\n")
    project = registry.register_project("Registered", root)
    workers = WorkersConfig(
        workers=[
            Worker(
                id=identity,
                provider="fake",
                model="configured-model",
                endpoint=endpoint,
                supports_tools=True,
                context_window=32768,
                deployment_label=identity,
                options={"api_key": PRIVATE},
            )
            for identity, endpoint in (
                ("local-4080", "http://localhost:11434"),
                ("home-i5", "http://192.168.50.12:11434"),
                ("ai395", "http://100.100.100.10:11434"),
                ("cloud", "https://inference.example.invalid/private-path"),
            )
        ]
    )
    provider = ScriptedProvider()

    def build(**overrides):
        arguments = dict(providers={"fake": provider})
        arguments.update(overrides)
        return Application(create_database_engine(database[1]), workers, **arguments)

    app = build()
    return SimpleNamespace(
        app=app,
        build=build,
        provider=provider,
        project=project,
        registry=registry,
        root=root,
        database=database,
        workers=workers,
        binding=dict(
            project_id=str(project.id),
            agent_id="repo_explorer",
            worker_id="home-i5",
            task="Inspect evidence.",
        ),
    )


async def call(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    assert not result.isError, result
    return result.structuredContent


async def error(client, name, arguments, code):
    result = await client.call_tool(name, arguments)
    assert result.isError
    assert result.structuredContent is None
    body = json.loads(result.content[0].text)
    assert body["code"] == code
    assert set(body) == {"code", "message"}
    assert PRIVATE not in result.model_dump_json()
    return body


def test_discovery_registration_schemas_and_privacy(setup):
    async def execute():
        server = create_server(lambda: setup.app)
        async with create_connected_server_and_client_session(server) as client:
            listed = (await client.list_tools()).tools
            assert {t.name for t in listed} == PUBLIC_TOOLS
            for tool in listed:
                assert tool.inputSchema["additionalProperties"] is False
                assert tool.outputSchema["type"] == "object"
            delegate = next(t for t in listed if t.name == "delegate_task")
            assert set(delegate.inputSchema["required"]) == set(setup.binding)
            assert "default" not in delegate.inputSchema["properties"]["worker_id"]
            assert delegate.inputSchema["properties"]["project_id"]["format"] == "uuid"
            status = await call(client, "agentforge_status")
            assert status["available"] and status["database_available"]
            assert status["project_count"] == 1 and status["worker_count"] == 4
            assert status["queued_tasks"] == status["running_tasks"] == 0
            capabilities = await call(client, "describe_capabilities")
            assert (
                capabilities["worker_selection"]
                == "explicit_project_agent_worker_required"
            )
            projects = await call(client, "list_projects")
            assert projects["projects"][0]["project_id"] == str(setup.project.id)
            assert projects["projects"][0]["root_path"] == str(setup.root)
            assert projects["projects"][0]["git_unavailable_reason"] == "not_probed"
            workers = await call(client, "list_workers")
            assert [w["worker_id"] for w in workers["workers"]] == [
                "ai395",
                "cloud",
                "home-i5",
                "local-4080",
            ]
            assert all(w["health_status"] == "not_probed" for w in workers["workers"])
            assert all(
                w["supports_tools"] and w["context_window"] == 32768
                for w in workers["workers"]
            )
            agents = await call(client, "list_agents")
            assert agents["agents"][0]["allowed_tools"] == list(
                REPO_EXPLORER.allowed_tools
            )
            assert agents["agents"][0]["limits"] == REPO_EXPLORER.limits.model_dump()
            raw = json.dumps([projects, workers, agents])
            assert PRIVATE not in raw and "endpoint" not in raw
            assert "system_prompt" not in raw and "recommended" not in raw
            assert not setup.provider.requests
            with pytest.raises(McpError, match="Method not found"):
                await client.list_resources()

    run(execute())


def test_bounded_stable_pagination_and_argument_validation(setup):
    for i in range(3):
        root = setup.root.parent / f"project-{i}"
        root.mkdir()
        setup.registry.register_project(f"Project {i}", root)

    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            for name, key in (
                ("list_projects", "projects"),
                ("list_workers", "workers"),
            ):
                first = await call(client, name, {"limit": 2})
                second = await call(
                    client, name, {"limit": 2, "offset": first["next_offset"]}
                )
                assert len(first[key]) == len(second[key]) == 2
                assert second["next_offset"] is None
                assert first[key] != second[key]
                assert (await call(client, name, {"offset": 100}))[key] == []
            for arguments in (
                {"limit": 0},
                {"limit": 101},
                {"limit": True},
                {"limit": "1"},
                {"offset": -1},
                {"path": PRIVATE},
            ):
                await error(client, "list_projects", arguments, "invalid_arguments")
            await error(
                client, "list_workers", {"include_health": True}, "invalid_arguments"
            )

    run(execute())


def test_protocol_delegate_does_not_wait_exact_binding_and_cancellation(
    setup, monkeypatch
):
    setup.provider.release.clear()
    submitted = []
    original_submit = setup.app.tasks.submit

    def submit(**binding):
        submitted.append(binding)
        return original_submit(**binding)

    monkeypatch.setattr(setup.app.tasks, "submit", submit)

    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            first = await call(client, "delegate_task", setup.binding)
            assert first["state"] == "queued"  # Provider still blocked; no wait_task.
            assert first["worker_id"] == "home-i5" and len(submitted) == 1
            await setup.provider.entered.wait()
            running = await call(client, "get_task", {"task_id": first["task_id"]})
            assert (
                running["state"] == "running"
                and running["telemetry_status"] == "pending"
            )
            queued = await call(
                client, "delegate_task", {**setup.binding, "worker_id": "cloud"}
            )
            status = await call(client, "agentforge_status")
            assert status["running_tasks"] == status["queued_tasks"] == 1
            queued_id = {"task_id": queued["task_id"]}
            assert (await call(client, "get_task", queued_id))["state"] == "queued"
            assert (await call(client, "cancel_task", queued_id))[
                "state"
            ] == "cancelled"
            assert setup.provider.workers[0].id == "home-i5"
            first_id = {"task_id": first["task_id"]}
            cancelled = await call(client, "cancel_task", first_id)
            assert cancelled["state"] == "running"
            assert cancelled["cancellation_requested_at"] is not None
            setup.provider.release.set()
            await setup.app.tasks.wait_task(first["task_id"])
            terminal = await call(client, "get_task", first_id)
            assert terminal["state"] == "cancelled" and terminal["final_answer"] is None
            assert (await call(client, "cancel_task", first_id)) == terminal
            assert (
                len(setup.provider.workers) == 1
            )  # Queued cancellation never executed.

    run(execute())


def test_sdk_repo_explorer_result_reasoning_privacy_and_multiworker(setup):
    setup.provider.turns = [
        GenerationResult(
            content="",
            model="backend",
            reasoning=PRIVATE,
            tool_calls=[
                ToolCall(id="read", name="read_file", arguments={"path": "source.py"})
            ],
        ),
        GenerationResult(
            content="Source evidence: source.py:1.", model="backend", reasoning=PRIVATE
        ),
    ]

    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            for worker in setup.workers.workers:
                task = await call(
                    client, "delegate_task", {**setup.binding, "worker_id": worker.id}
                )
                await setup.app.tasks.wait_task(task["task_id"])
                result = await call(client, "get_task", {"task_id": task["task_id"]})
                assert result["state"] == "completed" and result["final_answer"]
                assert result["worker_id"] == worker.id
                assert result["telemetry_status"] == "recorded"
                assert PRIVATE not in json.dumps(result)
                assert set(result["execution_summary"]) == {
                    "steps",
                    "tool_call_count",
                    "tool_output_bytes",
                }
                assert (
                    "trace" not in result
                    and "request" not in result
                    and "execution_result" not in result
                )
                assert (
                    await call(client, "cancel_task", {"task_id": task["task_id"]})
                ) == result
            assert [w.id for w in setup.provider.workers] == [
                "local-4080",
                "local-4080",
                "home-i5",
                "ai395",
                "cloud",
            ]
            assert PRIVATE in setup.provider.requests[1].messages[-1].content

    run(execute())


@pytest.mark.parametrize("field", ["project_id", "agent_id", "worker_id", "task"])
def test_required_explicit_bindings(setup, field):
    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            binding = setup.binding.copy()
            del binding[field]
            await error(client, "delegate_task", binding, "invalid_arguments")
            assert setup.app.tasks.list_tasks() == []

    run(execute())


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"project_id": "malformed"}, "invalid_arguments"),
        ({"project_id": str(uuid4())}, "project_not_found"),
        ({"agent_id": "missing"}, "agent_not_found"),
        ({"worker_id": "missing"}, "worker_not_found"),
        ({"task": "  "}, "invalid_arguments"),
        ({"task": "a" * 32769}, "invalid_arguments"),
        ({"root_path": PRIVATE}, "invalid_arguments"),
        ({"worker_id": None}, "invalid_arguments"),
    ],
)
def test_invalid_bindings_before_task_creation(setup, override, code):
    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            await error(client, "delegate_task", {**setup.binding, **override}, code)
            assert setup.app.tasks.list_tasks() == [] and not setup.provider.requests

    run(execute())


@pytest.mark.parametrize("kind", ["tools", "provider"])
def test_incompatible_binding_no_task(setup, kind):
    if kind == "tools":
        worker = setup.workers.workers[1].model_copy(update={"supports_tools": False})
        workers = setup.workers.model_copy(update={"workers": [worker]})
        app = Application(
            create_database_engine(setup.database[1]),
            workers,
            providers={"fake": setup.provider},
        )
    else:
        app = setup.build(providers={})

    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: app)
        ) as client:
            await error(
                client, "delegate_task", setup.binding, "invalid_execution_binding"
            )
            assert not app.tasks.list_tasks()

    run(execute())


@pytest.mark.parametrize("operation", ["get_task", "cancel_task"])
def test_missing_and_malformed_task_ids(setup, operation):
    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            await error(
                client, operation, {"task_id": "malformed"}, "invalid_arguments"
            )
            await error(client, operation, {"task_id": str(uuid4())}, "task_not_found")

    run(execute())


@pytest.mark.parametrize(
    "exception",
    [RuntimeError(PRIVATE), TaskStorageError(PRIVATE), ProjectStorageError(PRIVATE)],
)
def test_unexpected_and_storage_errors_are_sanitized(setup, monkeypatch, exception):
    def fail(**_):
        raise exception

    monkeypatch.setattr(setup.app.tasks, "submit", fail)

    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            await error(
                client,
                "delegate_task",
                setup.binding,
                "internal_error"
                if type(exception) is RuntimeError
                else "storage_unavailable",
            )
            assert not setup.app.tasks.list_tasks()

    run(execute())


def test_failed_task_safe_code_and_no_missing_telemetry_fabrication(setup):
    setup.provider.turns = [RuntimeError(PRIVATE)]

    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            task = await call(client, "delegate_task", setup.binding)
            await setup.app.tasks.wait_task(task["task_id"])
            sessions = create_session_factory(setup.database[0])
            with sessions() as session:
                session.execute(
                    update(TaskRecord)
                    .where(TaskRecord.task_id == UUID(task["task_id"]))
                    .values(telemetry_status="unavailable")
                )
                session.commit()
            result = await call(client, "get_task", {"task_id": task["task_id"]})
            assert (
                result["state"] == "failed" and result["error_code"] == "provider_error"
            )
            assert (
                result["final_answer"] is None
                and result["telemetry_status"] == "unavailable"
            )
            assert "tokens" not in json.dumps(result) and PRIVATE not in json.dumps(
                result
            )

    run(execute())


def test_lifecycle_once_shutdown_preserves_queue_and_reopen_recovery(
    setup, monkeypatch
):
    setup.provider.release.clear()
    counts = dict(start=0, close=0, dispose=0)
    original_start, original_close = setup.app.tasks.start, setup.app.tasks.close
    original_dispose = setup.app.database.dispose

    async def start():
        counts["start"] += 1
        await original_start()

    async def close():
        counts["close"] += 1
        await original_close()

    def dispose():
        counts["dispose"] += 1
        original_dispose()

    monkeypatch.setattr(setup.app.tasks, "start", start)
    monkeypatch.setattr(setup.app.tasks, "close", close)
    monkeypatch.setattr(setup.app.database, "dispose", dispose)

    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            await setup.app.start()
            first = await call(client, "delegate_task", setup.binding)
            await setup.provider.entered.wait()
            queued = await call(client, "delegate_task", setup.binding)
            second = setup.build()
            with pytest.raises(RuntimeError, match="already owns"):
                await second.start()
            await second.close()
        assert counts == dict(start=1, close=1, dispose=1)
        assert setup.provider.cleaned == 1
        repository = TaskRepository(create_session_factory(setup.database[0]))
        assert repository.get(UUID(first["task_id"])).reason == "executor_cancelled"
        # Simulate a process lost after claiming a queued row, without replay.
        repository.claim(UUID(queued["task_id"]))
        setup.provider.release.set()
        reopened = setup.build()
        async with create_connected_server_and_client_session(
            create_server(lambda: reopened)
        ) as client:
            orphan = await call(client, "get_task", {"task_id": queued["task_id"]})
            assert (
                orphan["state"] == "failed"
                and orphan["reason"] == "execution_interrupted"
            )
            historical = await call(client, "get_task", {"task_id": first["task_id"]})
            assert historical["reason"] == "executor_cancelled"
            assert (
                len(setup.provider.requests) == 1
            )  # The interrupted request was never replayed.
        assert counts == dict(start=1, close=1, dispose=1)

    run(execute())


def test_task_history_completed_survives_restart(setup):
    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            task = await call(client, "delegate_task", setup.binding)
            await setup.app.tasks.wait_task(task["task_id"])
            completed = await call(client, "get_task", {"task_id": task["task_id"]})
        reopened = setup.build()
        async with create_connected_server_and_client_session(
            create_server(lambda: reopened)
        ) as client:
            assert (
                await call(client, "get_task", {"task_id": task["task_id"]})
                == completed
            )

    run(execute())


def test_unavailable_executor_rejects_submission(setup):
    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            setup.app.tasks._loops[0].cancel()
            await asyncio.gather(*setup.app.tasks._loops, return_exceptions=True)
            assert not (await call(client, "agentforge_status"))["available"]
            await error(client, "delegate_task", setup.binding, "service_unavailable")
            assert not setup.app.tasks.list_tasks()

    run(execute())


def test_endpoint_userinfo_rejected():
    with pytest.raises(ValidationError):
        Worker(
            id="secret",
            provider="fake",
            model="model",
            endpoint=f"https://user:{PRIVATE}@example.invalid",
        )


def test_cli_startup_error_sanitized_stderr(monkeypatch, capsys, caplog):
    monkeypatch.setattr(
        "sys.argv",
        ["agentforge", "--database-url", "sqlite:///ignored", "--workers", "ignored"],
    )

    async def fail(**_):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr("agentforge.mcp.server.run_stdio", fail)
    monkeypatch.setattr("logging.basicConfig", lambda **_: None)
    assert main() == 1
    assert PRIVATE not in caplog.text
    assert capsys.readouterr().out == ""


def test_actual_storage_outage_and_no_bypass_tools(setup):
    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            for name in ("read_file", "shell", "provider_generate", "sql", "git"):
                await error(client, name, {"path": PRIVATE}, "invalid_arguments")
            TaskRecord.__table__.drop(setup.database[0])
            await error(client, "agentforge_status", {}, "storage_unavailable")
            await error(
                client, "get_task", {"task_id": str(uuid4())}, "storage_unavailable"
            )

    run(execute())


def test_factory_shared_once_and_queued_work_resumes(setup):
    setup.provider.release.clear()
    apps = []

    def factory():
        app = setup.build()
        apps.append(app)
        return app

    async def execute():
        server = create_server(factory)
        async with create_connected_server_and_client_session(server) as client:
            first = await call(client, "delegate_task", setup.binding)
            await setup.provider.entered.wait()
            queued = await call(client, "delegate_task", setup.binding)
            await call(client, "list_projects")
            await call(client, "list_workers")
            assert len(apps) == 1
        repository = TaskRepository(create_session_factory(setup.database[0]))
        assert repository.get(UUID(queued["task_id"])).state == "queued"
        setup.provider.release.set()
        async with create_connected_server_and_client_session(server) as client:
            assert len(apps) == 2
            await apps[-1].tasks.wait_task(queued["task_id"])
            result = await call(client, "get_task", {"task_id": queued["task_id"]})
            assert result["state"] == "completed"
            assert (await call(client, "get_task", {"task_id": first["task_id"]}))[
                "reason"
            ] == "executor_cancelled"
        assert len(setup.provider.workers) == 2

    run(execute())


def test_stdio_subprocess_discovery_only_no_network(setup, tmp_path):
    import sys

    from mcp import ClientSession, StdioServerParameters, types
    from mcp.client.stdio import stdio_client
    from mcp.shared.message import SessionMessage

    workers_path = tmp_path / "workers.toml"
    workers_path.write_text("""[[workers]]
id = "remote"
provider = "ollama"
model = "configured"
endpoint = "http://192.0.2.1:11434"
supports_tools = true
""")

    async def execute():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agentforge.mcp.server",
                "--database-url",
                str(setup.database[1]),
                "--workers",
                str(workers_path),
            ],
        )
        with (tmp_path / "stderr.log").open("w") as errlog:
            async with stdio_client(parameters, errlog=errlog) as (read, write):
                await write.send(
                    SessionMessage(
                        message=types.JSONRPCMessage(
                            types.JSONRPCRequest(
                                jsonrpc="2.0",
                                id=77,
                                method="tools/call",
                                params={"name": 42, "arguments": {"token": PRIVATE}},
                            )
                        )
                    )
                )
                invalid = await read.receive()
                assert (
                    invalid.message.root.error.message == "Invalid request parameters"
                )
                assert PRIVATE not in invalid.message.model_dump_json()
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    assert (await call(client, "agentforge_status"))["available"]
                    workers = await call(client, "list_workers")
                    assert workers["workers"][0]["worker_id"] == "remote"
                    assert workers["workers"][0]["health_status"] == "not_probed"
                    assert {
                        t.name for t in (await client.list_tools()).tools
                    } == PUBLIC_TOOLS
        diagnostics = (tmp_path / "stderr.log").read_text()
        assert "service_unavailable" in diagnostics and PRIVATE not in diagnostics

    run(execute())
