"""Cloud acceptance across real MCP, Application, HTTP adapters, Git and SQLite."""

import asyncio
import json
import sys
from uuid import UUID

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agentforge.application.service import Application
from agentforge.db.database import create_session_factory
from agentforge.db.tasks import TaskRepository
from agentforge.mcp.server import create_server
from agentforge.providers.factory import PROVIDER_TYPES
from agentforge.providers.ollama import OllamaProvider
from agentforge.providers.openai_compatible import OpenAICompatibleProvider
from tests.synthetic import ScriptedEndpoint
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_coding_workspaces import primary_state, sha
from tests.test_mcp import call
from tests.test_web import client_for

coding = coding_fixture


def run(coroutine):
    async def bounded():
        async with asyncio.timeout(30):
            await coroutine

    asyncio.run(bounded())


@pytest.fixture(params=["ollama", "openai_compatible"])
def cloud(request, coding, database, tmp_path, monkeypatch):
    protocol = request.param
    endpoint = ScriptedEndpoint(protocol)
    adapter = OllamaProvider if protocol == "ollama" else OpenAICompatibleProvider
    monkeypatch.setitem(
        PROVIDER_TYPES,
        protocol,
        lambda connection: adapter(connection, transport=httpx.MockTransport(endpoint)),
    )
    workers = tmp_path / "workers.toml"
    workers.write_text(
        f'[[providers]]\nid="synthetic"\ntype="{protocol}"\n'
        'base_url="http://synthetic.invalid"\n'
        '[[workers]]\nid="selected"\nprovider="synthetic"\nmodel="synthetic"\n'
        "supports_tools=true\ntimeout_seconds=5\n"
    )
    config = tmp_path / "coding.toml"
    config.write_text(
        "workspace_parent=" + json.dumps(str(coding.config.workspace_parent)) + "\n"
        "git_executable=" + json.dumps(str(coding.config.git_executable)) + "\n"
    )

    def application():
        return Application.from_config(
            database_url=str(database[1]),
            workers_path=str(workers),
            coding_path=str(config),
        )

    return application, endpoint


@pytest.mark.parametrize(
    "agent",
    [
        "general_agent",
        pytest.param(
            "coder",
            marks=pytest.mark.skipif(
                sys.platform not in {"linux", "win32"},
                reason="Coding backend requires Linux or Windows; native macOS is #45",
            ),
        ),
    ],
)
def test_mcp_tool_rounds_durable_history_companion_and_worktree(cloud, coding, agent):
    application, endpoint = cloud
    endpoint.operations = [("read_file", {"path": "source.py"})]
    if agent == "coder":
        endpoint.operations += [
            (
                "apply_patch",
                {
                    "path": "source.py",
                    "expected_sha256": sha("original\n"),
                    "old_text": "original",
                    "new_text": "edited",
                },
            ),
            ("read_file", {"path": "source.py"}),
        ]
    before = primary_state(coding.root)

    async def execute():
        app = application()
        async with create_connected_server_and_client_session(
            create_server(lambda: app)
        ) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"delegate_task", "get_task", "watch_task", "cancel_task"} <= names
            projects = await call(client, "list_projects")
            assert projects["projects"][0]["project_id"] == str(coding.project.id)
            submitted = await call(
                client,
                "delegate_task",
                {
                    "project_id": str(coding.project.id),
                    "agent_id": agent,
                    "worker_id": "selected",
                    "task": "Read source.py and verify evidence.",
                },
            )
            assert submitted["state"] == "queued"
            identity = UUID(submitted["task_id"])
            final = await app.tasks.wait_task(identity)
            assert final.state == "completed", final.model_dump_json()
            assert final.started_at and final.finished_at
            result = await call(client, "get_task", {"task_id": str(identity)})
            assert result["final_answer"] == "Evidence verified."
            assert app.tasks.list_tasks()[0].task_id == identity
            metric = app.telemetry.get_for_task(identity)
            expected_calls = 3 if agent == "coder" else 1
            assert metric.tool_call_count == expected_calls
            assert metric.model_call_count == expected_calls + 1
            assert metric.total_tokens == 12 * (expected_calls + 1)
            assert endpoint.tool_results[0]["content"] == "original\n"
            if agent == "coder":
                assert endpoint.tool_results[-1]["content"] == "edited\n"
                diff = await call(client, "get_coding_diff", {"task_id": str(identity)})
                assert "+edited" in diff["content"]
                assert final.coding_result.changed_files == ("source.py",)
            assert len(endpoint.requests) == expected_calls + 1
        # A fresh application reads persisted results and telemetry after shutdown.
        reopened = application()
        async with client_for(reopened) as (web, _):
            assert reopened.tasks.get_task(identity) == final
            assert reopened.telemetry.get_for_task(identity) == metric
            for path in (f"/tasks/{identity}", f"/companion/tasks/{identity}"):
                page = await web.get(path)
                assert page.status_code == 200 and "Evidence verified." in page.text
            if agent == "coder":
                assert "+edited" in reopened.get_coding_diff(task_id=identity).content
        assert primary_state(coding.root) == before

    run(execute())


@pytest.mark.parametrize("shutdown", [False, True])
def test_running_cancellation_and_shutdown_survive_restart(cloud, coding, shutdown):
    application, endpoint = cloud
    endpoint.block = True

    async def execute():
        app = application()
        await app.start()
        try:
            submitted = app.delegate_task(
                project_id=coding.project.id,
                agent_id="general_agent",
                worker_id="selected",
                task="Read evidence",
            )
            await endpoint.body.entered.wait()
            assert app.get_task(task_id=submitted.task_id).state == "running"
            if shutdown:
                await app.close()
            else:
                app.cancel_task(task_id=submitted.task_id)
                endpoint.body.release.set()
                await app.tasks.wait_task(submitted.task_id)
            final = app.tasks.get_task(submitted.task_id)
            assert final.state == ("failed" if shutdown else "cancelled")
            assert final.final_answer is None and endpoint.body.closed
            metric = app.telemetry.get_for_task(submitted.task_id)
            assert metric.model_call_count == 1
        finally:
            await app.close()
        reopened = application()
        try:
            await reopened.start()
            assert reopened.tasks.get_task(submitted.task_id) == final
            assert reopened.telemetry.get_for_task(submitted.task_id) == metric
            assert len(endpoint.requests) == 1
        finally:
            await reopened.close()

    run(execute())


def test_restart_fails_interrupted_task_and_executes_only_queued_work(cloud, coding):
    application, endpoint = cloud

    async def execute():
        app = application()
        binding = dict(
            project_id=coding.project.id,
            agent_id="general_agent",
            worker_id="selected",
            task="Read evidence",
        )
        try:
            interrupted = app.tasks.submit(**binding)
            queued = app.tasks.submit(**binding)
            cancelled = app.tasks.submit(**binding)
            # Persist the pre-crash state using the real repository transaction.
            repository = TaskRepository(create_session_factory(app.database))
            repository.cancel(cancelled.task_id)
            assert repository.claim(interrupted.task_id).state == "running"
        finally:
            await app.close()
        reopened = application()
        try:
            await reopened.start()
            orphan = reopened.tasks.get_task(interrupted.task_id)
            assert orphan.state == "failed" and orphan.reason == "execution_interrupted"
            assert reopened.tasks.get_task(cancelled.task_id).state == "cancelled"
            assert (await reopened.tasks.wait_task(queued.task_id)).state == "completed"
            assert len(endpoint.requests) == 1
            assert len(reopened.tasks.list_tasks()) == 3
        finally:
            await reopened.close()

    run(execute())
