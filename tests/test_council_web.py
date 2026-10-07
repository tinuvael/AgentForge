"""Offline Council dashboard, shared CSRF controls and actual bounded SSE."""

import asyncio
from contextlib import ExitStack
from uuid import uuid4

import pytest

from agentforge.core.inference import GenerationResult, ToolCall
from agentforge.core.provider_errors import ProviderTimeout
from agentforge.tasks.observation import QUEUE_LIMIT, SUBSCRIBER_LIMIT, TaskObserver
from tests.test_councils import arguments, finish
from tests.test_mcp import PRIVATE, run
from tests.test_mcp import setup as setup_fixture
from tests.test_web import LiveConnection, client_for, csrf

setup = setup_fixture


def test_history_detail_task_links_escaping_mixed_states_telemetry(setup):
    async def execute():
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
            GenerationResult(
                content="Opinion <script>alert('x')</script>",
                model="fake",
                reasoning=PRIVATE,
            ),
            ProviderTimeout(PRIVATE),
        ]
        async with client_for(setup.app) as (client, _):
            assert "No Councils" in (await client.get("/councils")).text
            assert 'href="/councils"' in (await client.get("/")).text
            council = setup.app.councils.submit(
                **arguments(setup, task="Review <script>bad()</script>")
            )
            await finish(setup.app, council)
            history = await client.get("/councils")
            assert (
                history.status_code == 200 and str(council.council_id) in history.text
            )
            assert "completed: 1" in history.text and "failed: 1" in history.text
            body = (await client.get(f"/councils/{council.council_id}")).text
            assert "Review &lt;script&gt;" in body and "Opinion &lt;script&gt;" in body
            assert "provider_timeout" in body and "configured-model" in body
            assert "recorded" in body and "partial / incomplete" in body
            assert (
                PRIVATE not in body
                and "api_key" not in body
                and "http://192.168" not in body
            )
            assert body.index("Participant 1 · ai395") < body.index(
                "Participant 2 · home-i5"
            )
            for p in council.participants:
                assert f'href="/tasks/{p.task_id}"' in body
                task_body = (await client.get(f"/tasks/{p.task_id}")).text
                assert f'href="/councils/{council.council_id}"' in task_body
            tasks = (await client.get("/tasks")).text
            assert all(str(p.task_id) in tasks for p in council.participants)
            assert not any(
                word in body.lower()
                for word in ["winner", "consensus", "ranking", "score"]
            )

    run(execute())


@pytest.mark.parametrize(
    "params", ["limit=0", "limit=101", "offset=-1", "offset=1000001", "limit=1&limit=2"]
)
def test_bounded_history(setup, params):
    async def execute():
        async with client_for(setup.app) as (client, _):
            assert (await client.get("/councils?" + params)).status_code == 422

    run(execute())


def test_history_pagination_and_confirmed_csrf_cancellation(setup):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, _):
            councils = [setup.app.councils.submit(**arguments(setup)) for _ in range(3)]
            page = await client.get("/councils?limit=2")
            assert "Next page" in page.text
            assert (
                "Previous page" in (await client.get("/councils?limit=2&offset=2")).text
            )
            current = councils[-1]
            url = f"/councils/{current.council_id}"
            response = await client.get(url)
            assert "Cancel remaining participants" in response.text
            token = csrf(response)
            assert (await client.get(url + "/cancel")).status_code == 405
            for data, headers in [
                ({"csrf": token}, {}),
                ({"csrf": "bad", "confirm": "yes"}, {}),
                ({"csrf": token, "confirm": "yes"}, {"Origin": "https://evil.invalid"}),
                ({"csrf": token, "confirm": "yes"}, {"sec-fetch-site": "cross-site"}),
            ]:
                assert (
                    await client.post(url + "/cancel", data=data, headers=headers)
                ).status_code == 403
            response = await client.post(
                url + "/cancel", data={"csrf": token, "confirm": "yes"}
            )
            assert response.status_code == 303 and response.headers["location"] == url
            response = await client.post(
                url + "/cancel",
                data={"csrf": token, "confirm": "yes"},
                headers={"HX-Request": "true"},
            )
            assert (
                response.status_code == 200 and 'id="council-detail"' in response.text
            )
            assert setup.app.councils.get(current.council_id).terminal
            for council in councils:
                setup.app.councils.cancel(council.council_id)
            setup.provider.release.set()

    run(execute())


@pytest.mark.parametrize("suffix", ["", "/fragment", "/events"])
@pytest.mark.parametrize("identity,code", [("malformed", 422), (str(uuid4()), 404)])
def test_missing_ids_safe_errors(setup, suffix, identity, code):
    async def execute():
        async with client_for(setup.app) as (client, _):
            response = await client.get(f"/councils/{identity}{suffix}")
            assert response.status_code == code and PRIVATE not in response.text

    run(execute())


def test_live_updates_individual_terminal_does_not_close_group(setup, monkeypatch):
    async def execute():
        setup.provider.release.clear()
        first_worker = None
        original = setup.provider.generate
        second_started = asyncio.Event()

        async def generate(worker, request):
            nonlocal first_worker
            if first_worker is None:
                first_worker = worker.id
            else:
                setup.provider.release.clear()
                second_started.set()
            return await original(worker, request)

        monkeypatch.setattr(setup.provider, "generate", generate)
        async with client_for(setup.app) as (client, web):
            council = setup.app.councils.submit(**arguments(setup))
            await setup.provider.entered.wait()
            path = f"/councils/{council.council_id}"
            async with LiveConnection(web, path + "/events") as live:
                assert await live.expect("resync") == b"event: resync\ndata: {}\n\n"
                assert setup.app.tasks.observer.subscriber_count == 1
                setup.provider.release.set()
                await second_started.wait()
                await live.expect("refresh")
                body = (await client.get(path + "/fragment")).text
                assert "completed: 1" in body and "running: 1" in body
                assert not setup.app.councils.get(council.council_id).terminal
                setup.provider.release.set()
                assert await live.expect("terminal") == b"event: terminal\ndata: {}\n\n"
            assert setup.app.tasks.observer.subscriber_count == 0
            wire = b"".join(m.get("body", b"") for m in live.messages)
            assert PRIVATE.encode() not in wire
            assert (
                await client.get(path + "/events")
            ).text == "event: terminal\ndata: {}\n\n"

    run(execute())


def test_live_disconnect_shutdown_capacity_and_slow_consumer(setup):
    async def execute():
        setup.provider.release.clear()
        async with client_for(setup.app) as (client, web):
            council = setup.app.councils.submit(**arguments(setup))
            await setup.provider.entered.wait()
            path = f"/councils/{council.council_id}/events"
            async with LiveConnection(web, path) as live:
                await live.expect("resync")
            assert setup.app.tasks.observer.subscriber_count == 0
            with ExitStack() as stack:
                for _ in range(SUBSCRIBER_LIMIT):
                    stack.enter_context(
                        setup.app.tasks.observer.subscribe_many(
                            tuple(p.task_id for p in council.participants)
                        )
                    )
                assert (await client.get(path)).status_code == 503
            async with LiveConnection(web, path) as live:
                await live.expect("resync")
                await setup.app.close()
                await live.expect("shutdown")
            assert setup.app.tasks.observer.subscriber_count == 0
        observer = TaskObserver()
        ids = (uuid4(), uuid4())
        with observer.subscribe_many(ids) as queue:
            for _ in range(1000):
                observer.notify(ids[0])
                observer.notify(ids[1])
            assert queue.qsize() <= QUEUE_LIMIT
            observer.finish(ids[0])
            assert queue.qsize() <= QUEUE_LIMIT
        assert observer.subscriber_count == 0 and not observer._subscribers

    run(execute())
