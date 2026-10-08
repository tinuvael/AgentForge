"""Offline release smoke against the active installed package, from outside source."""

import asyncio
import importlib
import json
import os
import pkgutil
import subprocess
import sys
import sysconfig
from contextlib import redirect_stdout
from importlib import metadata, resources
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from alembic import command
from alembic.runtime.migration import MigrationContext
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import inspect

from agentforge.agents import GENERAL_AGENT, REPO_EXPLORER
from agentforge.application.definitions import shipped_agents
from agentforge.application.service import Application
from agentforge.cli import main as operator_main
from agentforge.core.worker import Worker
from agentforge.db.database import Base, create_database_engine
from agentforge.db.migrate import _configuration
from agentforge.mcp.server import TOOL_CONTRACTS, create_server
from agentforge.providers.ollama import OllamaProvider
from agentforge.web.app import create_app
from agentforge.workers.config import WorkersConfig


def operator(*args, expected=0):
    executable = Path(sysconfig.get_path("scripts")) / (
        "agentforge.exe" if os.name == "nt" else "agentforge"
    )
    result = subprocess.run(
        [str(executable), *map(str, args)], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == expected, result.stderr
    assert "Traceback" not in result.stderr
    return json.loads(result.stdout) if result.stdout.startswith("{") else result.stdout


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
    entry = metadata.distribution("agentforge").entry_points
    assert any(
        e.name == "agentforge"
        and e.value == "agentforge.cli:main"
        and e.group == "console_scripts"
        for e in entry
    )
    for arguments in (
        (),
        ("db",),
        ("project",),
        ("worker",),
        ("worker", "probe"),
        ("worker", "diagnostics"),
        ("agent",),
        ("coding",),
        ("mcp",),
        ("web",),
    ):
        assert "usage:" in operator(*arguments, "--help")
    result = subprocess.run(
        [sys.executable, "-m", "agentforge", "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "project" in result.stdout
    print("Installed package, migrations, web assets and CLI help: OK")


def offline_diagnostic_actions(config, url):
    # Actual installed CLI dispatch with offline adapters; the console script
    # exercises passive diagnostics in a separate fresh process below.
    import agentforge.cli as cli

    original = cli.create_providers
    cli.create_providers = lambda _: {
        "ollama": OllamaProvider(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"models": [{"name": "offline:latest"}]}
                    if request.method == "GET"
                    else {
                        "model": "offline",
                        "message": {"content": "agentforge"},
                        "done": True,
                        "prompt_eval_count": 5,
                        "eval_count": 2,
                        "eval_duration": 500_000_000,
                    },
                )
            )
        )
    }
    try:
        for operation in ("check", "probe"):
            output = StringIO()
            with redirect_stdout(output):
                assert (
                    operator_main(
                        [
                            "worker",
                            operation,
                            "offline",
                            "--workers",
                            str(config),
                            "--database-url",
                            url,
                        ]
                    )
                    == 0
                )
            observed = json.loads(output.getvalue())["observation"]
            assert observed["status"] == (
                "available" if operation == "check" else "successful"
            )
    finally:
        cli.create_providers = original


async def smoke():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        url = "sqlite:///" + (root / "smoke.db").as_posix()
        assert (
            operator("db", "status", "--database-url", url, expected=4)["state"]
            == "uninitialized"
        )
        assert not (root / "smoke.db").exists()
        operator("db", "upgrade", "--database-url", url)
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
            # Destructive roundtrip uses only this empty temporary smoke database.
            with engine.begin() as connection:
                migration = _configuration()
                migration.attributes["connection"] = connection
                command.downgrade(migration, "base")
                assert inspect(connection).get_table_names() == ["alembic_version"]
                assert (
                    MigrationContext.configure(connection).get_current_revision()
                    is None
                )
                command.upgrade(migration, "head")
                command.check(migration)
                assert (
                    "worker_diagnostic_observations"
                    in inspect(connection).get_table_names()
                )
        finally:
            engine.dispose()
        print(
            "Installed migration CLI, fresh initial schema and head/base roundtrip: OK"
        )
        config = root / "workers.toml"
        config.write_text(
            '[[workers]]\nid = "offline"\nprovider = "ollama"\n'
            'endpoint = "http://worker.invalid"\nmodel = "offline"\n'
            "supports_tools = true\n"
        )
        operator("worker", "config-check", "--workers", config)
        assert (
            operator("worker", "list", "--workers", config)["availability"]
            == "not_probed"
        )
        assert [a["id"] for a in operator("agent", "list")["agents"]] == [
            "general_agent",
            "repo_explorer",
        ]
        project_root = root / "project with spaces"
        project_root.mkdir()
        (project_root / "example.py").write_text("def example(): return 1\n")
        identity = operator(
            "project", "add", project_root, "--name", "Synthetic", "--database-url", url
        )["registration"]["project_id"]
        operator(
            "project",
            "add",
            project_root,
            "--name",
            "Duplicate",
            "--database-url",
            url,
            expected=5,
        )
        assert (
            operator("project", "list", "--database-url", url)["projects"][0][
                "indexed_at"
            ]
            is None
        )
        operator("project", "inspect", identity, "--database-url", url)
        assert (
            operator("project", "index", identity, "--database-url", url)["file_count"]
            == 1
        )
        operator("project", "remove", identity, "--database-url", url, expected=2)
        operator("project", "remove", identity, "--database-url", url, "--yes")
        assert operator("project", "list", "--database-url", url)["projects"] == []
        assert (project_root / "example.py").exists()
        assert operator("db", "status", "--database-url", url)["state"] == "current"
        # Example files are explicit verification inputs, never runtime defaults.
        examples = Path(__file__).resolve().parents[1] / "config"
        for example in ("workers.example.toml", "workers.providers.example.toml"):
            operator("worker", "config-check", "--workers", examples / example)
        if os.name != "nt":
            operator("coding", "show", "--coding", examples / "coding.example.toml")
        print("Installed operator setup, Project lifecycle, configuration/examples: OK")
        await asyncio.to_thread(offline_diagnostic_actions, config, url)
        last = operator(
            "worker",
            "diagnostics",
            "offline",
            "--workers",
            config,
            "--database-url",
            url,
        )
        assert last["health"]["latest"]["status"] == "available"
        assert last["generation"]["latest"]["tokens_per_second"] == 4.0
        assert last["generation"]["latest"]["ttft_seconds"] is None
        print("Installed diagnostic CLI: health/probe and passive restart snapshot: OK")
        executable = Path(sysconfig.get_path("scripts")) / (
            "agentforge.exe" if os.name == "nt" else "agentforge"
        )
        for launcher, arguments in (
            (str(executable), ["mcp"]),
            (sys.executable, ["-m", "agentforge.mcp.server"]),
        ):
            parameters = StdioServerParameters(
                command=launcher,
                args=[*arguments, "--database-url", url, "--workers", str(config)],
            )
            async with asyncio.timeout(15), stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    assert {
                        tool.name for tool in (await client.list_tools()).tools
                    } == {contract.name for contract in TOOL_CONTRACTS}
                    status = await client.call_tool("agentforge_status", {})
                    assert not status.isError and status.structuredContent["available"]
                    assert status.structuredContent["agent_count"] == 2
                    agents = await client.call_tool("list_agents", {})
                    assert not agents.isError
                    assert [
                        a["agent_id"] for a in agents.structuredContent["agents"]
                    ] == [
                        "general_agent",
                        "repo_explorer",
                    ]
        print(
            "Installed MCP console script/module: stdio startup, discovery, "
            "status and shutdown: OK"
        )
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
        assert [a.id for a in shipped_agents(coding_enabled=True)] == [
            "general_agent",
            "repo_explorer",
            "coder",
        ]
        # Listing validates configuration syntax, not coding host readiness.
        # Use an absent Git path; no coding runtime is constructed by this check.
        coding_path = root / "coding.toml"
        coding_path.write_text(
            "workspace_parent=" + json.dumps(str(root / "coding workspaces")) + "\n"
            "git_executable="
            + json.dumps(str(root / "not-installed" / "git.exe"))
            + "\n"
        )
        assert [
            a["id"]
            for a in operator("agent", "list", "--coding", coding_path)["agents"]
        ] == ["general_agent", "repo_explorer", "coder"]
        print("Coding-enabled installed definitions/CLI discovery (syntax only): OK")
        requests = []
        final = (
            "README.md:1 specifies one process per database; operations.txt:1 "
            "places two services on separate databases. The notes are consistent."
        )

        def scripted(request):
            payload = json.loads(request.content)
            requests.append(payload)
            assert {t["function"]["name"] for t in payload["tools"]} == set(
                GENERAL_AGENT.allowed_tools
            )
            if len(requests) == 1:
                assert payload["messages"][0]["content"] == GENERAL_AGENT.system_prompt
                message = {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"path": path}}}
                        for path in ("README.md", "operations.txt")
                    ],
                }
            else:
                assert len(requests) == 2
                evidence = [
                    json.loads(m["content"])
                    for m in payload["messages"]
                    if m["role"] == "tool"
                ]
                assert len(evidence) == 2 and all(body["ok"] for body in evidence)
                assert [body["result"]["content"] for body in evidence] == [
                    "Deployment uses one process per database.\n",
                    "Run two services on separate databases.\n",
                ]
                message = {"content": final}
            return httpx.Response(
                200, json={"model": "offline", "message": message, "done": True}
            )

        provider = OllamaProvider(transport=httpx.MockTransport(scripted))
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
            assert [a.agent_id for a in app.list_agents().agents] == [
                "general_agent",
                "repo_explorer",
            ]
            agents = await client.call_tool("list_agents", {})
            assert not agents.isError
            for observed, definition in zip(
                agents.structuredContent["agents"],
                (GENERAL_AGENT, REPO_EXPLORER),
                strict=True,
            ):
                assert observed["agent_id"] == definition.id
                assert observed["allowed_tools"] == list(definition.allowed_tools)
                assert observed["workspace_mode"] == "project_readonly"
                assert observed["limits"] == definition.limits.model_dump()
            (project_root / "README.md").write_text(
                "Deployment uses one process per database.\n"
            )
            (project_root / "operations.txt").write_text(
                "Run two services on separate databases.\n"
            )
            before = {path.name: path.read_bytes() for path in project_root.iterdir()}
            project = app.projects.register_project("Synthetic", project_root)
            result = await client.call_tool(
                "delegate_task",
                {
                    "project_id": str(project.id),
                    "agent_id": "general_agent",
                    "worker_id": "offline",
                    "task": (
                        "Compare README.md and operations.txt "
                        "for deployment contradictions."
                    ),
                },
            )
            assert not result.isError
            task = await app.tasks.wait_task(result.structuredContent["task_id"])
            assert task.state == "completed" and task.final_answer == final
            execution = task.execution_result
            assert execution.steps == 2 and execution.tool_call_count == 2
            assert (
                execution.tool_output_bytes
                <= GENERAL_AGENT.limits.max_tool_output_bytes
            )
            assert app.telemetry.get_for_task(task.task_id).model_call_count == 2
            assert {
                path.name: path.read_bytes() for path in project_root.iterdir()
            } == before
        print("Application/MCP discovery, General Agent document Task, telemetry: OK")
        web = create_app(factory)
        async with web.router.lifespan_context(web):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=web), base_url="http://localhost"
            ) as client:
                for path in ("/", "/workers", "/projects", "/tasks", "/councils"):
                    response = await client.get(path)
                    assert response.status_code == 200, path
                    if path == "/":
                        assert (
                            "Configured Agents</span><strong>2</strong>"
                            in response.text
                        )
                    if path == "/workers":
                        assert "Configuration" in response.text
                        assert "Last observed diagnostics" in response.text
                assert (await client.get("/static/dashboard.css")).status_code == 200
        print("Dashboard migrated startup, pages, static assets and shutdown: OK")


if __name__ == "__main__":
    package_checks()
    asyncio.run(smoke())
