"""Offline release smoke against the active installed package, from outside source."""

import asyncio
import importlib
import pkgutil
import subprocess
import sys
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from alembic.runtime.migration import MigrationContext
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import inspect

from agentforge.application.service import Application
from agentforge.core.worker import Worker
from agentforge.db.database import Base, create_database_engine
from agentforge.mcp.server import TOOL_CONTRACTS, create_server
from agentforge.providers.ollama import OllamaProvider
from agentforge.web.app import create_app
from agentforge.workers.config import WorkersConfig


def package_checks():
    package = importlib.import_module("agentforge")
    assert Path(sys.prefix) in Path(package.__file__).parents, package.__file__
    for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        # Alembic scripts execute only inside its configured migration context.
        if not module.name.startswith("agentforge.db.migrations"):
            imported = importlib.import_module(module.name)
            assert Path(sys.prefix) in Path(imported.__file__).parents, module.name
    root = resources.files("agentforge")
    for name in (
        "db/migrations/env.py",
        "db/migrations/script.py.mako",
        "db/migrations/versions/0001_initial.py",
        "web/templates/base.html",
        "web/static/dashboard.css",
        "web/static/dashboard.js",
        "web/static/htmx.min.js",
        "web/static/htmx.LICENSE",
        "web/static/THIRD_PARTY.md",
    ):
        assert root.joinpath(name).is_file(), name
    assert {
        file.name
        for file in root.joinpath("db/migrations/versions").iterdir()
        if file.name.endswith(".py")
    } == {"0001_initial.py"}
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
        for _ in range(2):
            subprocess.run(
                [sys.executable, "-m", "agentforge.db.migrate", "--database-url", url],
                check=True,
                capture_output=True,
                timeout=10,
            )
        engine = create_database_engine(url)
        try:
            with engine.connect() as connection:
                assert (
                    MigrationContext.configure(connection).get_current_revision()
                    == "0001_initial"
                )
                assert set(inspect(connection).get_table_names()) == {
                    *Base.metadata.tables,
                    "alembic_version",
                }
                columns = inspect(connection).get_columns("projects")
                assert {column["name"] for column in columns} == {
                    "id",
                    "name",
                    "root_path",
                    "created_at",
                    "root_identity",
                }
                assert any(
                    column["name"] == "root_identity" and not column["nullable"]
                    for column in columns
                )
        finally:
            engine.dispose()
        print("Installed migration CLI, fresh initial schema and idempotency: OK")
        config = root / "workers.toml"
        config.write_text(
            '[[workers]]\nid = "offline"\nprovider = "ollama"\n'
            'endpoint = "http://worker.invalid"\nmodel = "offline"\n'
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agentforge.mcp.server",
                "--database-url",
                url,
                "--workers",
                str(config),
            ],
        )
        async with asyncio.timeout(15), stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                assert {tool.name for tool in (await client.list_tools()).tools} == {
                    contract.name for contract in TOOL_CONTRACTS
                }
                status = await client.call_tool("agentforge_status", {})
                assert not status.isError and status.structuredContent["available"]
        print("Installed MCP module: stdio startup, discovery, status and shutdown: OK")
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
