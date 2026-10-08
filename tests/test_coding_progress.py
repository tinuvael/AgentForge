"""Coding lifecycle metadata joins the same trace/observer; captures stay private."""

import asyncio

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agentforge.coding.models import ValidationRun
from agentforge.mcp.server import create_server
from tests.test_coding_tasks import Provider, application
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_mcp import PRIVATE, call, run

coding = coding_fixture


@pytest.mark.parametrize("exit_code", [0, 7])
def test_live_coding_lifecycle_is_safe_and_factual(
    coding, database, monkeypatch, exit_code
):
    async def execute():
        entered = asyncio.Event()
        release = asyncio.Event()
        received = []

        async def validate(_argv, **_options):
            entered.set()
            await release.wait()
            return ValidationRun(
                name="check",
                exit_code=exit_code,
                duration_seconds=0.25,
                stdout=PRIVATE,
                stderr=PRIVATE,
            )

        monkeypatch.setattr("agentforge.coding.service.validation_process", validate)
        provider = Provider([("run_validation", {"name": "check"})])
        app, _ = application(coding, database, provider)

        async def progress(_count, _total, message):
            received.append(message)
            release.set()

        async with create_connected_server_and_client_session(
            create_server(lambda: app)
        ) as client:
            identity = (
                await call(
                    client,
                    "delegate_task",
                    {
                        "project_id": str(coding.project.id),
                        "agent_id": "coder",
                        "worker_id": "one",
                        "task": "Run validation",
                    },
                )
            )["task_id"]
            await entered.wait()
            watched = await client.call_tool(
                "watch_task", {"task_id": identity}, progress_callback=progress
            )
            assert not watched.isError
            latest = app.task_progress(task_id=identity)
            assert latest.terminal
            # Validation failure is a recoverable tool outcome; Task may complete.
            assert latest.state == "completed"
            kinds = [event.kind for event in latest.timeline]
            assert kinds.index("workspace_provisioned") < kinds.index("model_request")
            assert kinds.index("validation_started") < kinds.index(
                "validation_completed"
            )
            started = next(e for e in latest.timeline if e.kind == "validation_started")
            assert started.step == 1 and started.success is None
            completed = next(
                e for e in latest.timeline if e.kind == "validation_completed"
            )
            assert completed.step == 1 and completed.success == (exit_code == 0)
            assert completed.duration_seconds == 0.25
            assert latest.timeline == app.dashboard.detail(latest.task_id).timeline
            public = watched.model_dump_json() + "".join(received)
            for private in (
                PRIVATE,
                str(coding.parent),
                "argv",
                "stdout",
                "stderr",
                "reasoning",
                "arguments",
            ):
                assert private not in public
            assert (
                app.get_coding_workspace(task_id=latest.task_id)
                .validation_runs[0]
                .stdout
                == PRIVATE
            )
            assert app.tasks.observer.subscriber_count == 0

    run(execute())


def test_validation_observer_failure_does_not_change_outcome(coding):
    setup = coding.create()

    def broken(*_):
        raise RuntimeError(PRIVATE)

    setup.session.on_validation = broken
    from tests.test_coding_validation import run_validation

    assert asyncio.run(run_validation(setup))["exit_code"] == 0
