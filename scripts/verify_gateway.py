"""Real TCP/SDK multi-client acceptance with scripted inference, no workstation.

Run in an isolated interpreter (the regression network guard remains intact).
Also imported by installed-wheel smoke; every AgentForge import uses that wheel.
"""

import asyncio
import json
import logging
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.engine import URL

from agentforge.application.access import ClientPolicy, Policies, ProjectGrant
from agentforge.application.definitions import shipped_agents
from agentforge.application.service import Application
from agentforge.core.worker import Worker
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.migrate import upgrade_database
from agentforge.db.ownership import BUSY_MESSAGE
from agentforge.db.projects import ProjectRepository
from agentforge.db.tasks import TaskRepository
from agentforge.gateway.auth import CredentialStore, fingerprint, save_policies
from agentforge.gateway.server import GatewayConfig, create_gateway
from agentforge.mcp.server import TOOL_CONTRACTS, _SafeDiagnostics
from agentforge.projects.service import ProjectRegistry
from agentforge.providers.ollama import OllamaProvider
from agentforge.specialists.models import SpecialistConfig, SpecialistTool
from agentforge.web.combined import BorrowedServer, CompanionConfig
from agentforge.workers.config import WorkersConfig


@asynccontextmanager
async def listener(web, *, port=None, tls=None):
    sock = socket.socket()
    sock.bind(("127.0.0.1", port or 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = BorrowedServer(
        uvicorn.Config(
            web,
            access_log=False,
            log_config=None,
            proxy_headers=False,
            ssl_certfile=str(tls[0]) if tls else None,
            ssl_keyfile=str(tls[1]) if tls else None,
            timeout_graceful_shutdown=1,
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("Listener failed startup")
                await asyncio.sleep(0.01)
        yield f"{'https' if tls else 'http'}://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 5)
        finally:
            sock.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def client(url, token, *, verify=True):
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=5,
        verify=verify,
    ) as http:
        async with streamable_http_client(url, http_client=http) as (
            read,
            write,
            session_id,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session, session_id


async def call(session, name, arguments=None, *, error=None, **kwargs):
    result = await session.call_tool(name, arguments or {}, **kwargs)
    if error:
        assert result.isError, result
        value = json.loads(result.content[0].text)
        assert value["code"] == error, value
        return value
    assert not result.isError, result
    return result.structuredContent


class Inference:
    def __init__(self):
        self.release = asyncio.Event()
        self.release.set()
        self.requests = []
        self.active = 0
        self.maximum = 0

    async def __call__(self, request):
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        assert payload["model"] == "synthetic" and not payload["stream"]
        self.requests.append(payload)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await self.release.wait()
            last = payload["messages"][-1]
            if last["role"] == "tool":
                assert "Windows checkout evidence" in last["content"]
                message = {
                    "content": "README.md:1 — Windows checkout evidence verified."
                }
            else:
                message = {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_file",
                                "arguments": {"path": "README.md"},
                            }
                        }
                    ],
                }
            return httpx.Response(
                200,
                json={
                    "model": "synthetic",
                    "done": True,
                    "message": message,
                    "prompt_eval_count": 10,
                    "eval_count": 5,
                },
            )
        finally:
            self.active -= 1


async def wait_running(app, count):
    async with asyncio.timeout(5):
        while app.tasks.active_counts().get("running", 0) != count:
            await asyncio.sleep(0.01)


async def conflicting_executors(database_url, workers):
    for mode in ("mcp", "web"):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agentforge",
            mode,
            "--database-url",
            str(database_url),
            "--workers",
            str(workers),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(b""), 10)
        assert process.returncode != 0 and BUSY_MESSAGE.encode() in err, (
            mode,
            out,
            err,
        )


async def integration(root, *, volatile=False):
    # volatile=True is only a local transport diagnostic on unsupported hosts.
    # CI and installed-wheel acceptance always use persistent storage.
    database_url = URL.create("sqlite", database=str(root / "gateway.db"))
    if volatile:
        database_url = "sqlite://"
        from alembic import command

        from agentforge.db.migrate import _configuration

        memory = create_database_engine(database_url)
        with memory.begin() as connection:
            config = _configuration()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    else:
        upgrade_database(database_url)
    workers = root / "workers.toml"
    workers.write_text(
        """[[workers]]
id = "local-4080"
provider = "ollama"
model = "synthetic"
endpoint = "http://127.0.0.1:11434"
supports_tools = true
""",
        encoding="utf-8",
    )
    inference = Inference()
    provider = OllamaProvider(transport=httpx.MockTransport(inference))
    config = WorkersConfig(
        workers=[
            Worker(
                id=name,
                provider="ollama",
                model="synthetic",
                endpoint="http://127.0.0.1:11434",
                supports_tools=True,
                deployment_label="Synthetic Windows host"
                if name == "local-4080"
                else "Scripted LAN",
            )
            for name in ("local-4080", "home-i5", "denied-cloud")
        ]
    )
    applications = []
    starts, closes = [], []
    artifact_directory = root / "artifacts"
    artifact_directory.mkdir(mode=0o700)
    specialists = SpecialistConfig(
        artifact_directory=artifact_directory,
        tools=(
            SpecialistTool(
                tool_id="image",
                kind="comfyui_image",
                adapter="fake_comfyui",
                connection_id="fake",
                output_formats=("png",),
                enabled=True,
            ),
        ),
    )

    def factory():
        app = Application(
            memory if volatile else create_database_engine(database_url),
            config,
            agents=shipped_agents(coding_enabled=True),
            providers={"ollama": provider},
            concurrency=2,
            specialist_config=specialists,
        )
        original_start, original_close = app.tasks.start, app.tasks.close

        async def start():
            starts.append(id(app.tasks))
            await original_start()

        async def close():
            closes.append(id(app.tasks))
            await original_close()

        app.tasks.start, app.tasks.close = start, close
        applications.append(app)
        return app

    # Registry setup is an explicit host-side operator operation.
    setup_database = memory if volatile else create_database_engine(database_url)
    registry = ProjectRegistry(
        ProjectRepository(create_session_factory(setup_database))
    )
    project_root = root / "windows-checkout"
    project_root.mkdir()
    (project_root / "README.md").write_text(
        "Windows checkout evidence\n", encoding="utf-8"
    )
    project = registry.register_project("Synthetic host checkout", project_root)
    private_root = root / "private-project"
    private_root.mkdir()
    private_project = registry.register_project("Private Project", private_root)
    if not volatile:
        setup_database.dispose()
    binding = dict(
        project_id=str(project.id),
        agent_id="repo_explorer",
        worker_id="local-4080",
        task="Analyze README.md",
    )
    auth_path = root / "clients.json"
    token_a, token_b, token_c = ("test-" + name * 48 for name in ("a", "b", "c"))
    tools = tuple(t.name for t in TOOL_CONTRACTS if "coding" not in t.name)
    grant = ProjectGrant(
        project_id=project.id, workers=("local-4080", "home-i5"), specialists=("image",)
    )
    a = ClientPolicy(
        client_id="windows-codex",
        token_sha256=fingerprint(token_a),
        tools=tools,
        projects=(grant,),
        agents=("repo_explorer", "general_agent", "coder"),
    )
    b = a.model_copy(
        update={"client_id": "macbook-codex", "token_sha256": fingerprint(token_b)}
    )
    c = ClientPolicy(client_id="unprivileged", token_sha256=fingerprint(token_c))

    def policy(*clients):
        snapshot = CredentialStore(auth_path).load() if auth_path.exists() else None
        save_policies(auth_path, Policies(clients=clients), expected=snapshot)

    policy(a, b, c)
    port, companion_port = free_port(), free_port()
    settings = GatewayConfig(
        port=port,
        max_sessions=4,
        session_idle_seconds=2.0,
        max_pending_tasks=8,
        watch_seconds=2,
    )
    web = create_gateway(
        factory,
        CredentialStore(auth_path),
        settings,
        companion=CompanionConfig(port=companion_port),
    )
    async with listener(web, port=port) as base:
        url = base + "/mcp"
        async with httpx.AsyncClient(trust_env=False, timeout=5) as raw:
            for token in (None, "invalid-credentials"):
                response = await raw.post(
                    url, headers={"Authorization": "Bearer " + token} if token else {}
                )
                assert (
                    response.status_code == 401
                    and "Bearer" in response.headers["www-authenticate"]
                )
            for headers, status in (
                ({"Host": "evil.invalid"}, 421),
                ({"Origin": "https://evil.invalid"}, 403),
                (
                    {
                        "Host": "evil.invalid",
                        "X-Forwarded-Host": f"127.0.0.1:{port}",
                        "X-Forwarded-Proto": "https",
                    },
                    421,
                ),
            ):
                assert (await raw.post(url, headers=headers)).status_code == status
            assert (await raw.post(url + "?token=never-accepted")).status_code == 400
            assert (
                await raw.post(url, content=b"x" * (settings.max_request_bytes + 1))
            ).status_code == 413
            assert (
                await raw.get(
                    base + "/companion", headers={"Authorization": "Bearer " + token_a}
                )
            ).status_code == 404
        async with (
            client(url, token_a) as (ca, session_a),
            client(url, token_b) as (cb, session_b),
        ):
            app = applications[0]
            assert len(applications) == len(starts) == 1 and len(app.tasks._loops) == 2
            engine_id = id(app.tasks)
            tool_fields = dict(
                project_id=str(project.id),
                tool_id="image",
                request_id=str(uuid4()),
                idempotency_key=uuid4().hex,
            )
            tool_job = await call(ca, "submit_tool_job", tool_fields)
            job_query = {"tool_job_id": tool_job["tool_job_id"]}
            async with asyncio.timeout(5):
                while True:
                    tool_job = await call(ca, "get_tool_job", job_query)
                    if tool_job["state"] == "completed":
                        break
                    assert tool_job["state"] in {"queued", "running"}, tool_job
                    await asyncio.sleep(0.01)
            duplicate = await call(ca, "submit_tool_job", tool_fields)
            assert duplicate["tool_job_id"] == tool_job["tool_job_id"]
            artifact_query = {
                "project_id": str(project.id),
                "artifact_id": tool_job["output_artifact_ids"][0],
            }
            await call(cb, "get_tool_job", job_query, error="access_denied")
            await call(cb, "cancel_tool_job", job_query, error="access_denied")
            await call(cb, "retrieve_artifact", artifact_query, error="access_denied")
            assert not (
                await call(cb, "list_artifacts", {"project_id": str(project.id)})
            )["artifacts"]
            descriptor = await call(ca, "inspect_artifact", artifact_query)
            assert descriptor["simulated"] and descriptor["status"] == "ready"
            chunk = await call(
                ca, "retrieve_artifact", {**artifact_query, "max_bytes": 8}
            )
            assert chunk["next_offset"] == 8
            print(
                "S: authenticated specialist idempotency, ownership "
                "and bounded retrieval",
                flush=True,
            )
            assert await call(ca, "list_workers") == await call(cb, "list_workers")
            assert await call(ca, "list_agents") == await call(cb, "list_agents")
            projects = await call(cb, "list_projects")
            assert [p["project_id"] for p in projects["projects"]] == [str(project.id)]
            assert "private-project" not in json.dumps(projects)
            assert (await call(cb, "agentforge_status"))["client_id"] == "macbook-codex"
            assert (await call(cb, "list_workers", {"limit": 1}))["next_offset"] == 1
            assert (await call(cb, "list_workers", {"offset": 1}))[
                "next_offset"
            ] is None
            print(
                "A: two SDK clients share one Application/TaskEngine",
                flush=True,
            )

            submitted = await call(cb, "delegate_task", binding)
            identity = {"task_id": submitted["task_id"]}
            await app.tasks.wait_task(submitted["task_id"])
            result = await call(cb, "get_task", identity)
            assert (
                result["state"] == "completed"
                and "README.md:1" in result["final_answer"]
            )
            task = app.tasks.get_task(submitted["task_id"])
            assert task.client_id == "macbook-codex" and task.worker_id == "local-4080"
            telemetry = app.telemetry.get_for_task(task.task_id)
            assert (
                telemetry.worker_id == "local-4080" and telemetry.model == "synthetic"
            )
            await call(ca, "get_task", identity, error="access_denied")
            policy(a.model_copy(update={"read_shared": True}), b, c)
            assert await call(ca, "get_task", identity) == result
            await call(ca, "cancel_task", identity, error="access_denied")
            policy(a, b, c)
            print(
                "B: Mac-side repository analysis via scripted host Ollama",
                flush=True,
            )

            for override in (
                {"project_id": str(private_project.id)},
                {"worker_id": "denied-cloud"},
                {"agent_id": "coder"},
            ):
                await call(
                    cb, "delegate_task", {**binding, **override}, error="access_denied"
                )
            await call(
                cb,
                "delegate_task",
                {**binding, "client_id": "windows-codex"},
                error="invalid_arguments",
            )
            await call(cb, "get_task", {"task_id": str(uuid4())}, error="access_denied")
            for name in (
                "get_coding_workspace",
                "get_coding_diff",
                "cleanup_coding_workspace",
            ):
                args = {
                    **identity,
                    **(
                        {"workspace_id": str(uuid4())}
                        if name.startswith("cleanup")
                        else {}
                    ),
                }
                await call(cb, name, args, error="access_denied")
            async with client(url, token_c) as (cc, _):
                assert (await cc.list_tools()).tools == []
                await call(cc, "list_projects", error="access_denied")
            # Presenting A's session with B's credential cannot impersonate A.
            async with httpx.AsyncClient(trust_env=False) as raw:
                response = await raw.post(
                    url,
                    headers={
                        "Authorization": "Bearer " + token_b,
                        "Mcp-Session-Id": session_a(),
                        "Accept": "application/json, text/event-stream",
                    },
                    json={"jsonrpc": "2.0", "id": 90, "method": "tools/list"},
                )
                assert response.status_code == 404

            inference.release.clear()
            long = await call(cb, "delegate_task", binding)
            await wait_running(app, 1)
            progress = []

            async def on_progress(value, total, message):
                progress.append(json.loads(message))

            watch = asyncio.create_task(
                call(
                    cb,
                    "watch_task",
                    {"task_id": long["task_id"]},
                    progress_callback=on_progress,
                )
            )
            await asyncio.sleep(0.1)
            # A capacity refusal is explicit and never commits partial Council work.
            settings.max_pending_tasks = 1
            before = len(app.tasks.list_tasks())
            await call(cb, "delegate_task", binding, error="resource_limit")
            await call(
                cb,
                "delegate_council",
                {
                    k: v
                    for k, v in {
                        **binding,
                        "worker_ids": ["local-4080", "home-i5"],
                    }.items()
                    if k != "worker_id"
                },
                error="resource_limit",
            )
            assert len(app.tasks.list_tasks()) == before
            settings.max_pending_tasks = 8
            if not volatile:
                await conflicting_executors(database_url, workers)
                assert (await call(cb, "get_task", {"task_id": long["task_id"]}))[
                    "state"
                ] == "running"
                print(
                    "F: stdio/web contention refused before recovery; owner unaffected",
                    flush=True,
                )
            watched = await watch  # Bounded watch returns; underlying Task still runs.
            assert not watched["terminal"] and progress
            assert not settings.watchers and app.tasks.observer.subscriber_count == 0
        # Both SDK sessions have sent DELETE; neither closes the persistent owner.
        assert (
            app.tasks.available
            and id(app.tasks) == engine_id
            and len(applications) == 1
        )
        inference.release.set()
        await app.tasks.wait_task(long["task_id"])
        async with client(url, token_b) as (cb, session_b):
            recovered = await call(cb, "get_task", {"task_id": long["task_id"]})
            assert (
                recovered["state"] == "completed"
                and recovered["task_id"] == long["task_id"]
            )
            assert len(app.tasks.list_tasks()) == 2
            print(
                "C: Tasks survive client DELETE; reconnect recovers the same ID",
                flush=True,
            )
            inference.release.clear()
            async with client(url, token_a) as (ca, _):
                first, second = await asyncio.gather(
                    call(ca, "delegate_task", binding),
                    call(cb, "delegate_task", binding),
                )
                await wait_running(app, 2)
                council_binding = {
                    k: v
                    for k, v in {
                        **binding,
                        "worker_ids": ["local-4080", "home-i5"],
                    }.items()
                    if k != "worker_id"
                }
                council = await call(cb, "delegate_council", council_binding)
                council_id = {"council_id": council["council_id"]}
                await call(ca, "get_council", council_id, error="access_denied")
                await call(ca, "cancel_council", council_id, error="access_denied")
                assert (await call(cb, "agentforge_status"))["running_tasks"] == 1
                assert (await call(ca, "agentforge_status"))["running_tasks"] == 1
                inference.release.set()
                for item in (first, second, *council["participants"]):
                    await app.tasks.wait_task(item["task_id"])
                complete = await call(cb, "get_council", council_id)
                assert (
                    complete["terminal"]
                    and complete["participant_counts"]["completed"] == 2
                ), complete
                assert {p["worker_id"] for p in complete["participants"]} == {
                    "local-4080",
                    "home-i5",
                }
                assert inference.maximum == 2
                print(
                    "D: concurrent Tasks/Council respect executor slots and telemetry",
                    flush=True,
                )

                # Exercise successful HTTP cancellation, separately from denial.
                inference.release.clear()
                running_a = await call(ca, "delegate_task", binding)
                running_b = await call(cb, "delegate_task", binding)
                await wait_running(app, 2)
                queued_council = await call(cb, "delegate_council", council_binding)
                cancelled_council = await call(
                    cb, "cancel_council", {"council_id": queued_council["council_id"]}
                )
                assert cancelled_council["terminal"]
                assert cancelled_council["participant_counts"]["cancelled"] == 2
                policy(
                    a.model_copy(update={"read_shared": True, "cancel_shared": True}),
                    b,
                    c,
                )
                cancelled = await call(
                    ca, "cancel_task", {"task_id": running_b["task_id"]}
                )
                assert cancelled["cancellation_requested_at"] is not None
                await call(ca, "cancel_task", {"task_id": running_a["task_id"]})
                policy(a, b, c)
                inference.release.set()
                for item in (running_a, running_b):
                    terminal = await app.tasks.wait_task(item["task_id"])
                    assert (
                        terminal.state == "cancelled" and terminal.final_answer is None
                    )

                # Standard MCP request cancellation releases bounded watches,
                # without cancelling the durable Task they observe.
                inference.release.clear()
                observed = await call(cb, "delegate_task", binding)
                watches = [
                    asyncio.create_task(
                        call(
                            cb,
                            "watch_task",
                            {"task_id": observed["task_id"]},
                            progress_callback=on_progress,
                        )
                    )
                    for _ in range(4)
                ]
                async with asyncio.timeout(5):
                    while settings.watchers.get("macbook-codex") != 4:
                        await asyncio.sleep(0.01)
                await call(
                    cb,
                    "watch_task",
                    {"task_id": observed["task_id"]},
                    error="resource_limit",
                    progress_callback=on_progress,
                )
                for watching in watches:
                    watching.cancel()
                await asyncio.gather(*watches, return_exceptions=True)
                async with asyncio.timeout(5):
                    while settings.watchers or app.tasks.observer.subscriber_count:
                        await asyncio.sleep(0.01)
                assert (await call(cb, "get_task", {"task_id": observed["task_id"]}))[
                    "state"
                ] == "running"
                inference.release.set()
                await app.tasks.wait_task(observed["task_id"])

                # Revoke while a request is already streaming: revalidation stops
                # its output, even though the POST authenticated before revocation.
                inference.release.clear()
                retained = await call(cb, "delegate_task", binding)
                watch = asyncio.create_task(
                    call(
                        cb,
                        "watch_task",
                        {"task_id": retained["task_id"]},
                        error="access_denied",
                        progress_callback=on_progress,
                    )
                )
                async with asyncio.timeout(5):
                    while (
                        settings.watchers.get("macbook-codex") != 1
                        or app.tasks.observer.subscriber_count != 1
                    ):
                        await asyncio.sleep(0.01)
                policy(a, b.model_copy(update={"enabled": False}), c)
                await asyncio.wait_for(watch, 3)
                async with httpx.AsyncClient(trust_env=False) as raw:
                    assert (
                        await raw.post(
                            url, headers={"Authorization": "Bearer " + token_b}
                        )
                    ).status_code == 401
                    assert (
                        await raw.post(
                            url,
                            headers={
                                "Authorization": "Bearer " + token_b,
                                "Mcp-Session-Id": session_b(),
                            },
                        )
                    ).status_code == 401
                inference.release.set()
                await app.tasks.wait_task(retained["task_id"])
                # New credential, same durable identity; old token/session revoked.
                token_b_new = "rotated-" + "b" * 48
                b = b.model_copy(update={"token_sha256": fingerprint(token_b_new)})
                policy(a, b, c)
                async with httpx.AsyncClient(trust_env=False) as raw:
                    headers = {
                        "Authorization": "Bearer " + token_b_new,
                        "Mcp-Session-Id": session_b(),
                        "Accept": "application/json, text/event-stream",
                    }
                    assert (
                        await raw.post(
                            url,
                            headers=headers,
                            json={"jsonrpc": "2.0", "id": 91, "method": "tools/list"},
                        )
                    ).status_code == 404
                async with client(url, token_b_new) as (rotated, _):
                    assert (
                        await call(
                            rotated, "get_task", {"task_id": retained["task_id"]}
                        )
                    )["state"] == "completed"
                token_b = token_b_new
                print(
                    "E: access denial, isolation, live revocation and rotation",
                    flush=True,
                )
        await asyncio.sleep(
            2.2
        )  # Reclaim the session whose revoked DELETE was refused.
        # SDK idle expiry and admission use raw independent initialized sessions
        # without holding a GET stream open, which intentionally prevents idling.
        async with httpx.AsyncClient(trust_env=False) as raw:
            headers = {
                "Authorization": "Bearer " + token_a,
                "Accept": "application/json, text/event-stream",
            }
            initialization = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "expiry", "version": "1"},
                },
            }
            opened = [
                await raw.post(url, headers=headers, json=initialization)
                for _ in range(4)
            ]
            assert all(r.status_code == 200 for r in opened)
            assert (
                await raw.post(url, headers=headers, json=initialization)
            ).status_code == 503
            session = opened[0].headers["mcp-session-id"]
            assert (
                await raw.delete(url, headers={**headers, "Mcp-Session-Id": session})
            ).status_code == 200
            assert (
                await raw.post(url, headers=headers, json=initialization)
            ).status_code == 200
            await asyncio.sleep(2.2)
            response = await raw.post(
                url,
                headers={
                    **headers,
                    "Mcp-Session-Id": opened[1].headers["mcp-session-id"],
                },
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            assert response.status_code == 404
        async with httpx.AsyncClient(trust_env=False) as raw:
            for path in ("/", "/companion", "/tasks", "/councils"):
                response = await raw.get(f"http://127.0.0.1:{companion_port}" + path)
                assert response.status_code == 200
        if not volatile:
            async with client(url, token_b) as (cb, _):
                inference.release.clear()
                shutdown_tasks = [
                    await call(cb, "delegate_task", binding) for _ in range(4)
                ]
                await wait_running(app, 2)
    assert closes == starts and len(starts) == 1
    if volatile:
        return
    # Graceful shutdown releases ownership; a queued row claimed by a lost owner
    # represents the existing interrupted-work recovery path (never replay).
    engine = create_database_engine(database_url)
    repository = TaskRepository(create_session_factory(engine))
    queued = [
        t
        for t in shutdown_tasks
        if repository.get(UUID(t["task_id"])).state == "queued"
    ]
    assert len(queued) == 2
    orphan = queued[0]["task_id"]
    repository.claim(UUID(orphan))
    engine.dispose()
    before = len(inference.requests)
    inference.release.set()
    web = create_gateway(factory, CredentialStore(auth_path), settings)
    async with (
        listener(web, port=port) as base,
        client(base + "/mcp", token_b) as (cb, _),
    ):
        assert len(applications) == len(starts) == 2
        await applications[-1].tasks.wait_task(queued[1]["task_id"])
        assert (await call(cb, "get_task", {"task_id": orphan}))[
            "reason"
        ] == "execution_interrupted"
        assert (await call(cb, "get_task", identity))["state"] == "completed"
        assert (
            len(inference.requests) == before + 2
        )  # One queued analysis, two model turns.
        reasons = [
            applications[-1].tasks.get_task(t["task_id"]).reason for t in shutdown_tasks
        ]
        assert (
            reasons.count("executor_cancelled") == 2
            and reasons.count("execution_interrupted") == 1
            and reasons.count("completed") == 1
        )
    assert len(closes) == 2
    print(
        "G: release, persistence, queued continuation and recovery; no replay",
        flush=True,
    )


async def main():
    handler = logging.StreamHandler()
    handler.addFilter(_SafeDiagnostics())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    with TemporaryDirectory() as directory:
        async with asyncio.timeout(55):
            await integration(Path(directory).resolve())
        await tls_transport()


async def installed_gateway():
    """Launch the installed CLI with a portable test-only SIGINT control pipe."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        database = "sqlite:///" + (root / "installed.db").as_posix()
        auth, token_file, workers = (
            root / "auth.json",
            root / "client.token",
            root / "workers.toml",
        )

        def operator(*arguments):
            completed = subprocess.run(
                [sys.executable, "-m", "agentforge", *map(str, arguments)],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            return json.loads(completed.stdout)

        operator("db", "upgrade", "--database-url", database)
        checkout = root / "checkout"
        checkout.mkdir()
        (checkout / "README.md").write_text(
            "Windows checkout evidence\n", encoding="utf-8"
        )
        project = operator(
            "project",
            "add",
            checkout,
            "--name",
            "Installed synthetic",
            "--database-url",
            database,
        )
        # The operator Project response is deliberately compact; fetch its ID.
        project_id = project["registration"]["project_id"]
        assert project_id, project
        operator(
            "gateway",
            "client-add",
            "--client-id",
            "installed-codex",
            "--auth",
            auth,
            "--token-file",
            token_file,
        )
        store = CredentialStore(auth)
        snapshot = store.load()
        entry = snapshot.clients[0].model_copy(
            update={
                "tools": (
                    "agentforge_status",
                    "list_workers",
                    "list_agents",
                    "list_projects",
                    "delegate_task",
                    "get_task",
                    "watch_task",
                ),
                "projects": (
                    ProjectGrant(project_id=project_id, workers=("local-4080",)),
                ),
                "agents": ("general_agent",),
            }
        )
        save_policies(auth, Policies(clients=(entry,)), expected=snapshot)
        token = token_file.read_text().strip()
        inference = Inference()

        async def infer(request):
            response = await inference(
                httpx.Request(
                    "POST", "http://scripted/api/chat", content=await request.body()
                )
            )
            return JSONResponse(response.json())

        fake = Starlette(routes=[Route("/api/chat", infer, methods=["POST"])])
        async with listener(fake) as provider_url:
            workers.write_text(
                f'''[[workers]]
id = "local-4080"
provider = "ollama"
model = "synthetic"
endpoint = "{provider_url}"
supports_tools = true
''',
                encoding="utf-8",
            )
            task_id = None
            for _ in range(2):
                port = free_port()
                # No service behavior is replaced. The helper thread only raises
                # the same SIGINT as Ctrl+C so hosted Windows needs no console UI.
                bootstrap = """import signal, sys, threading
from agentforge.cli import main
def stop():
    sys.stdin.readline()
    signal.raise_signal(signal.SIGINT)
threading.Thread(target=stop, daemon=True).start()
raise SystemExit(main())
"""
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    bootstrap,
                    "gateway",
                    "serve",
                    "--database-url",
                    database,
                    "--workers",
                    str(workers),
                    "--auth",
                    str(auth),
                    "--port",
                    str(port),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                url = f"http://127.0.0.1:{port}/mcp"
                try:
                    async with httpx.AsyncClient(trust_env=False, timeout=1) as raw:
                        async with asyncio.timeout(15):
                            while True:
                                if process.returncode is not None:
                                    out, err = await process.communicate()
                                    raise AssertionError((process.returncode, out, err))
                                try:
                                    if (await raw.post(url)).status_code == 401:
                                        break
                                except httpx.TransportError:
                                    pass
                                await asyncio.sleep(0.05)
                    async with client(url, token) as (session, _):
                        assert (await call(session, "agentforge_status"))[
                            "client_id"
                        ] == "installed-codex"
                        assert (await call(session, "list_workers"))["workers"][0][
                            "worker_id"
                        ] == "local-4080"
                        assert "delegate_task" in {
                            t.name for t in (await session.list_tools()).tools
                        }
                        if task_id is None:
                            task_id = (
                                await call(
                                    session,
                                    "delegate_task",
                                    {
                                        "project_id": project_id,
                                        "agent_id": "general_agent",
                                        "worker_id": "local-4080",
                                        "task": "Read README.md",
                                    },
                                )
                            )["task_id"]
                        async with asyncio.timeout(5):
                            while True:
                                result = await call(
                                    session, "get_task", {"task_id": task_id}
                                )
                                if result["state"] == "completed":
                                    assert "README.md:1" in result["final_answer"]
                                    break
                                assert result["state"] in {"queued", "running"}, result
                                await asyncio.sleep(0.05)
                finally:
                    if process.returncode is None:
                        process.stdin.write(b"stop\n")
                        await process.stdin.drain()
                    try:
                        out, err = await asyncio.wait_for(process.communicate(), 10)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                        raise
                    assert process.returncode == 0, (process.returncode, out, err)
                    assert token.encode() not in out + err and b"Traceback" not in err
            assert (
                len(inference.requests) == 2
            )  # Durable read after restart; no re-execution.
    print(
        "H: installed CLI discovery, authentication, inference and restart",
        flush=True,
    )


async def tls_transport():
    """Ephemeral TLS and certificate verification; no physical/private network."""
    import ipaddress
    import ssl
    from datetime import UTC, datetime, timedelta

    from alembic import command
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from agentforge.db.migrate import _configuration

    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
                ),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .sign(key, hashes.SHA256())
        )
        certificate, private_key = root / "cert.pem", root / "key.pem"
        certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private_key.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        database = create_database_engine("sqlite://")
        with database.begin() as connection:
            migration = _configuration()
            migration.attributes["connection"] = connection
            command.upgrade(migration, "head")
        token = "tls-fixture-" + "a" * 48
        auth = root / "auth.json"
        save_policies(
            auth,
            Policies(
                clients=(
                    ClientPolicy(
                        client_id="tls-client",
                        token_sha256=fingerprint(token),
                        tools=("agentforge_status",),
                    ),
                )
            ),
            expected=None,
        )
        port = free_port()
        settings = GatewayConfig(
            port=port, tls_cert=str(certificate), tls_key=str(private_key)
        )
        web = create_gateway(
            lambda: Application(database, WorkersConfig(workers=[]), providers={}),
            CredentialStore(auth),
            settings,
        )
        async with listener(web, port=port, tls=(certificate, private_key)) as base:
            async with httpx.AsyncClient(trust_env=False) as raw:
                try:
                    await raw.post(base + "/mcp")
                except httpx.ConnectError:
                    pass  # Default trust rejects the ephemeral self-signed certificate.
                else:
                    raise AssertionError("Untrusted TLS certificate was accepted")
            context = ssl.create_default_context(cafile=str(certificate))
            async with client(base + "/mcp", token, verify=context) as (session, _):
                assert (await call(session, "agentforge_status"))[
                    "client_id"
                ] == "tls-client"
    print(
        "TLS: real HTTPS MCP with explicit certificate trust; no verification bypass",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
