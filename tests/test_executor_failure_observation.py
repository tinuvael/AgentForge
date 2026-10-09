"""Real SQLite terminal commit failure must stop truthful active/queued watches."""

import asyncio
from contextlib import AsyncExitStack

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agentforge.application.errors import ServiceError
from agentforge.mcp.server import create_server
from agentforge.tasks.models import TaskStorageError
from agentforge.web.app import create_app
from tests.sqlite_busy import blocked_commit, durable_tasks
from tests.test_director_progress import until
from tests.test_mcp import PRIVATE, call, run
from tests.test_mcp import setup as setup_fixture
from tests.test_web import LiveConnection

setup = setup_fixture


async def fail_terminal_commit(setup):
    with blocked_commit(setup.app.database):
        setup.provider.release.set()
        results = await asyncio.gather(*setup.app.tasks._loops, return_exceptions=True)
        assert len(results) == 1 and isinstance(results[0], TaskStorageError)
    assert setup.app.database.pool.checkedout() == 0
    assert {row[1] for row in durable_tasks(setup.app.database)} == {
        "running",
        "queued",
    }


async def close_failed(app):
    with pytest.raises(TaskStorageError):
        await app.close()


def test_active_and_queued_watch_end_on_executor_failure(setup):
    async def execute():
        app = setup.app
        setup.provider.release.clear()
        await app.start()
        active = app.delegate_task(**setup.binding).task_id
        await setup.provider.entered.wait()
        queued = app.delegate_task(**setup.binding).task_id
        try:
            async with AsyncExitStack() as stack:
                watches = [
                    await stack.enter_async_context(app.watch_task(task_id=identity))
                    for identity in (active, queued)
                ]
                assert [(await anext(w)).state for w in watches] == [
                    "running",
                    "queued",
                ]
                hints = stack.enter_context(app.tasks.observer.subscribe(active))
                await fail_terminal_commit(setup)
                notices = [hints.get_nowait() for _ in range(hints.qsize())]
                assert "terminal" not in notices and "unavailable" in notices
                for watch, state in zip(watches, ("running", "queued"), strict=True):
                    updates = [item async for item in watch]
                    assert updates[-1].observation == "unavailable"
                    assert updates[-1].resync_required and not updates[-1].terminal
                    assert updates[-1].state == state
                    assert all(item.observation != "terminal" for item in updates)
                assert (
                    app.task_progress(
                        task_id=active, observation="terminal"
                    ).observation
                    == "resync"
                )
            assert app.tasks.observer.subscriber_count == 0
            with pytest.raises(ServiceError, match="service_unavailable"):
                async with app.watch_task(task_id=queued):
                    pytest.fail("Unavailable executor must reject a new active watch")
        finally:
            await close_failed(app)

    run(execute())


def test_queued_watch_ends_when_claim_commit_stops_executor(setup):
    async def execute():
        app = setup.app
        await app.start()
        queued = app.delegate_task(**setup.binding).task_id
        try:
            async with app.watch_task(task_id=queued) as watch:
                assert (await anext(watch)).state == "queued"
                # No yield before the reader is established: the first executor
                # claim, rather than terminal persistence, fails its real COMMIT.
                with blocked_commit(app.database):
                    results = await asyncio.gather(
                        *app.tasks._loops, return_exceptions=True
                    )
                    assert len(results) == 1 and isinstance(
                        results[0], TaskStorageError
                    )
                updates = [item async for item in watch]
                assert updates[-1].observation == "unavailable"
                assert updates[-1].state == "queued" and not updates[-1].terminal
            assert app.tasks.observer.subscriber_count == 0
            assert not setup.provider.requests
            assert app.database.pool.checkedout() == 0
            assert [row[1] for row in durable_tasks(app.database)] == ["queued"]
        finally:
            await close_failed(app)

    run(execute())


def test_mcp_progress_returns_safe_unavailable_for_active_and_queued(setup):
    async def execute():
        setup.provider.release.clear()
        with pytest.raises(ExceptionGroup) as stopped:
            async with create_connected_server_and_client_session(
                create_server(lambda: setup.app)
            ) as client:
                active = (await call(client, "delegate_task", setup.binding))["task_id"]
                await setup.provider.entered.wait()
                queued = (await call(client, "delegate_task", setup.binding))["task_id"]
                received = {active: [], queued: []}

                def callback(identity):
                    async def progress(_count, _total, message):
                        received[identity].append(message)

                    return progress

                watches = [
                    asyncio.create_task(
                        client.call_tool(
                            "watch_task",
                            {"task_id": identity},
                            progress_callback=callback(identity),
                        )
                    )
                    for identity in (active, queued)
                ]
                await until(lambda: all(received.values()))
                await fail_terminal_commit(setup)
                for watch, state in zip(watches, ("running", "queued"), strict=True):
                    result = await watch
                    assert not result.isError
                    assert result.structuredContent["observation"] == "unavailable"
                    assert result.structuredContent["state"] == state
                    assert not result.structuredContent["terminal"]
                    assert PRIVATE not in result.model_dump_json()
                assert setup.app.tasks.observer.subscriber_count == 0
        storage_failure, unexpected = stopped.value.split(TaskStorageError)
        assert storage_failure is not None and unexpected is None

    run(execute())


def test_http_task_and_council_streams_end_on_executor_failure(setup):
    async def execute():
        setup.provider.release.clear()
        await setup.app.start()
        web = create_app(shared_application=setup.app)
        try:
            async with web.router.lifespan_context(web):
                council = setup.app.delegate_council(
                    project_id=setup.project.id,
                    agent_id="repo_explorer",
                    worker_ids=("home-i5", "local-4080"),
                    task="Inspect evidence",
                )
                await setup.provider.entered.wait()
                identities = [p.task_id for p in council.participants]
                paths = [
                    f"{prefix}/tasks/{identity}/events"
                    for prefix in ("", "/companion")
                    for identity in identities
                ] + [
                    f"{prefix}/councils/{council.council_id}/events"
                    for prefix in ("", "/companion")
                ]
                async with AsyncExitStack() as stack:
                    streams = [
                        await stack.enter_async_context(LiveConnection(web, path))
                        for path in paths
                    ]
                    for stream in streams:
                        await stream.expect("resync")
                    await fail_terminal_commit(setup)
                    for stream in streams:
                        await stream.expect("unavailable")
                        await stream.task
                        assert "event: terminal" not in str(stream.messages)
                assert setup.app.tasks.observer.subscriber_count == 0
        finally:
            await close_failed(setup.app)

    run(execute())
