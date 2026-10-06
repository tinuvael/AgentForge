# AgentForge

Generic execution infrastructure for agents directed by an external orchestrator.
The director chooses projects, agents and workers and evaluates their results.
AgentForge supplies the execution platform; it does not intelligently route tasks
in v1.

Phase 02 adds validated Workers and an async Ollama Provider with health checks,
generation and streaming. Phase 08 adds a local stdio MCP server; API and
dashboard behavior remain planned. Worker selection is always explicit;
configuration starts with
[the TOML example](config/workers.example.toml).
See [the architecture](docs/architecture.md) and [coding instructions](AGENTS.md).

Phase 03 adds a persistent Project Registry for external local directories, with
canonical root validation and live Git inspection. Its transport-independent API
and explicit Alembic setup are documented in
[Project Registry and persistence](docs/architecture.md#project-registry-and-persistence-phase-03).

Phase 06 adds a durable Task Engine around the bounded Phase 05 AgentRuntime.
Normal delegated execution uses explicit Project/Agent/Worker IDs through
`submit`, `get_task`, `list_tasks` and `cancel_task`, with bounded background
execution and conservative restart recovery. See
[Task Engine and durable history](docs/architecture.md#task-engine-and-durable-history-phase-06).
The [manual smoke script](docs/manual-repo-explorer.md) executes through Task Engine.

Phase 07 automatically persists terminal Task telemetry with historical target
identity, observed token/timing metrics and tool aggregates. Missing observations
remain null. `TelemetryService` provides filters and grouped comparisons without
routing or recommending Workers. See
[Task telemetry](docs/architecture.md#task-telemetry-phase-07) for exact metric
definitions, coverage rules and failure semantics. Upgrade with `alembic upgrade head`.

Phase 08 exposes eight typed MCP tools for discovery and asynchronous delegation:
`agentforge_status`, `describe_capabilities`, `list_projects`, `list_workers`,
`list_agents`, `delegate_task`, `get_task`, `cancel_task`. Start with an explicitly
configured migrated database and Worker file:

```sh
python -m agentforge.mcp.server --database-url sqlite:///agentforge.db --workers workers.local.toml
```

See [MCP setup, schemas, errors and optional smoke](docs/mcp.md). The shared
application owns one TaskEngine and database; Workers remain inference endpoints
while repository/index tools execute on the central host. MCP stdio supports
native Windows. Repository tools and Index select POSIX descriptors or a native
Windows NTFS handle backend, with fail-closed path/reparse/identity checks. See
[Windows security](docs/windows-repository-security.md) and the
[native Windows MCP smoke](docs/native-windows-smoke.md).

Use the foundation directly from Python (inside your own async function):

```python
from agentforge.core.inference import GenerationRequest, Message
from agentforge.providers.ollama import OllamaProvider
from agentforge.workers.config import load_workers

config = load_workers("config/workers.example.toml")
worker = next(worker for worker in config.workers if worker.id == "local-4080")
provider = OllamaProvider()
request = GenerationRequest(messages=[Message(role="user", content="Hello")])

health = await provider.health(worker)
result = await provider.generate(worker, request)
async with provider.stream(worker, request) as chunks:
    async for chunk in chunks:
        print(chunk.content, end="", flush=True)
```

Edit the example for your deployment. Inference requires a reachable Ollama backend
with that model installed; tests use only mocks. Streams must stay inside the async
context manager so an early stop or cancellation closes the request.

## Development

Use Python 3.12+:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
ruff format --check .
```

The package lives under `src/agentforge/`; tests live under `tests/`.
Install the package before running tests so imports exercise the installed package.
Implement subsequent functionality incrementally through GitHub issues.
