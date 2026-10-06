"""Optional real MCP/Ollama smoke; never run by the automated test suite."""

import argparse
import asyncio
import json
import sys
from time import monotonic

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.telemetry import TelemetryRepository
from agentforge.telemetry.models import TelemetryError
from agentforge.telemetry.service import TelemetryService


async def smoke(arguments):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agentforge.mcp.server",
            "--database-url",
            arguments.database_url,
            "--workers",
            arguments.workers,
        ],
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, values=None):
                result = await client.call_tool(name, values or {})
                if result.isError:
                    # Only AgentForge's fixed safe error contract is printed.
                    print(result.content[0].text, file=sys.stderr)
                    raise RuntimeError("MCP tool failed")
                return result.structuredContent

            for name in (
                "agentforge_status",
                "describe_capabilities",
                "list_projects",
                "list_workers",
                "list_agents",
            ):
                print(json.dumps(await call(name), indent=2))
            submitted = await call(
                "delegate_task",
                dict(
                    project_id=arguments.project_id,
                    agent_id="repo_explorer",
                    worker_id=arguments.worker_id,
                    task=arguments.task,
                ),
            )
            print(json.dumps(submitted, indent=2))
            identity = {"task_id": submitted["task_id"]}
            deadline = monotonic() + arguments.poll_timeout
            while True:
                result = await call("get_task", identity)
                if result["state"] in {"completed", "failed", "cancelled"}:
                    print(json.dumps(result, indent=2))
                    return result
                if monotonic() >= deadline:
                    print(json.dumps(await call("cancel_task", identity), indent=2))
                    raise RuntimeError(
                        "Smoke polling deadline reached; cancellation requested"
                    )
                await asyncio.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--workers", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--poll-timeout", type=float, default=180.0)
    arguments = parser.parse_args()
    if arguments.poll_timeout <= 0:
        parser.error("--poll-timeout must be positive")
    try:
        result = asyncio.run(smoke(arguments))
    except Exception:
        print(
            "Smoke did not complete; inspect the safe tool status above.",
            file=sys.stderr,
        )
        return 1
    # The server has exited; read observational telemetry, without another executor.
    database = create_database_engine(arguments.database_url)
    try:
        telemetry = TelemetryService(
            TelemetryRepository(create_session_factory(database))
        )
        try:
            print(telemetry.get_for_task(result["task_id"]).model_dump_json(indent=2))
        except TelemetryError:
            print("Telemetry unavailable; no values fabricated.", file=sys.stderr)
    finally:
        database.dispose()
    return 0 if result["state"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
