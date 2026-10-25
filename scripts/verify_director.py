"""Offline real TCP acceptance for Local Director; also run from installed wheel."""

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from verify_gateway import Inference, free_port, listener

from agentforge.application.access import ClientPolicy, Policies, ProjectGrant
from agentforge.application.service import Application
from agentforge.core.worker import Worker
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.migrate import upgrade_database
from agentforge.db.projects import ProjectRepository
from agentforge.director.config import DirectorConfig
from agentforge.director.core import Director
from agentforge.gateway.auth import CredentialStore, fingerprint, save_policies
from agentforge.gateway.server import GatewayConfig, create_gateway
from agentforge.mcp.server import TOOL_CONTRACTS, _SafeDiagnostics
from agentforge.projects.service import ProjectRegistry
from agentforge.providers.ollama import OllamaProvider
from agentforge.web.combined import CompanionConfig
from agentforge.workers.config import WorkersConfig


class DirectorInference:
    """Native Ollama tool protocol, not an alternate orchestration implementation."""

    def __init__(self, binding):
        self.binding = binding
        self.requests = []

    async def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        final = payload["messages"][-1]["role"] == "tool" or any(
            "Recovered known Task" in m.get("content", "")
            or "Approved operation result" in m.get("content", "")
            for m in payload["messages"]
        )
        message = (
            {"content": "README.md:1 — evidence reported by the observed Worker Task."}
            if final
            else {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "delegate_task", "arguments": self.binding}}
                ],
            }
        )
        return httpx.Response(
            200, json={"model": "director", "done": True, "message": message}
        )


async def integration(root, *, web_chat=False):
    from sqlalchemy.engine import URL

    url = URL.create("sqlite", database=str(root / "gateway.db"))
    upgrade_database(url)
    engine = create_database_engine(url)
    registry = ProjectRegistry(ProjectRepository(create_session_factory(engine)))
    project_root = root / "project"
    project_root.mkdir()
    (project_root / "README.md").write_text(
        "Windows checkout evidence\n", encoding="utf-8"
    )
    project = registry.register_project("Scripted project", project_root)
    engine.dispose()
    binding = {
        "project_id": str(project.id),
        "agent_id": "repo_explorer",
        "worker_id": "local",
        "task": "Read README.md and cite evidence.",
    }
    inference = Inference()
    worker_provider = OllamaProvider(transport=httpx.MockTransport(inference))
    config = WorkersConfig(
        workers=[
            Worker(
                id="local",
                provider="ollama",
                model="synthetic",
                endpoint="http://127.0.0.1:11434",
                trust="local",
                supports_tools=True,
            )
        ]
    )
    applications, starts = [], []

    def factory():
        app = Application(
            create_database_engine(url),
            config,
            providers={"ollama": worker_provider},
            concurrency=2,
        )
        applications.append(app)
        start = app.tasks.start

        async def counted():
            starts.append(id(app.tasks))
            await start()

        app.tasks.start = counted
        return app

    auth = root / "clients.json"
    tokens = ["test-" + letter * 48 for letter in ("d", "e", "w")]
    clients = tuple(
        ClientPolicy(
            client_id=f"director-{i}",
            token_sha256=fingerprint(token),
            tools=tuple(t.name for t in TOOL_CONTRACTS),
            projects=(ProjectGrant(project_id=project.id, workers=("local",)),),
            agents=("repo_explorer",),
        )
        for i, token in enumerate(tokens)
    )
    save_policies(auth, Policies(clients=clients), expected=None)
    port = free_port()
    async with AsyncExitStack() as stack:
        companion = None
        if web_chat:
            scripted_web = DirectorInference(binding)

            async def inference_endpoint(request):
                response = await scripted_web(
                    httpx.Request(
                        "POST",
                        "http://127.0.0.1/api/chat",
                        content=await request.body(),
                    )
                )
                return JSONResponse(response.json())

            inference_app = Starlette(
                routes=[Route("/api/chat", inference_endpoint, methods=["POST"])]
            )
            inference_url = await stack.enter_async_context(listener(inference_app))
            os.environ["AGENTFORGE_WEB_SMOKE_TOKEN"] = tokens[2]
            settings = root / "director.toml"
            settings.write_text(
                f'gateway_url = "http://127.0.0.1:{port}/mcp"\n'
                'token_env = "AGENTFORGE_WEB_SMOKE_TOKEN"\n'
                f"journal = {json.dumps(str(root / 'web-history'))}\n"
                'model = "director"\n[inference]\nid = "director"\n'
                'type = "ollama"\ntrust = "local"\n'
                f'base_url = "{inference_url}"\n',
                encoding="utf-8",
            )
            companion = CompanionConfig(port=free_port(), director_config=str(settings))
        gateway = create_gateway(
            factory,
            CredentialStore(auth),
            GatewayConfig(port=port),
            companion=companion,
        )
        base = await stack.enter_async_context(listener(gateway, port=port))

        async def run_one(i):
            env = f"AGENTFORGE_SMOKE_DIRECTOR_{i}"
            os.environ[env] = tokens[i]
            director_config = DirectorConfig(
                gateway_url=base + "/mcp",
                token_env=env,
                journal=root / f"journal-{i}",
                model="director",
                inference={
                    "id": "director",
                    "type": "ollama",
                    "base_url": "http://127.0.0.1:11434",
                    "trust": "local",
                },
            )
            scripted = DirectorInference(binding)
            provider = OllamaProvider(
                director_config.inference, transport=httpx.MockTransport(scripted)
            )
            try:
                async with Director(director_config, provider=provider) as director:
                    item = director.create("cli")
                    item = await director.run(
                        item.id, "cli", "Inspect the project using local models."
                    )
                    assert item.state == "completed", item.error
                    assert (
                        len(item.tasks) == 1
                        and item.tasks[0].evidence == "source_tools_observed"
                    )
                    assert item.client_id == f"director-{i}"
                    known = item.tasks[0].task_id
                    item = await director.run(item.id, "cli", resume=True)
                    assert (
                        item.state == "completed"
                        and len(item.tasks) == 1
                        and item.tasks[0].task_id == known
                    )
                    assert tokens[i] not in director.journal.path(item.id).read_text()
                    return item
            finally:
                os.environ.pop(env)

        if web_chat:

            async def chat():
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{companion.port}",
                    trust_env=False,
                    timeout=20,
                ) as browser:
                    home = await browser.get("/director")
                    assert home.status_code == 200, home.text
                    csrf = browser.cookies.get("agentforge_csrf")
                    response = await browser.post("/director/new", data={"csrf": csrf})
                    path = response.headers["location"]
                    page = await browser.get(path)
                    revision = re.search(
                        r'name="revision" value="(\d+)"', page.text
                    ).group(1)
                    response = await browser.post(
                        path + "/send",
                        data={
                            "csrf": csrf,
                            "revision": revision,
                            "message": "Check the project using local models.",
                        },
                    )
                    assert response.status_code == 303
                    async with asyncio.timeout(20):
                        while True:
                            page = await browser.get(path + "/fragment")
                            if 'class="badge">completed' in page.text:
                                break
                            assert 'class="badge">failed' not in page.text, page.text
                            await asyncio.sleep(0.05)
                    assert "source tools observed" in page.text
                    task_id = re.search(r'href="/tasks/([a-f0-9-]+)"', page.text).group(
                        1
                    )
                    assert (await browser.get(f"/tasks/{task_id}")).status_code == 200
                    for _ in range(2):
                        assert (await browser.get(path)).status_code == 200
                    count = len(applications[0].tasks.history())
                    assert len(applications) == len(starts) == 1
                    assert applications[0].status().available
                    print(
                        "Browser HTTP chat -> native Ollama HTTP -> authenticated MCP "
                        "-> existing TaskEngine; reload/no replay: OK"
                    )
                    return count

            results = await asyncio.gather(run_one(0), run_one(1), chat())
            items = results[:2]
        else:
            items = await asyncio.gather(run_one(0), run_one(1))
        assert items[0].tasks[0].task_id != items[1].tasks[0].task_id
        assert len(starts) == len(applications) == 1
        assert applications[0].status().available
        assert applications[0].tasks.observer.subscriber_count == 0
        assert len(applications[0].tasks.history()) == (3 if web_chat else 2)
        print(
            "Two authenticated Directors; one executor; native Ollama tools; "
            "evidence; recovery: OK"
        )


if __name__ == "__main__":
    handler = logging.StreamHandler()
    handler.addFilter(_SafeDiagnostics())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    with TemporaryDirectory() as directory:
        asyncio.run(
            integration(Path(directory).resolve(), web_chat="--web" in sys.argv)
        )
