"""Ordinary Task/Provider, Council, MCP and dashboard coding composition."""

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import func, select

from agentforge.agents.models import CancellationToken
from agentforge.application.service import Application
from agentforge.coding.tools import CODER
from agentforge.core.inference import GenerationResult, ToolCall
from agentforge.core.worker import Worker
from agentforge.councils.models import InvalidCouncil
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.models import CouncilRecord, TaskRecord
from agentforge.db.tasks import TaskRepository
from agentforge.mcp.server import create_server
from agentforge.workers.config import WorkersConfig
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_coding_workspaces import git, primary_state, sha, write
from tests.test_mcp import call as mcp_call
from tests.test_mcp import error
from tests.test_web import client_for, csrf

coding = coding_fixture


async def call(client, name, **arguments):
    return await mcp_call(client, name, arguments or None)


class Provider:
    name = "fake"

    def __init__(self, operations=()):
        self.operations = list(operations)
        self.requests = []
        self.app = None

    async def generate(self, worker, request):
        if self.app:
            tasks = self.app.tasks.list_tasks(state="running")
            report = self.app.coding.get(tasks[0].task_id)
            assert report.state == "ready" and report.inspection_available
        self.requests.append(request)
        operation = self.operations.pop(0) if self.operations else None
        if isinstance(operation, Exception):
            raise operation
        return GenerationResult(
            model=worker.model,
            content="" if operation else "Fixed and reviewed.",
            reasoning="PRIVATE_REASONING",
            tool_calls=[
                ToolCall(
                    id=str(len(self.requests)),
                    name=operation[0],
                    arguments=operation[1],
                )
            ]
            if operation
            else [],
        )


def application(coding, database, provider=None, **options):
    provider = provider or Provider()
    workers = WorkersConfig(
        workers=[
            Worker(
                id=identity,
                provider="fake",
                model="offline",
                endpoint="http://localhost:9999",
                supports_tools=True,
            )
            for identity in ("one", "two")
        ]
    )
    app = Application(
        create_database_engine(database[1]),
        workers,
        providers={"fake": provider},
        coding_config=coding.config,
        **options,
    )
    provider.app = app
    return app, provider


def submit(app, coding, agent="coder"):
    return app.tasks.submit(
        project_id=coding.project.id,
        agent_id=agent,
        worker_id="one",
        task="Fix the file",
    )


def test_coder_edits_validates_and_reads_own_worktree(coding, database):
    provider = Provider(
        [
            ("read_file", {"path": "source.py"}),
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
            ("run_validation", {"name": "check"}),
            ("git_diff", {}),
        ]
    )
    before = primary_state(coding.root)

    async def execute():
        app, _ = application(coding, database, provider)
        indexed = app.index.refresh_index(coding.project.id)
        try:
            await app.start()
            task = submit(app, coding)
            final = await app.tasks.wait_task(task.task_id)
            assert final.state == "completed"
            assert final.coding_result.state == "completed"
            assert final.coding_result.task_id == final.task_id
            assert final.coding_result.worker_id == "one"
            assert final.coding_result.changed_files == ("source.py",)
            assert final.coding_result.validation_runs[0].exit_code == 0
            assert final.coding_result.bytes_written == len("edited\n")
            assert final.final_answer == "Fixed and reviewed."
            assert "PRIVATE_REASONING" not in final.model_dump_json()
            source_result = json.loads(provider.requests[3].messages[-1].content)[
                "result"
            ]
            assert source_result["content"] == "edited\n"
            assert source_result["sha256"] == sha("edited\n")
            assert "+edited" in app.get_coding_diff(task_id=task.task_id).content
            assert (
                app.index.get_index_status(coding.project.id).indexed_at
                == indexed.indexed_at
            )
            assert not app.index.get_index_status(coding.project.id).changed_paths
            public = app.get_task(task_id=task.task_id).model_dump_json()
            assert str(coding.parent) not in public and "root_path" not in public
            telemetry = app.telemetry.get_for_task(task.task_id)
            assert telemetry.tool_call_count == 5 and telemetry.model_call_count == 6
        finally:
            await app.close()

    asyncio.run(execute())
    assert primary_state(coding.root) == before


@pytest.mark.parametrize("agent", ["repo_explorer", "renamed_coder"])
def test_tool_capabilities_and_council_generic_rejection(coding, database, agent):
    async def execute():
        renamed = CODER.model_copy(update={"id": "renamed_coder"})
        from agentforge.agents.repo_explorer import REPO_EXPLORER

        provider = Provider(
            [
                (
                    "write_file",
                    {"path": "attack", "content": "bad", "expected_sha256": None},
                )
            ]
        )
        app, _ = application(
            coding, database, provider, agents=(REPO_EXPLORER, renamed)
        )
        try:
            await app.start()
            if agent == "repo_explorer":
                # Provider assertion is coding-specific; explorer has no workspace.
                provider.app = None
                task = submit(app, coding, agent)
                final = await app.tasks.wait_task(task.task_id)
                assert final.reason == "tool_not_allowed"
                assert not list(coding.parent.glob("*/*"))
                assert not (coding.root / "attack").exists()
            else:
                with create_session_factory(app.database)() as session:
                    before = session.scalar(
                        select(func.count()).select_from(TaskRecord)
                    )
                with pytest.raises(InvalidCouncil):
                    app.delegate_council(
                        project_id=coding.project.id,
                        agent_id=agent,
                        worker_ids=("one", "two"),
                        task="Fix",
                    )
                with create_session_factory(app.database)() as session:
                    assert (
                        session.scalar(select(func.count()).select_from(TaskRecord))
                        == before
                    )
                    assert (
                        session.scalar(select(func.count()).select_from(CouncilRecord))
                        == 0
                    )
                assert not list(coding.parent.glob("*/*")) and not provider.requests
        finally:
            await app.close()

    asyncio.run(execute())


def test_provisioning_failure_has_zero_inference(coding, database):
    async def execute():
        app, provider = application(coding, database)
        try:
            task = submit(app, coding)
            git(coding.root, "branch", "agentforge/task-" + str(task.task_id))
            await app.start()
            final = await app.tasks.wait_task(task.task_id)
            assert final.state == "failed" and not provider.requests
            assert not list(coding.parent.glob("*/*"))
            assert app.telemetry.get_for_task(task.task_id).model_call_count == 0
        finally:
            await app.close()

    asyncio.run(execute())


def test_cancel_during_validation_preserves_changes(coding, database):
    from tests.test_coding_validation import command

    command(
        coding, "import time; print('started',flush=True); time.sleep(10)", timeout=2.0
    )
    coding.config = coding.manager.config
    provider = Provider(
        [
            (
                "write_file",
                {"path": "partial", "content": "review me", "expected_sha256": None},
            ),
            ("run_validation", {"name": "test"}),
            (
                "write_file",
                {"path": "too_late", "content": "bad", "expected_sha256": None},
            ),
        ]
    )

    async def execute():
        app, _ = application(coding, database, provider)
        try:
            await app.start()
            task = submit(app, coding)
            async with asyncio.timeout(5):
                while len(provider.requests) < 2:
                    await asyncio.sleep(0.01)
                # Runtime is awaiting the process; cancellation must run on the
                # same executor loop and promptly terminate validation descendants.
                await asyncio.sleep(0.05)
                app.cancel_task(task_id=task.task_id)
                final = await app.tasks.wait_task(task.task_id)
            assert final.state == "cancelled"
            report = app.get_coding_workspace(task_id=task.task_id)
            assert report.state == "cancelled" and report.validation_runs[0].cancelled
            assert report.changed_files == ("partial",)
            assert "review me" in app.get_coding_diff(task_id=task.task_id).content
            assert len(provider.requests) == 2
        finally:
            await app.close()

    asyncio.run(execute())


def test_failed_task_retains_partial_workspace(coding, database):
    provider = Provider(
        [
            (
                "write_file",
                {"path": "partial", "content": "partial", "expected_sha256": None},
            ),
            RuntimeError("PRIVATE_SECRET"),
        ]
    )

    async def execute():
        app, _ = application(coding, database, provider)
        try:
            await app.start()
            task = submit(app, coding)
            final = await app.tasks.wait_task(task.task_id)
            assert final.state == "failed" and final.coding_result.state == "failed"
            assert "partial" in app.get_coding_diff(task_id=task.task_id).content
            assert "PRIVATE_SECRET" not in final.model_dump_json()
        finally:
            await app.close()

    asyncio.run(execute())


def test_restart_does_not_replay_dirty_workspace(coding, database):
    app, provider = application(coding, database)
    task = submit(app, coding)
    repository = TaskRepository(create_session_factory(app.database))
    running = repository.claim(task.task_id)
    app.coding.create(running)
    session = app.coding.bind(running, CancellationToken())
    write(SimpleNamespace(session=session), "partial", "partial")

    async def execute():
        try:
            await app.start()
            final = app.tasks.get_task(task.task_id)
            assert final.reason == "execution_interrupted"
            report = app.get_coding_workspace(task_id=task.task_id)
            assert report.state == "interrupted"
            assert "partial" in app.get_coding_diff(task_id=task.task_id).content
            assert not provider.requests
            removed = app.cleanup_coding_workspace(
                task_id=task.task_id, workspace_id=task.task_id
            )
            assert removed.state == "removed"
        finally:
            await app.close()

    asyncio.run(execute())


def test_unprovisioned_direct_coder_run_has_zero_requests(coding, database):
    async def execute():
        app, provider = application(coding, database)
        try:
            result = await app.tasks._runtime.run(
                project_id=coding.project.id,
                agent_id="coder",
                worker_id="one",
                task="Fix",
            )
            assert result.reason == "invalid_configuration" and not provider.requests
        finally:
            await app.close()

    asyncio.run(execute())


def test_mcp_coding_delegation_inspection_cleanup(coding, database):
    provider = Provider(
        [("write_file", {"path": "new", "content": "new", "expected_sha256": None})]
    )

    async def execute():
        app, _ = application(coding, database, provider)
        server = create_server(lambda: app)
        async with create_connected_server_and_client_session(server) as client:
            listed = (await client.list_tools()).tools
            cleanup = next(
                tool for tool in listed if tool.name == "cleanup_coding_workspace"
            )
            assert (
                cleanup.annotations.destructiveHint
                and not cleanup.annotations.readOnlyHint
            )
            assert all(
                tool.name not in {"shell", "push", "merge", "create_worktree"}
                for tool in listed
            )
            agents = await call(client, "list_agents")
            assert (
                next(a for a in agents["agents"] if a["agent_id"] == "coder")[
                    "workspace_mode"
                ]
                == "isolated_write"
            )
            submitted = await call(
                client,
                "delegate_task",
                project_id=str(coding.project.id),
                agent_id="coder",
                worker_id="one",
                task="Fix",
            )
            task_id = UUID(submitted["task_id"])
            await app.tasks.wait_task(task_id)
            report = await call(client, "get_coding_workspace", task_id=str(task_id))
            diff = await call(client, "get_coding_diff", task_id=str(task_id))
            assert "+new" in diff["content"]
            assert str(coding.parent) not in json.dumps(report)
            await error(
                client,
                "cleanup_coding_workspace",
                {"task_id": str(task_id), "workspace_id": str(uuid4())},
                "coding_unavailable",
            )
            removed = await call(
                client,
                "cleanup_coding_workspace",
                task_id=str(task_id),
                workspace_id=report["workspace_id"],
            )
            assert removed["state"] == "removed"

    asyncio.run(execute())


def test_dashboard_coding_escaping_and_confirmed_csrf_cleanup(coding, database):
    from tests.test_coding_validation import command

    command(coding, "print('\\x1b[31m<script>alert(1)</script>\\x1b[0m')", name="html")
    coding.config = coding.manager.config
    provider = Provider(
        [
            (
                "write_file",
                {
                    "path": "html.txt",
                    "content": "<script>diff</script>\n",
                    "expected_sha256": None,
                },
            ),
            ("run_validation", {"name": "html"}),
        ]
    )

    async def execute():
        app, _ = application(coding, database, provider)
        async with client_for(app) as (client, _):
            task = submit(app, coding)
            await app.tasks.wait_task(task.task_id)
            path = f"/tasks/{task.task_id}"
            response = await client.get(path)
            assert response.status_code == 200
            assert (
                "Coding workspace" in response.text
                and "Remove workspace" in response.text
            )
            assert (
                "Base commit" in response.text and "agentforge/task-" in response.text
            )
            assert "&lt;script&gt;diff&lt;/script&gt;" in response.text
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
            assert (
                "<script>alert(1)</script>" not in response.text
                and "\x1b" not in response.text
            )
            assert str(coding.parent) not in response.text
            assert "Merge" not in response.text and "Push" not in response.text
            remove = path + "/workspace/remove"
            assert (await client.get(remove)).status_code == 405
            assert (
                await client.post(remove, data={"confirm": "yes"})
            ).status_code == 403
            assert (
                await client.post(remove, data={"csrf": csrf(response)})
            ).status_code == 403
            assert (
                await client.post(
                    remove, data={"csrf": csrf(response), "confirm": "yes"}
                )
            ).status_code == 303
            assert app.get_coding_workspace(task_id=task.task_id).state == "removed"

    asyncio.run(execute())
