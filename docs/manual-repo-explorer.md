# Manual Repo Explorer smoke test

For native Windows MCP acceptance, use [the Windows procedure](native-windows-smoke.md).
The direct Task Engine smoke below remains available on both supported backends.

This is opt-in and never run by pytest/CI. It uses the production Task Engine,
Agent Runtime, Project Registry, Index, RepositoryTools and OllamaProvider.
You need a development venv, Git and a reachable configured Ollama endpoint with
`gpt-oss:20b` installed. The endpoint may be on another machine. Run from the
AgentForge checkout; activate the venv installed with `pip install -e '.[dev]'`.

The example Worker configuration explicitly defines `local-4080`, `gpt-oss:20b`,
`http://localhost:11434` and native tool support. Copy/edit this file when your
administrator-approved endpoint or deployment differs. No Worker is auto-selected.

Prepare the existing registry/index schema and register this checkout once:

```sh
alembic upgrade head
python - <<'PY'
from pathlib import Path
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.projects import ProjectRepository
from agentforge.db.index import IndexRepository
from agentforge.projects.service import ProjectRegistry
from agentforge.index.service import ProjectIndex

engine = create_database_engine("sqlite:///agentforge.db")
try:
    sessions = create_session_factory(engine)
    projects = ProjectRegistry(ProjectRepository(sessions))
    root = Path.cwd().resolve()
    # Reuse an existing registration, preserving its recorded root identity.
    project = next((p for p in projects.list_projects() if p.root_path == root), None)
    if project is None:
        project = projects.register_project("AgentForge", root)
    ProjectIndex(projects, IndexRepository(sessions)).refresh_index(project.id)
    print(project.id)
finally:
    engine.dispose()
PY
```

Registration and index refresh above are developer operations outside Agent
execution. The Agent has no registration, refresh or database-write tool. If an
old registration lacks root identity, explicitly remove and re-register it as
described in the architecture; the runtime will not authorize a replacement root.

Use the printed UUID explicitly:

```sh
python scripts/smoke_repo_explorer.py \
  --database-url sqlite:///agentforge.db \
  --project-id YOUR_PRINTED_PROJECT_UUID \
  --workers config/workers.example.toml \
  --worker-id local-4080
```

The selected Agent is `repo_explorer`. The default task is:

> Explain how ProjectRegistry.open_root protects repository access.
> Inspect the implementation and relevant tests before answering.

Pass `--task "Where is Ollama streaming implemented and how does cancellation work?"`
for another request. The script submits a durable Task and waits for completion.
It prints Task JSON with UUID, lifecycle/timestamps and an `execution_result`
containing the answer, explicit identities, usage when supplied, counts, sanitized
trace and termination reason. History remains in the selected database.
It then prints the terminal telemetry record, including provider/model identity,
observed tokens, timings and tool aggregates. Inspect `telemetry_status` on the
Task if telemetry could not be recorded. Missing metrics remain null; TTFT is
unavailable for this non-streaming runtime. Throughput uses observed output tokens
and backend output evaluation duration, never whole-Task runtime. The same record
can later be read with `TelemetryService(TelemetryRepository(sessions)).get_for_task(id)`.
Successful exploration should show focused source/test reads and a final answer
with path/line citations. A model answer without sufficient reads is a model
quality limitation: the director still evaluates evidence and sufficiency.

This runtime uses non-streaming native Ollama chat tool calls and sequential tools.
Some Ollama/model versions may omit native calls, emit malformed arguments, return
an empty final response, repeat tools or hit limits. Those outcomes are reported;
there is no textual tool parser, automatic model replacement or `gpt-oss` special
case. Native Ollama generally omits IDs; the adapter creates local correlation IDs
and uses native `tool_name` on wire messages. Native `message.thinking` is preserved
as opaque normalized `reasoning` on assistant history, then round-tripped by the
adapter as `thinking` on subsequent tool-result turns. AgentRuntime never
interprets this state as an instruction, tool call, authorization or final answer.
It counts against context but is excluded from execution-result JSON, trace
content and sanitized errors. Missing/null state stays absent on the wire;
non-string/non-null thinking fails safely. No reasoning UI or persistence of
reasoning is implemented; telemetry stores metadata only.
A known Worker tool capability of `False` rejects the Agent before inference;
unknown capability allows an attempt without claiming support.

Default limits are 12 model turns, 24 tool calls, a 120-second whole-run deadline,
12,000 bytes per result, 48,000 cumulative result bytes and 24,000 approximate
context tokens, further capped by a known Worker context window. The deterministic
estimate is `ceil(serialized UTF-8 bytes / 3)`, including opaque reasoning. It
bounds conversation growth, but is not a tokenizer-independent upper bound or a
guarantee of fitting every model's token window. The script uses those defaults.
A cold model load may consume the deadline. Task cancellation is
cooperative at model/tool boundaries; Ctrl-C cancels the asyncio task and preserves
Provider cleanup through Task Engine shutdown and records a safe terminal outcome.
Synchronous repository calls cannot be interrupted mid-call and are checked after
returning. Startup fails orphaned running Tasks as `execution_interrupted` and
resumes queued Tasks; started work is never replayed. The script owns the sole
Task Engine for this database; do not run it beside another control-plane process
using the same database. MCP, external schedulers, shell, repository
mutation and Agent-driven test execution remain outside this phase.
