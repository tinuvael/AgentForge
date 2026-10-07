"""Council tools through actual MCP registration and SDK client calls."""

from uuid import uuid4

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agentforge.core.inference import GenerationResult, ToolCall
from agentforge.core.provider_errors import ProviderTimeout
from agentforge.mcp.server import create_server
from agentforge.tasks.models import TaskStorageError
from tests.test_councils import arguments, finish, row_counts
from tests.test_mcp import PRIVATE, call, error, run
from tests.test_mcp import setup as setup_fixture

setup = setup_fixture


def test_discovery_prompt_submission_running_completion_and_privacy(setup):
    async def execute():
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
            GenerationResult(content="Public opinion", model="fake", reasoning=PRIVATE),
            GenerationResult(
                content="Another opinion", model="fake", reasoning=PRIVATE
            ),
        ]
        server = create_server(lambda: setup.app)
        async with create_connected_server_and_client_session(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            tool = tools["delegate_council"]
            assert set(tool.inputSchema["required"]) == {
                "project_id",
                "agent_id",
                "task",
                "worker_ids",
            }
            workers = tool.inputSchema["properties"]["worker_ids"]
            assert workers["minItems"] == 2 and workers["maxItems"] == 16
            assert "default" not in workers
            assert (
                not tool.annotations.idempotentHint
                and not tool.annotations.readOnlyHint
            )
            assert tools["cancel_council"].annotations.idempotentHint
            capabilities = await call(client, "describe_capabilities")
            assert capabilities["council_operations"] == [
                "delegate_council",
                "get_council",
                "cancel_council",
            ]
            assert capabilities["council_max_participants"] == 16
            submitted = await call(client, "delegate_council", arguments(setup))
            assert not submitted["terminal"]
            await setup.provider.entered.wait()
            identity = {"council_id": submitted["council_id"]}
            during = await call(client, "get_council", identity)
            assert during["participant_counts"]["running"] == 1
            assert during["participant_counts"]["queued"] == 1
            setup.provider.release.set()
            council = setup.app.councils.get(submitted["council_id"])
            await finish(setup.app, council)
            done = await call(client, "get_council", identity)
            assert done["terminal"] and done["participant_counts"]["completed"] == 2
            assert [p["worker_id"] for p in done["participants"]] == [
                "ai395",
                "home-i5",
            ]
            assert PRIVATE not in str([during, done])
            assert "trace" not in str(done) and "request" not in done
            assert all(
                p["provider"] == "fake" and p["model"] == "configured-model"
                for p in done["participants"]
            )
            assert await call(client, "cancel_council", identity) == done

    run(execute())


def test_mcp_mixed_failure_then_cancellation(setup):
    async def execute():
        setup.provider.turns = [
            ProviderTimeout(PRIVATE),
            GenerationResult(content="Opinion", model="fake"),
        ]
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            submitted = await call(client, "delegate_council", arguments(setup))
            await finish(setup.app, setup.app.councils.get(submitted["council_id"]))
            result = await call(
                client, "get_council", {"council_id": submitted["council_id"]}
            )
            assert result["terminal"]
            assert (
                result["participant_counts"]["completed"]
                == result["participant_counts"]["failed"]
                == 1
            )
            assert any(
                p["error_code"] == "provider_timeout" for p in result["participants"]
            )
            assert PRIVATE not in str(result)
            setup.provider.release.clear()
            setup.provider.entered.clear()
            second = await call(client, "delegate_council", arguments(setup))
            await setup.provider.entered.wait()
            identity = {"council_id": second["council_id"]}
            cancelled = await call(client, "cancel_council", identity)
            assert cancelled["participant_counts"]["cancelled"] == 1
            assert cancelled["participant_counts"]["running"] == 1
            assert not cancelled["terminal"]
            assert await call(client, "cancel_council", identity) == cancelled
            setup.provider.release.set()
            await finish(setup.app, setup.app.councils.get(second["council_id"]))
            assert (await call(client, "get_council", identity))["participant_counts"][
                "cancelled"
            ] == 2

    run(execute())


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"worker_ids": ["ai395", "ai395"]}, "invalid_arguments"),
        ({"worker_ids": []}, "invalid_arguments"),
        ({"worker_ids": [str(i) for i in range(17)]}, "invalid_arguments"),
        ({"worker_ids": ["ai395", "missing"]}, "worker_not_found"),
        ({"project_id": str(uuid4())}, "project_not_found"),
        ({"agent_id": "missing"}, "agent_not_found"),
        ({"task": " "}, "invalid_arguments"),
    ],
)
def test_mcp_safe_errors_and_no_partial_submission(setup, overrides, code):
    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            await error(client, "delegate_council", arguments(setup, **overrides), code)
            assert row_counts(setup) == (0, 0, 0)
            await error(client, "delegate_council", setup.binding, "invalid_arguments")
            await error(
                client, "get_council", {"council_id": str(uuid4())}, "council_not_found"
            )
            await error(
                client,
                "cancel_council",
                {"council_id": str(uuid4())},
                "council_not_found",
            )

    run(execute())


def test_mcp_storage_and_service_errors(setup, monkeypatch):
    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:

            def fail(**_):
                raise TaskStorageError(PRIVATE)

            with monkeypatch.context() as patch:
                patch.setattr(setup.app.tasks._repository, "add_council", fail)
                await error(
                    client, "delegate_council", arguments(setup), "storage_unavailable"
                )
            with monkeypatch.context() as patch:
                patch.setattr(
                    type(setup.app.tasks), "available", property(lambda _: False)
                )
                await error(
                    client, "delegate_council", arguments(setup), "service_unavailable"
                )
            assert row_counts(setup) == (0, 0, 0)

    run(execute())
