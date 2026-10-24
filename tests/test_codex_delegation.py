"""Scripted Director acceptance rubric, NOT model-driven Codex tool selection.

Real typed MCP adapter/application lifecycle with a mocked inference Worker.
Authorization choices are explicit fixture inputs, not an AgentForge router.
"""

import asyncio
from uuid import uuid4

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agentforge.mcp.server import create_server
from tests.test_mcp import call, error, run
from tests.test_mcp import setup as setup_fixture

setup = setup_fixture


@pytest.mark.parametrize(
    "scenario", ["trivial", "unavailable", "unregistered", "denied-cloud"]
)
def test_scripted_non_delegation_cases(setup, scenario):
    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            if scenario == "trivial":
                # Scripted answer to "What is 2 + 2?": zero MCP calls required.
                assert 2 + 2 == 4
            elif scenario == "unavailable":
                # Simulate a stale/unavailable service, not a live client outage.
                setup.app.tasks._loops[0].cancel()
                await asyncio.gather(*setup.app.tasks._loops, return_exceptions=True)
                assert not (await call(client, "agentforge_status"))["available"]
            else:
                assert (await call(client, "agentforge_status"))["available"]
                await call(client, "describe_capabilities")
                projects = (await call(client, "list_projects"))["projects"]
                workers = (await call(client, "list_workers"))["workers"]
                await call(client, "list_agents")
                if scenario == "unregistered":
                    opened_project_id = str(uuid4())
                    assert opened_project_id not in {p["project_id"] for p in projects}
                    # No automatic registration or delegate call follows discovery.
                else:
                    selected = next(w for w in workers if w["worker_id"] == "cloud")
                    operator_authorized_workers = {"home-i5"}
                    assert selected["worker_id"] not in operator_authorized_workers
                    # Discovery does not authorize cloud data egress or paid use.
            assert setup.app.status().queued_tasks == 0
            assert setup.app.status().running_tasks == 0
            assert not setup.provider.requests
            assert len(setup.registry.list_projects()) == 1

    run(execute())


def test_scripted_bounded_consultation_explicit_selection_and_observation(setup):
    async def execute():
        setup.provider.release.clear()
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            assert (await call(client, "agentforge_status"))["available"]
            await call(client, "describe_capabilities")
            projects = (await call(client, "list_projects"))["projects"]
            workers = (await call(client, "list_workers"))["workers"]
            agents = (await call(client, "list_agents"))["agents"]
            # Authorized IDs are supplied by the scripted Director; no ranking.
            assert setup.binding["project_id"] in {p["project_id"] for p in projects}
            assert setup.binding["worker_id"] in {w["worker_id"] for w in workers}
            assert setup.binding["agent_id"] in {a["agent_id"] for a in agents}
            missing_worker = {
                k: v for k, v in setup.binding.items() if k != "worker_id"
            }
            await error(client, "delegate_task", missing_worker, "invalid_arguments")
            await error(
                client,
                "delegate_task",
                {**setup.binding, "worker_id": "missing"},
                "worker_not_found",
            )
            await error(
                client,
                "delegate_task",
                {**setup.binding, "project_id": str(uuid4())},
                "project_not_found",
            )
            task = await call(client, "delegate_task", setup.binding)
            assert task["state"] == "queued"
            assert task["worker_id"] == "home-i5"
            await setup.provider.entered.wait()
            arguments = {"task_id": task["task_id"]}
            observed = await call(client, "watch_task", arguments)
            assert observed["state"] == "running"
            assert (await call(client, "get_task", arguments))["state"] == "running"
            assert [worker.id for worker in setup.provider.workers] == ["home-i5"]
            setup.provider.release.set()
            for _ in range(100):
                completed = await call(client, "get_task", arguments)
                if completed["state"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert completed["state"] == "completed"
            assert completed["final_answer"] == "Evidence found."
            # A fixture answer is delegated output, not independently verified fact.
            assert len(setup.provider.requests) == 1

    run(execute())


def test_scripted_unreachable_mcp_falls_back_without_submission():
    from unittest.mock import AsyncMock

    from mcp import ClientSession

    async def execute():
        client = AsyncMock(spec=ClientSession)
        client.call_tool.side_effect = ConnectionError("Synthetic disconnected MCP")
        try:
            await client.call_tool("agentforge_status", {})
        except ConnectionError:
            outcome = "continue direct Codex execution"
        assert outcome == "continue direct Codex execution"
        client.call_tool.assert_awaited_once_with("agentforge_status", {})

    run(execute())
