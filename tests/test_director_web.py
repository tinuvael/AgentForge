"""Real ASGI chat lifecycle over the shared Director and official MCP SDK."""

import asyncio
import hashlib
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from uuid import uuid4

import httpx
import pytest

from agentforge.application.definitions import shipped_agents
from agentforge.core.inference import GenerationResult
from agentforge.core.provider_errors import BackendUnavailable
from agentforge.web.app import create_app
from agentforge.web.director import COOKIE
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_director import fixture, tool
from tests.test_mcp import setup as setup_fixture

setup = setup_fixture
coding = coding_fixture


def test_real_http_chat_with_concurrent_authenticated_directors():
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/verify_director.py"),
            "--web",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Browser HTTP chat" in result.stdout
    assert "Traceback" not in result.stderr


@asynccontextmanager
async def browser(setup, f):
    @asynccontextmanager
    async def borrowed():
        yield f.director

    web = create_app(shared_application=setup.app, director_factory=borrowed)
    async with web.router.lifespan_context(web):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web), base_url="http://127.0.0.1"
        ) as client:
            yield client, web


def owner(client):
    return hashlib.sha256(client.cookies.get(COOKIE).encode()).hexdigest()


def csrf(client):
    return {"csrf": client.cookies.get("agentforge_csrf")}


async def new(client):
    response = await client.get("/director")
    assert response.status_code == 200
    response = await client.post("/director/new", data=csrf(client))
    assert response.status_code == 303
    path = response.headers["location"]
    assert (await client.get(path)).status_code == 200
    return path


def submission(client, director, path, **values):
    item = director.load(path.split("/")[-1], owner(client))
    return {**csrf(client), "revision": str(item.revision), **values}


async def idle(web):
    async with asyncio.timeout(8):
        while web.state.director.jobs:
            await asyncio.sleep(0.01)


def test_chat_real_task_progress_reconnect_and_safe_rendering(setup, tmp_path):
    async def scenario():
        turns = [
            tool("delegate_task", setup.binding),
            GenerationResult(
                content='<script>alert("model")</script> source.py:1', model="director"
            ),
        ]
        async with fixture(setup, tmp_path, turns=turns) as f:
            async with browser(setup, f) as (client, web):
                path = await new(client)
                assert (
                    await client.post(
                        "/director/new",
                        data=csrf(client),
                        headers={
                            "origin": "http://127.0.0.1",
                            "sec-fetch-site": "same-origin",
                        },
                    )
                ).status_code == 303
                assert (
                    await client.post(
                        "/director/new",
                        data=csrf(client),
                        headers={"origin": "null"},
                    )
                ).status_code == 403
                assert "home-i5" in (await client.get(path)).text
                data = submission(
                    client,
                    f.director,
                    path,
                    message='<img src=x onerror="alert(1)"> inspect source',
                    project=str(setup.project.id),
                    worker="home-i5",
                )
                response = await client.post(path + "/send", data=data)
                assert response.status_code == 303
                assert (await client.post(path + "/send", data=data)).status_code == 409
                progress = await client.get(path + "/fragment")
                assert 'hx-trigger="every 2s"' in progress.text
                assert (
                    "Analyzing" in progress.text
                    or "analyzing" in progress.text
                    or "Discovering" in progress.text
                )
                await idle(web)
                response = await client.get(path)
                assert (
                    "&lt;script&gt;" in response.text
                    and '<script>alert("model")' not in response.text
                )
                assert "&lt;img" in response.text and "<img src=x" not in response.text
                assert "source tools observed" in response.text
                item = f.director.load(path.split("/")[-1], owner(client))
                assert item.state == "completed" and len(item.tasks) == 1
                task_id = item.tasks[0].task_id
                assert f'href="/tasks/{task_id}"' in response.text
                assert (await client.get(f"/tasks/{task_id}")).status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert (
                    "script-src 'self'" in response.headers["content-security-policy"]
                )
                assert len(setup.app.tasks.history()) == 1
                restored = await client.post(
                    path + "/resume", data=submission(client, f.director, path)
                )
                assert restored.status_code == 303
                await idle(web)
                assert len(setup.app.tasks.history()) == 1
                assert (await client.get(path)).status_code == 200
                clear = await client.post(
                    path + "/clear",
                    data=submission(client, f.director, path, confirm="clear"),
                )
                assert (
                    clear.status_code == 303
                    and (await client.get(path)).status_code == 404
                )
                assert len(setup.app.tasks.history()) == 1
            assert setup.app.status().available

    asyncio.run(scenario())


def test_cross_session_origin_host_csrf_and_input_limits(setup, tmp_path):
    async def scenario():
        async with fixture(setup, tmp_path) as f:
            async with browser(setup, f) as (client, web):
                path = await new(client)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=web), base_url="http://127.0.0.1"
                ) as other:
                    assert (await other.get(path)).status_code == 404
                    assert (await other.get(path + "/fragment")).status_code == 404
                    assert (
                        await other.post(
                            path + "/approval", data={"decision": "approve"}
                        )
                    ).status_code == 404
                assert (await client.get("/director/not-a-uuid")).status_code == 422
                assert (await client.get(f"/director/{uuid4()}")).status_code == 404
                assert (
                    await client.get(path, headers={"origin": "https://evil.example"})
                ).status_code == 403
                assert (
                    await client.get(path, headers={"host": "evil.example"})
                ).status_code == 400
                assert (
                    await client.post(path + "/send", data={"message": "hello"})
                ).status_code == 403
                assert (
                    await client.post(
                        path + "/send",
                        data=submission(client, f.director, path, message="x" * 8001),
                    )
                ).status_code == 413
                assert (
                    await client.post(
                        path + "/send",
                        content="x" * 70_000,
                        headers={"content-type": "application/x-www-form-urlencoded"},
                    )
                ).status_code == 413
                assert (
                    await client.post(
                        path + "/send",
                        data=submission(
                            client, f.director, path, message="hello", worker="denied"
                        ),
                    )
                ).status_code == 403
                assert not setup.app.tasks.history()

    asyncio.run(scenario())


def test_coding_approval_cannot_be_bypassed_replayed_or_expired(setup, tmp_path):
    async def scenario():
        binding = setup.binding | {"agent_id": "coder"}
        async with fixture(
            setup,
            tmp_path,
            turns=[tool("delegate_task", binding)],
            agents=shipped_agents(coding_enabled=True),
        ) as f:
            async with browser(setup, f) as (client, web):
                path = await new(client)
                assert (
                    await client.post(
                        path + "/send",
                        data=submission(client, f.director, path, message="Fix source"),
                    )
                ).status_code == 303
                await idle(web)
                page = await client.get(path)
                assert (
                    "Approve this action" in page.text
                    and str(setup.project.id) in page.text
                )
                assert not setup.app.tasks.history()
                item = f.director.load(path.split("/")[-1], owner(client))
                nonce = item.pending.nonce
                assert (
                    await client.post(
                        path + "/approval",
                        data=submission(
                            client, f.director, path, decision="approve", nonce="wrong"
                        ),
                    )
                ).status_code == 409
                item.pending.expires = time() - 1
                f.director.journal.save(item)
                assert (
                    await client.post(
                        path + "/approval",
                        data=submission(
                            client, f.director, path, decision="approve", nonce=nonce
                        ),
                    )
                ).status_code == 409
                item.pending.expires = time() + 30
                f.director.journal.save(item)
                data = submission(
                    client, f.director, path, decision="reject", nonce=nonce
                )
                assert (
                    await client.post(path + "/approval", data=data)
                ).status_code == 303
                await idle(web)
                assert (
                    await client.post(path + "/approval", data=data)
                ).status_code == 409
                assert not setup.app.tasks.history()

    asyncio.run(scenario())


def test_model_failure_and_browser_stop_release_only_director(setup, tmp_path):
    async def scenario():
        async with fixture(setup, tmp_path, turns=[BackendUnavailable("private")]) as f:
            async with browser(setup, f) as (client, web):
                path = await new(client)
                await client.post(
                    path + "/send",
                    data=submission(client, f.director, path, message="Inspect"),
                )
                await idle(web)
                page = await client.get(path)
                assert "backend_unavailable" in page.text and "private" not in page.text
                f.provider.release.clear()
                await client.post(
                    path + "/send",
                    data=submission(client, f.director, path, message="Inspect again"),
                )
                await f.provider.entered.wait()
                item = f.director.load(path.split("/")[-1], owner(client))
                assert (
                    await client.post(
                        path + "/stop", data={**csrf(client), "operation": "stale"}
                    )
                ).status_code == 409
                assert (
                    await client.post(
                        path + "/stop",
                        data={**csrf(client), "operation": str(item.request_id)},
                    )
                ).status_code == 303
                await idle(web)
                assert f.director.load(item.id, owner(client)).state == "cancelled"
            assert setup.app.status().available

    asyncio.run(scenario())


def test_chat_disabled_without_borrowed_gateway_and_for_lan(setup, tmp_path):
    async def scenario():
        standalone = create_app(lambda: setup.app)
        async with standalone.router.lifespan_context(standalone):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=standalone),
                base_url="http://127.0.0.1",
            ) as client:
                assert (await client.get("/director")).status_code == 503
                assert (await client.post("/director/new", data={})).status_code == 503
        with pytest.raises(ValueError, match="borrowed"):
            create_app(lambda: setup.app, director_factory=lambda: None)

    asyncio.run(scenario())


def test_configured_chat_rejects_remote_peer_and_foreign_task_cancellation(
    setup, tmp_path
):
    async def scenario():
        async with fixture(setup, tmp_path) as f:
            async with browser(setup, f) as (client, web):
                path = await new(client)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(
                        app=web, client=("192.168.1.50", 12000)
                    ),
                    base_url="http://127.0.0.1",
                ) as remote:
                    assert (await remote.get("/director")).status_code == 403
                    assert (
                        await remote.post("/director/new", data={})
                    ).status_code == 403
                assert (
                    await client.post(
                        path + "/cancel-task",
                        data=submission(client, f.director, path, task=str(uuid4())),
                    )
                ).status_code == 403
                await client.post(
                    path + "/send",
                    data=submission(client, f.director, path, message="Inspect"),
                )
                await idle(web)
                item = f.director.load(path.split("/")[-1], owner(client))
                task = item.tasks[0]
                assert (
                    await client.post(
                        path + "/cancel-task",
                        data=submission(
                            client, f.director, path, task=str(task.task_id)
                        ),
                    )
                ).status_code == 303
                item = f.director.load(item.id, owner(client))
                assert item.pending.task_id == str(task.task_id)
                assert (
                    await client.post(
                        path + "/approval",
                        data=submission(
                            client,
                            f.director,
                            path,
                            nonce=item.pending.nonce,
                            decision="approve",
                            task=str(uuid4()),
                        ),
                    )
                ).status_code == 422
                assert (
                    await client.post(
                        path + "/approval",
                        data=submission(
                            client,
                            f.director,
                            path,
                            nonce=item.pending.nonce,
                            decision="approve",
                        ),
                    )
                ).status_code == 303
                await idle(web)
                assert setup.app.tasks.get_task(task.task_id).state == "completed"
                assert f.director.load(item.id, owner(client)).pending is None

    asyncio.run(scenario())


def test_approved_coder_changes_only_isolated_workspace(setup, coding, tmp_path):
    setup.project = coding.project
    setup.binding = setup.binding | {
        "project_id": str(coding.project.id),
        "agent_id": "coder",
    }

    async def scenario():
        async with fixture(setup, tmp_path, coding_config=coding.config) as f:
            setup.provider.turns = [
                tool(
                    "write_file",
                    {
                        "path": "new.txt",
                        "content": "approved isolated change\n",
                        "expected_sha256": None,
                    },
                ),
                GenerationResult(
                    content="new.txt created in isolated workspace.",
                    model="configured-model",
                ),
            ]
            async with browser(setup, f) as (client, web):
                path = await new(client)
                await client.post(
                    path + "/send",
                    data=submission(client, f.director, path, message="Create new.txt"),
                )
                await idle(web)
                item = f.director.load(path.split("/")[-1], owner(client))
                assert item.pending and not setup.app.tasks.history()
                await client.post(
                    path + "/approval",
                    data=submission(
                        client,
                        f.director,
                        path,
                        decision="approve",
                        nonce=item.pending.nonce,
                    ),
                )
                await idle(web)
                item = f.director.load(item.id, owner(client))
                assert item.state == "completed", item.error
                assert len(item.tasks) == 1
                report = setup.app.get_coding_workspace(task_id=item.tasks[0].task_id)
                assert (
                    "new.txt"
                    in setup.app.get_coding_diff(
                        task_id=item.tasks[0].task_id
                    ).model_dump_json()
                )
                assert not (coding.root / "new.txt").exists()
                f.director.approval(
                    item,
                    "cleanup_coding_workspace",
                    {
                        "task_id": str(item.tasks[0].task_id),
                        "workspace_id": str(report.workspace_id),
                    },
                    coding.project.id,
                    item.tasks[0].task_id,
                )
                await client.post(
                    path + "/approval",
                    data=submission(
                        client,
                        f.director,
                        path,
                        decision="approve",
                        nonce=item.pending.nonce,
                    ),
                )
                await idle(web)
                assert (
                    setup.app.get_coding_workspace(task_id=item.tasks[0].task_id).state
                    == "removed"
                )

    asyncio.run(scenario())
