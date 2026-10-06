# AgentForge

Generic execution infrastructure for agents directed by an external orchestrator.
The director chooses projects, agents and workers and evaluates their results.
AgentForge supplies the execution platform; it does not intelligently route tasks
in v1.

Phase 01 establishes architecture and an importable Python package skeleton.
There is no running API, model integration, task engine, MCP server or dashboard yet.
See [the architecture](docs/architecture.md) and [coding instructions](AGENTS.md).

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
