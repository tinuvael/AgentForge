"""Offline release smoke against the active installed package, from outside source."""

import asyncio
import importlib
import subprocess
import sys
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from agentforge.application.service import Application
from agentforge.core.worker import Worker
from agentforge.db.database import create_database_engine
from agentforge.db.migrate import upgrade_database
from agentforge.mcp.server import TOOL_CONTRACTS, create_server
from agentforge.providers.ollama import OllamaProvider
from agentforge.web.app import create_app
from agentforge.workers.config import WorkersConfig


def package_checks():
    package = importlib.import_module("agentforge")
    assert Path(sys.prefix) in Path(package.__file__).parents, package.__file__
    for name in (
        "application.service",
        "agents.runtime",
        "coding.service",
        "index.service",
        "projects.windows",
        "providers.openai_compatible",
        "tasks.engine",
        "telemetry.service",
        "mcp.server",
        "web.app",
    ):
        importlib.import_module("agentforge." + name)
    root = resources.files("agentforge")
    for name in (
        "db/migrations/env.py",
        "db/migrations/script.py.mako",
        "db/migrations/versions/0001_projects.py",
        "db/migrations/versions/0008_coding_workspaces.py",
        "web/templates/base.html",
        "web/static/dashboard.css",
        "web/static/dashboard.js",
        "web/static/htmx.min.js",
        "web/static/htmx.LICENSE",
        "web/static/THIRD_PARTY.md",
    ):
        assert root.joinpath(name).is_file(), name
    for module in ("db.migrate", "mcp.server", "web.server"):
        result = subprocess.run(
            [sys.executable, "-m", "agentforge." + module, "--help"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "--database-url" in result.stdout
    print("Installed package, migrations, web assets and CLI help: OK")


async def smoke():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        url = "sqlite:///" + (root / "smoke.db").as_posix()
        upgrade_database(url)
        upgrade_database(url)
        workers = WorkersConfig(
            workers=[
                Worker(
                    id="offline",
                    provider="ollama",
                    endpoint="http://worker.invalid",
                    model="offline",
                    supports_tools=True,
                )
            ]
        )
        provider = OllamaProvider(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "model": "offline",
                        "message": {"content": "Offline smoke completed."},
                        "done": True,
                    },
                )
            )
        )
        apps = []

        def factory():
            app = Application(
                create_database_engine(url), workers, providers={"ollama": provider}
            )
            apps.append(app)
            return app

        async with create_connected_server_and_client_session(
            create_server(factory)
        ) as client:
            discovered = await client.list_tools()
            assert {t.name for t in discovered.tools} == {
                t.name for t in TOOL_CONTRACTS
            }
            app = apps[-1]
            project = app.projects.register_project("Synthetic", root)
            result = await client.call_tool(
                "delegate_task",
                {
                    "project_id": str(project.id),
                    "agent_id": "repo_explorer",
                    "worker_id": "offline",
                    "task": "Return a brief answer.",
                },
            )
            assert not result.isError
            task = await app.tasks.wait_task(result.structuredContent["task_id"])
            assert task.state == "completed"
            assert app.telemetry.get_for_task(task.task_id).model_call_count == 1
        print("MCP SDK discovery, scripted Task, telemetry and shutdown: OK")
        web = create_app(factory)
        async with web.router.lifespan_context(web):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=web), base_url="http://localhost"
            ) as client:
                for path in ("/", "/workers", "/projects", "/tasks", "/councils"):
                    response = await client.get(path)
                    assert response.status_code == 200, path
                assert (await client.get("/static/dashboard.css")).status_code == 200
        print("Dashboard migrated startup, pages, static assets and shutdown: OK")


if __name__ == "__main__":
    package_checks()
    asyncio.run(smoke())
