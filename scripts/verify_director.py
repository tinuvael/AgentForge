"""Offline real TCP acceptance for Local Director; also run from installed wheel."""

import asyncio
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
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


async def integration(root):
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
    tokens = ["test-" + letter * 48 for letter in ("d", "e")]
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
    gateway = create_gateway(factory, CredentialStore(auth), GatewayConfig(port=port))
    async with listener(gateway, port=port) as base:

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
                    count = len(applications[0].tasks.history())
                    item = await director.run(item.id, "cli", resume=True)
                    assert (
                        item.state == "completed"
                        and len(applications[0].tasks.history()) == count
                    )
                    assert tokens[i] not in director.journal.path(item.id).read_text()
                    return item
            finally:
                os.environ.pop(env)

        items = await asyncio.gather(run_one(0), run_one(1))
        assert items[0].tasks[0].task_id != items[1].tasks[0].task_id
        assert len(starts) == len(applications) == 1
        assert applications[0].status().available
        assert applications[0].tasks.observer.subscriber_count == 0
        assert len(applications[0].tasks.history()) == 2
        print(
            "Two authenticated Directors; one executor; native Ollama tools; "
            "evidence; recovery: OK"
        )


if __name__ == "__main__":
    handler = logging.StreamHandler()
    handler.addFilter(_SafeDiagnostics())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    with TemporaryDirectory() as directory:
        asyncio.run(integration(Path(directory).resolve()))
