"""Bounded Application watches and official SDK request-scoped progress, offline."""

import asyncio
import json
from contextlib import ExitStack
from uuid import UUID, uuid4

import pytest
from mcp import types
from mcp.server.session import ServerSession
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError

from agentforge.agents.models import TraceEvent
from agentforge.application.contracts import TaskProgress
from agentforge.application.errors import ServiceError
from agentforge.core.inference import GenerationResult, ToolCall
from agentforge.mcp.server import create_server
from agentforge.tasks.models import TaskNotFound
from agentforge.tasks.observation import QUEUE_LIMIT, SUBSCRIBER_LIMIT, TRACE_LIMIT
from tests.test_mcp import PRIVATE, call, error, run
from tests.test_mcp import setup as setup_fixture

setup = setup_fixture


async def until(predicate):
    # Only cooperatively yield; the shared run() helper bounds the whole test.
    while not predicate():
        await asyncio.sleep(0)


async def running(setup):
    setup.provider.release.clear()
    await setup.app.start()
    task_id = setup.app.delegate_task(**setup.binding).task_id
    await setup.provider.entered.wait()
    return task_id


def test_queued_subscription_cancellation_and_unknown_identity(setup):
    async def execute():
        app = setup.app
        identity = app.tasks.submit(**setup.binding).task_id
        try:
            async with app.watch_task(task_id=identity) as updates:
                first = await anext(updates)
                assert first.state == "queued" and first.resync_required
                assert first.observation == "resync" and not first.terminal
                assert first.timeline == ()
                app.cancel_task(task_id=identity)
                last = await anext(updates)
                assert last.terminal and last.state == "cancelled"
                assert last.cancellation_requested and last.reason == "cancelled"
                with pytest.raises(StopAsyncIteration):
                    await anext(updates)
            assert app.tasks.observer.subscriber_count == 0
            with pytest.raises(TaskNotFound):
                async with app.watch_task(task_id=uuid4()):
                    pytest.fail("Unknown identity must not subscribe")
            assert app.tasks.observer.subscriber_count == 0
        finally:
            await app.close()

    run(execute())


def test_running_reconnect_and_dashboard_share_timeline(setup):
    async def execute():
        identity = await running(setup)
        app = setup.app
        try:
            for _ in range(2):
                async with app.watch_task(task_id=identity) as updates:
                    first = await anext(updates)
                    assert first.state == "running" and first.resync_required
                    assert first.timeline[-1].kind == "model_request"
                    assert first.timeline == app.dashboard.detail(identity).timeline
                assert app.tasks.observer.subscriber_count == 0
            setup.provider.release.set()
            await app.tasks.wait_task(identity)
            # Already-terminal watch also works after the observer is closed.
            await app.tasks.close()
            async with app.watch_task(task_id=identity) as updates:
                final = await anext(updates)
                assert final.terminal and final.state == "completed"
                assert final.observation == "terminal" and final.resync_required
                assert app.tasks.observer.subscriber_count == 0
                assert final.timeline == app.dashboard.detail(identity).timeline
                with pytest.raises(StopAsyncIteration):
                    await anext(updates)
        finally:
            await app.close()

    run(execute())


def test_backpressure_truncation_and_multiple_watchers(setup):
    async def execute():
        identity = await running(setup)
        app = setup.app
        hub = app.tasks.observer
        try:
            async with (
                app.watch_task(task_id=identity) as slow,
                app.watch_task(task_id=identity) as fast,
            ):
                await anext(slow)
                await anext(fast)
                assert hub.subscriber_count == 2
                for step in range(QUEUE_LIMIT + 1):
                    hub.record(identity, TraceEvent(step=step, kind="model_request"))
                    assert not (await anext(fast)).resync_required
                overflow = await anext(slow)
                assert overflow.resync_required and overflow.observation == "resync"
                for step in range(TRACE_LIMIT * 2):
                    hub.record(identity, TraceEvent(step=step, kind="model_request"))
                truncated = await anext(slow)
                assert truncated.truncated and truncated.resync_required
                assert len(truncated.timeline) == TRACE_LIMIT
                assert truncated.timeline[0].step == TRACE_LIMIT
                # Real execution must finish even if neither observer drains again.
                setup.provider.release.set()
                assert (await app.tasks.wait_task(identity)).state == "completed"
            assert hub.subscriber_count == 0
        finally:
            await app.close()

    run(execute())


def test_subscriber_limit_is_safe_and_released(setup):
    async def execute():
        identity = await running(setup)
        hub = setup.app.tasks.observer
        try:
            with ExitStack() as subscriptions:
                for _ in range(SUBSCRIBER_LIMIT):
                    subscriptions.enter_context(hub.subscribe(identity))
                with pytest.raises(ServiceError, match="service_unavailable"):
                    async with setup.app.watch_task(task_id=identity):
                        pytest.fail("Watch must respect shared dashboard capacity")
                assert hub.subscriber_count == SUBSCRIBER_LIMIT
            async with setup.app.watch_task(task_id=identity) as updates:
                assert (await anext(updates)).state == "running"
            assert hub.subscriber_count == 0
        finally:
            await setup.app.close()

    run(execute())


def test_watch_cancellation_releases_subscription_without_cancelling_task(setup):
    async def execute():
        identity = await running(setup)
        ready = asyncio.Event()

        async def consume():
            async with setup.app.watch_task(task_id=identity) as updates:
                await anext(updates)
                ready.set()
                await anext(updates)

        watcher = asyncio.create_task(consume())
        try:
            await ready.wait()
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
            assert setup.app.tasks.observer.subscriber_count == 0
            current = setup.app.get_task(task_id=identity)
            assert current.state == "running" and not current.cancellation_requested_at
            setup.provider.release.set()
            assert (await setup.app.tasks.wait_task(identity)).state == "completed"
        finally:
            await setup.app.close()

    run(execute())


def test_running_task_cancellation_terminates_watch(setup):
    async def execute():
        identity = await running(setup)
        try:
            async with setup.app.watch_task(task_id=identity) as updates:
                await anext(updates)
                setup.app.cancel_task(task_id=identity)
                pending = await anext(updates)
                assert pending.state == "running" and pending.cancellation_requested
                setup.provider.release.set()
                final = [item async for item in updates][-1]
                assert final.terminal and final.state == "cancelled"
                assert final.reason == "cancelled"
            assert setup.app.tasks.observer.subscriber_count == 0
        finally:
            await setup.app.close()

    run(execute())


def test_executor_shutdown_closes_active_and_queued_watches(setup):
    async def execute():
        identity = await running(setup)
        queued = setup.app.delegate_task(**setup.binding).task_id
        try:
            async with (
                setup.app.watch_task(task_id=identity) as active,
                setup.app.watch_task(task_id=queued) as waiting,
            ):
                await anext(active)
                await anext(waiting)
                await setup.app.tasks.close()
                last = [item async for item in active][-1]
                assert last.observation == "shutdown" and last.resync_required
                assert (
                    last.state == "failed" and last.error_code == "executor_cancelled"
                )
                queued_last = [item async for item in waiting][-1]
                assert queued_last.observation == "shutdown"
                assert not queued_last.terminal and queued_last.state == "queued"
            assert setup.app.tasks.observer.subscriber_count == 0
        finally:
            await setup.app.close()

    run(execute())


def test_mcp_official_progress_delivery_and_safe_tool_projection(setup):
    setup.provider.release.clear()
    setup.provider.turns = [
        GenerationResult(
            content=PRIVATE,
            model="fake",
            reasoning=PRIVATE,
            tool_calls=[
                ToolCall(id=PRIVATE, name="read_file", arguments={"path": "source.py"})
            ],
        ),
        GenerationResult(content="Final answer.", model="fake", reasoning=PRIVATE),
    ]

    async def execute():
        received = []
        ready = asyncio.Event()

        async def progress(count, total, message):
            received.append((count, total, TaskProgress.model_validate_json(message)))
            ready.set()

        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            submitted = await call(client, "delegate_task", setup.binding)
            assert submitted["state"] == "queued"
            identity = submitted["task_id"]
            await setup.provider.entered.wait()
            watcher = asyncio.create_task(
                client.call_tool(
                    "watch_task", {"task_id": identity}, progress_callback=progress
                )
            )
            await ready.wait()
            assert received[0][2].state == "running" and received[0][2].resync_required
            assert not watcher.done()
            # Another MCP request works while the watch remains outstanding.
            assert (await call(client, "get_task", {"task_id": identity}))[
                "state"
            ] == "running"
            setup.provider.release.set()
            result = await watcher
            assert not result.isError
            latest = TaskProgress.model_validate(result.structuredContent)
            assert latest.terminal and latest.state == "completed"
            await until(lambda: received[-1][2].terminal)
            assert received[-1][2] == latest
            assert [item[0] for item in received] == list(range(1, len(received) + 1))
            assert all(total is None for _, total, _ in received)
            events = latest.timeline
            tool = next(e for e in events if e.kind == "tool_result")
            assert tool.tool_name == "read_file" and tool.success
            assert tool.duration_seconds >= 0 and tool.step == 1
            assert PRIVATE not in result.model_dump_json()
            assert all(PRIVATE not in item.model_dump_json() for _, _, item in received)
            assert "source.py" not in result.model_dump_json()
            assert "Final answer." not in result.model_dump_json()
            assert "arguments" not in result.model_dump_json()
            assert setup.app.tasks.observer.subscriber_count == 0
            assert (await call(client, "get_task", {"task_id": identity}))[
                "final_answer"
            ] == "Final answer."

    run(execute())


def test_mcp_no_token_prompt_snapshot_terminal_and_unknown(setup):
    setup.provider.release.clear()

    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            identity = (await call(client, "delegate_task", setup.binding))["task_id"]
            await setup.provider.entered.wait()
            latest = await call(client, "watch_task", {"task_id": identity})
            assert latest["state"] == "running" and latest["resync_required"]
            assert setup.app.tasks.observer.subscriber_count == 0
            await error(
                client, "watch_task", {"task_id": str(uuid4())}, "task_not_found"
            )
            await error(client, "watch_task", {"task_id": PRIVATE}, "invalid_arguments")
            setup.provider.release.set()
            await setup.app.tasks.wait_task(identity)
            received = []

            async def progress(_count, _total, message):
                received.append(json.loads(message))

            terminal = await client.call_tool(
                "watch_task", {"task_id": identity}, progress_callback=progress
            )
            await until(lambda: bool(received))
            assert received == [terminal.structuredContent]
            assert received[0]["terminal"]
            assert setup.app.tasks.observer.subscriber_count == 0

    run(execute())


def test_mcp_protocol_cancel_only_releases_watch(setup):
    setup.provider.release.clear()

    async def execute():
        tokens = []
        ready = asyncio.Event()

        async def messages(message):
            if isinstance(message, types.ServerNotification) and isinstance(
                message.root, types.ProgressNotification
            ):
                # SDK 1.30 call_tool(progress_callback=...) uses request ID as token.
                tokens.append(message.root.params.progressToken)
                ready.set()

        async def progress(*_):
            pass

        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app), message_handler=messages
        ) as client:
            identity = (await call(client, "delegate_task", setup.binding))["task_id"]
            await setup.provider.entered.wait()
            watcher = asyncio.create_task(
                client.call_tool(
                    "watch_task", {"task_id": identity}, progress_callback=progress
                )
            )
            await ready.wait()
            await client.send_notification(
                types.ClientNotification(
                    types.CancelledNotification(
                        params=types.CancelledNotificationParams(requestId=tokens[0])
                    )
                )
            )
            with pytest.raises(McpError, match="Request cancelled"):
                await watcher
            await until(lambda: setup.app.tasks.observer.subscriber_count == 0)
            assert not setup.app.get_task(
                task_id=UUID(identity)
            ).cancellation_requested_at
            setup.provider.release.set()
            assert (await setup.app.tasks.wait_task(identity)).state == "completed"

    run(execute())


def test_mcp_disconnect_cleans_watch_and_executor(setup):
    setup.provider.release.clear()

    async def execute():
        ready = asyncio.Event()

        async def progress(*_):
            ready.set()

        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            identity = (await call(client, "delegate_task", setup.binding))["task_id"]
            await setup.provider.entered.wait()
            watcher = asyncio.create_task(
                client.call_tool(
                    "watch_task", {"task_id": identity}, progress_callback=progress
                )
            )
            await ready.wait()
            assert setup.app.tasks.observer.subscriber_count == 1
        # The official SDK cancels outstanding handlers on transport/server exit.
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        assert setup.app.tasks.observer.subscriber_count == 0
        assert (
            setup.app.get_task(task_id=UUID(identity)).error_code
            == "executor_cancelled"
        )
        assert setup.provider.cleaned == 1

    run(execute())


def test_mcp_delivery_failure_cannot_fail_execution(setup, monkeypatch):
    setup.provider.release.clear()

    async def broken(*_args, **_kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(ServerSession, "send_progress_notification", broken)

    async def execute():
        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            identity = (await call(client, "delegate_task", setup.binding))["task_id"]
            await setup.provider.entered.wait()
            result = await client.call_tool(
                "watch_task", {"task_id": identity}, meta={"progressToken": "director"}
            )
            assert result.isError and PRIVATE not in result.model_dump_json()
            assert json.loads(result.content[0].text)["code"] == "internal_error"
            assert setup.app.tasks.observer.subscriber_count == 0
            setup.provider.release.set()
            assert (await setup.app.tasks.wait_task(identity)).state == "completed"

    run(execute())


def test_mcp_simultaneous_tokens_are_request_local(setup):
    setup.provider.release.clear()

    async def execute():
        received = {0: [], "second": []}

        async def messages(message):
            if isinstance(message, types.ServerNotification) and isinstance(
                message.root, types.ProgressNotification
            ):
                params = message.root.params
                received[params.progressToken].append(json.loads(params.message))

        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app), message_handler=messages
        ) as client:
            first = (await call(client, "delegate_task", setup.binding))["task_id"]
            await setup.provider.entered.wait()
            second = (await call(client, "delegate_task", setup.binding))["task_id"]
            watches = [
                asyncio.create_task(
                    client.call_tool(
                        "watch_task",
                        {"task_id": identity},
                        meta={"progressToken": token},
                    )
                )
                for identity, token in ((first, 0), (second, "second"))
            ]
            await until(lambda: all(received.values()))
            assert setup.app.tasks.observer.subscriber_count == 2
            assert received[0][0]["state"] == "running"
            assert received["second"][0]["state"] == "queued"
            await call(client, "cancel_task", {"task_id": second})
            assert (await watches[1]).structuredContent["state"] == "cancelled"
            assert not watches[0].done()
            setup.provider.release.set()
            assert (await watches[0]).structuredContent["state"] == "completed"
            await until(
                lambda: all(items[-1]["terminal"] for items in received.values())
            )
            for token, identity in ((0, first), ("second", second)):
                assert all(item["task_id"] == identity for item in received[token])
            assert setup.app.tasks.observer.subscriber_count == 0

    run(execute())


def test_unconsumed_watch_cleanup_and_contract_bound(setup):
    async def execute():
        identity = await running(setup)
        try:
            async with setup.app.watch_task(task_id=identity):
                assert setup.app.tasks.observer.subscriber_count == 1
            assert setup.app.tasks.observer.subscriber_count == 0
            snapshot = setup.app.task_progress(task_id=identity).model_dump()
            snapshot["timeline"] *= TRACE_LIMIT + 1
            with pytest.raises(ValidationError):
                TaskProgress.model_validate(snapshot)
        finally:
            await setup.app.close()

    run(execute())


@pytest.mark.parametrize("failure", ["tool", "provider"])
def test_mcp_safe_failure_categories(setup, failure):
    setup.provider.release.clear()
    setup.provider.turns = (
        [
            GenerationResult(
                content=PRIVATE,
                model="fake",
                reasoning=PRIVATE,
                tool_calls=[
                    ToolCall(id=PRIVATE, name="read_file", arguments={"path": PRIVATE})
                ],
            ),
        ]
        if failure == "tool"
        else [RuntimeError(PRIVATE)]
    )

    async def execute():
        async def progress(_count, _total, message):
            assert PRIVATE not in message
            setup.provider.release.set()

        async with create_connected_server_and_client_session(
            create_server(lambda: setup.app)
        ) as client:
            identity = (await call(client, "delegate_task", setup.binding))["task_id"]
            await setup.provider.entered.wait()
            result = await client.call_tool(
                "watch_task", {"task_id": identity}, progress_callback=progress
            )
            assert not result.isError and PRIVATE not in result.model_dump_json()
            latest = TaskProgress.model_validate(result.structuredContent)
            assert latest.terminal
            if failure == "tool":
                outcome = next(e for e in latest.timeline if e.kind == "tool_result")
                assert not outcome.success and outcome.error_code == "path_not_found"
                assert latest.state == "completed"
            else:
                assert (
                    latest.state == "failed" and latest.error_code == "provider_error"
                )
            assert setup.app.tasks.observer.subscriber_count == 0

    run(execute())
