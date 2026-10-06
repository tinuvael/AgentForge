# MCP for external directors (Phase 08)

AgentForge is the execution/control plane. A trusted local MCP director (Codex,
ChatGPT/Astra, Claude, or another MCP client) chooses **Project, Agent, Worker and
request explicitly**. AgentForge validates that binding and submits a durable
Task. The director evaluates results and decides when to poll, cancel, or explicitly
submit another Task. There is no routing, Worker ranking, fallback, judging or Council.

## Installation and startup

Requires Python 3.12+ and the official Python MCP SDK (`mcp>=1.30,<2`, tested with
1.30.0). AnyIO (`>=4.7,<5`) is the SDK async support library, used for lifespan
cancellation shielding and the server entry point. The low-level SDK Server provides protocol handling, tool registration,
stdio and lifespan. No custom JSON-RPC or HTTP/SSE service is added.

From the checkout, install in a venv. Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

On POSIX, use `.venv/bin/python` in place of `.venv\Scripts\python.exe`.
Before startup, explicitly migrate the database using Alembic. The existing
`alembic.ini` uses `sqlite:///agentforge.db`. To use another file, copy that config
and set its `sqlalchemy.url`; pass **the same database URL** to MCP. Run from the
checkout so Alembic can find the existing `migrations/` directory:

```powershell
.venv\Scripts\alembic.exe -c alembic.ini upgrade head
```

Copy `config/workers.example.toml` to an ignored local file (for example
`workers.local.toml`) and edit the configured inference targets. The server requires
an explicit database URL and Worker file; it never discovers configuration or
creates/migrates tables during startup:

```powershell
.venv\Scripts\python.exe -m agentforge.mcp.server --database-url sqlite:///agentforge.db --workers workers.local.toml
```

Optional `--concurrency` is 1–32 (default 1). It limits executor work, not selection
of Workers. Resolve relative database/Worker file paths against the server's
working directory; absolute paths are preferable in a director configuration.
Projects come only from the existing durable ProjectRegistry. The shipped Agent
is the actual `REPO_EXPLORER` definition; the shipped Provider is Ollama. No second
Provider or Agent loading framework is introduced. Unsupported Provider bindings
are rejected before creating Tasks.

For an initially empty registry, use the existing Python registration interface
once, pointing at a directory you intend to authorize:

```python
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.projects import ProjectRepository
from agentforge.projects.service import ProjectRegistry

engine = create_database_engine("sqlite:///agentforge.db")
try:
    registry = ProjectRegistry(ProjectRepository(create_session_factory(engine)))
    project = registry.register_project(
        "Example", "/absolute/path/to/authorized/project"
    )
    print(project.id)
finally:
    engine.dispose()
```

Use a Windows absolute path when registering on Windows. This is administrator
setup, not an MCP tool. No MCP caller can supply or register a project root.
Index refresh remains an explicit administrator Python operation with
`ProjectIndex(registry, IndexRepository(sessions)).refresh_index(project.id)`;
startup/discovery never scans project files. Repo Explorer's cached map can be empty
or stale; its allowlisted source tools provide evidence.

## Connecting a director

Configure your client's **stdio MCP server** with an absolute interpreter path,
arguments, and a working directory. A representative client configuration (adapt
its outer wrapper to your client) is:

```json
{
  "mcpServers": {
    "agentforge": {
      "command": "C:/path/to/AgentForge/.venv/Scripts/python.exe",
      "args": [
        "-m", "agentforge.mcp.server",
        "--database-url", "sqlite:///C:/path/to/state/agentforge.db",
        "--workers", "C:/path/to/config/workers.local.toml"
      ]
    }
  }
}
```

These are placeholders, not machine-specific Project paths or IDs. Use a POSIX
interpreter/path syntax when appropriate. The client launches the process and
exchanges MCP messages over stdin/stdout. Diagnostics use sanitized stderr logging (including SDK validation logs); no startup
banner or application logs go to stdout. SDK stdio wraps the binary handles as
UTF-8, including on Windows. No POSIX signal handling is introduced in MCP.

Use **one server process per database**, including while running smoke scripts.
The existing TaskEngine rejects duplicate ownership in one process. There is no
cross-process lease; do not connect two directors by launching two processes
against the same database. Local stdio inherits the trust of the launching user;
there is no remote listener, authentication platform, or remote exposure contract.

## Public typed tool contracts

The SDK `tools/list` response provides JSON Schema for every input and output,
generated from explicit Pydantic models in `application/contracts.py`.
Unknown arguments are rejected. UUIDs are JSON strings with `format: uuid`.
IDs are strict nonempty strings of at most 100 characters. Requests are strict
nonblank strings of at most 32,768 characters.

| Tool | Input object | Successful output |
| --- | --- | --- |
| agentforge_status | `{}` | `Status`: availability, version, database availability, TaskEngine availability, registered Project/configured Worker/Agent counts, queued/running Task counts |
| describe_capabilities | `{}` | `Capabilities`: responsibility, explicit selection rule, Agent/Worker discovery tool names, Task operation names, read-only central repository boundary, telemetry availability, fixed limitations |
| list_projects | `{limit?: integer=100, offset?: integer=0}` | `ProjectsPage`: `projects` and nullable `next_offset` |
| list_workers | `{limit?: integer=100, offset?: integer=0}` | `WorkersPage`: `workers` and nullable `next_offset` |
| list_agents | `{limit?: integer=100, offset?: integer=0}` | `AgentsPage`: `agents` and nullable `next_offset` |
| delegate_task | `{project_id: UUID, agent_id: string, worker_id: string, task: string}`; **all required** | `TaskSnapshot`: durable queued identity/snapshot; does not wait for inference |
| get_task | `{task_id: UUID}` | `TaskSnapshot`: persisted current state and completed answer |
| cancel_task | `{task_id: UUID}` | `TaskSnapshot`: resulting persisted state/cancellation request |

Page limits are 1–100; offsets are 0–1,000,000. Fetch `next_offset` until null.
Projects order by creation time/UUID; Workers and Agents order by ID, without
ranking. Offset pagination is stable for an unchanged configuration/registry;
concurrent registration/removal can change page contents.

`ProjectInfo` contains `project_id`, `name`, `root_path`, `created_at`,
`git_status="unavailable"`, and `git_unavailable_reason="not_probed"`. Root paths
are intentional metadata for the trusted local director. Discovery does not probe
Git or read arbitrary files, including when directories are missing. No Git branch
or commit is invented; live Git inspection stays in the existing Registry Python
interface/allowlisted Agent tools.

`WorkerInfo` contains `worker_id`, `provider`, `model`, nullable `context_window`,
nullable `supports_tools`, `supports_streaming`, nullable `deployment_label`, and
`health_status="not_probed"`. All capabilities come from the same configuration
used by execution. Unknown tool support remains null. Endpoints and provider options
are **omitted**, including endpoint paths, userinfo and API keys. Phase 08 has no
health-probe argument and never claims a configured Worker is online.

`AgentInfo` contains `agent_id`, `name`, `description` (at most 2,000 characters),
`allowed_tools`, and `limits`: `max_steps`, `timeout_seconds`, `max_tool_calls`,
`max_tool_result_bytes`, `max_tool_output_bytes`, `max_context_tokens`. System prompts
and hidden runtime/model state are omitted. Definitions come from application
configuration, not duplicated MCP definitions. Discovery fields/collections are
bounded; oversized Worker identity/metadata configuration fails safely at startup.

`TaskSnapshot` contains:

- `task_id`, `project_id`, `agent_id`, `worker_id`, `state`;
- UTC `created_at`, `updated_at`, nullable `started_at`, `finished_at`,
  `cancellation_requested_at`;
- nullable `reason` and `error_code` using the existing fixed Task reason contract;
- nullable `execution_summary` containing `steps`, `tool_call_count`, `tool_output_bytes`;
- nullable `final_answer`, present only for completed Tasks;
- `telemetry_status`: `pending`, `recorded`, or `unavailable`.

States are `queued`, `running`, `completed`, `failed`, `cancelled`. The actual
completed answer is preserved according to the existing runtime/Task contract;
MCP neither silently truncates it nor attaches unrelated history. No request,
trace, tool arguments/results, per-turn history, raw provider response, or reasoning
is attached. The intentional final answer can contain repository evidence chosen
by the Agent; it remains untrusted model output, not authorization.

Phase 07 telemetry is recorded by the same TaskRepository checkpoints. MCP exposes
coverage status only, no invented metrics. `recorded` does not mean every metric is
known. Actual observational metrics (nullable counts/timings with coverage rules)
remain available through the composed `TelemetryService` Python interface and the
optional smoke script. No telemetry routing policy or analytics MCP API is added.

## Asynchronous interaction and cancellation

Example MCP tool arguments (replace IDs with discovery output):

```text
list_projects({})
list_workers({})
list_agents({})

delegate_task({
  "project_id": "<registered UUID>",
  "agent_id": "repo_explorer",
  "worker_id": "<explicit configured Worker ID>",
  "task": "Inspect the repository entry point and cite source evidence."
})
# -> {"task_id": "...", "state": "queued", ...}

get_task({"task_id": "<returned UUID>"})
# -> running or terminal; the director chooses its polling cadence/deadline.

cancel_task({"task_id": "<returned UUID>"})
```

`delegate_task` calls **TaskEngine.submit**, never AgentRuntime directly. Invalid
bindings fail before creation; submission failures are actual failed tool results.
There is no default Worker or fallback. Repeating successful delegation creates
another Task: if a connection fails after a commit, inspect durable history through
the existing Python service before retrying blindly; Phase 08 has no idempotency key
or Task-history MCP endpoint. `get_task` works across requests and service restarts
using durable storage, not an MCP in-memory store.

Queued cancellation is immediately terminal and prevents execution. Running
cancellation records a durable cooperative request and may still return `running`;
poll until terminal. The token is checked at runtime boundaries, not a remote
inference force-kill. Terminal cancellation leaves the Task unchanged; committed
cancellation wins a late completion race and discards that answer. Closing local
HTTP resources does not guarantee remote generation stopped.

## Safe error boundary

Tool failures return MCP `isError=true` with a JSON text content object:

```json
{"code":"worker_not_found","message":"Select a configured worker_id from list_workers."}
```

They never masquerade as successful Task snapshots. Successful calls also provide
SDK `structuredContent` conforming to the advertised output schema. Error results
have no success `structuredContent`; parse their JSON content for the safe code.

| Code | Meaning |
| --- | --- |
| `invalid_arguments` | Missing/extra arguments, malformed UUID, invalid types/bounds/blank request, or unsupported tool name |
| `project_not_found` | Well-formed UUID not registered |
| `worker_not_found` | Worker ID absent from application configuration |
| `agent_not_found` | Agent ID absent from application configuration |
| `invalid_execution_binding` | Incompatible Agent tools/Worker capabilities or unavailable/mismatched Provider configuration |
| `task_not_found` | Well-formed UUID absent from durable Task storage |
| `storage_unavailable` | Safe storage failure, including schema/database access failures |
| `service_unavailable` | Executor/application lifecycle unavailable |
| `internal_error` | Unexpected operation/response failure; generic diagnostic |

A **created** Task's execution failure uses its durable safe `error_code`, such as
`provider_error`, `provider_timeout`, `security_error`, `invalid_configuration`,
`execution_interrupted`, or `executor_cancelled`, rather than an MCP operation
error. Error mapping never serializes exception strings/repr, SQL, provider bodies,
repository contents or secrets. Argument-validation diagnostics deliberately omit
caller values. Startup/shutdown failures print only a generic stderr diagnostic
and exit nonzero.

## Composition, lifecycle and central-host security

`Application.from_config` constructs one database engine/session factory,
ProjectRegistry, ProjectIndex, RepositoryTools, actual Agent/Worker definitions,
Provider mapping, AgentRuntime, TaskRepository/TaskEngine and TelemetryService.
The MCP SDK lifespan constructs it once, starts TaskEngine once and shares it
across calls on the owning thread/event loop. Startup applies Phase 06 recovery:
running orphans fail as `execution_interrupted`, queued Tasks remain eligible,
and lost inference is never replayed. Shutdown awaits local executor/provider
cleanup, preserves queued rows, resolves active work as `executor_cancelled`
(or cancelled if explicit cancellation already won), and disposes the database.
Cleanup is shielded from client disconnect/cancellation. Forced process death
cannot run cleanup; the next exclusive startup applies recovery.

All Project files, index queries and repository tools execute on the **central
AgentForge host**. Workers are inference endpoints; they need no mounted Project,
repository checkout, remote file synchronization or local filesystem access.
Worker HTTP(S) endpoints come from administrator configuration and can be local,
LAN/VPN, or cloud destinations. Neither MCP nor generic application logic assumes
localhost or reads Ollama-specific response fields. Only the existing concrete
Ollama Provider handles that protocol; other protocols still require future adapters.

The public surface is exactly these eight tools. There are no generic file-read,
shell, arbitrary Git, SQL, environment, direct Provider HTTP, or registration tools.
The director delegates to an Agent whose allowlisted tools are enforced by
AgentRuntime with server-side Project binding. Registered-root containment,
root identity checks, exclusions and source budgets remain unchanged.

**Windows limitation:** MCP startup/discovery/stdio and durable Task operations are
portable. Existing secure filesystem primitives require POSIX no-follow
descriptors (Git observation additionally uses Linux descriptor cwd). Native Windows
Repo Explorer execution therefore fails closed with a safe security/tool error;
Phase 08 does not weaken this boundary. A successful source-tool smoke currently
requires running the central AgentForge process and registered Project in a
supported POSIX/Linux environment, such as WSL on the Windows workstation. Use a
separate migrated database and re-register the WSL project root there; Windows
registration paths/identities must not be repurposed. Configure a reachable Windows
Ollama host address from WSL rather than assuming WSL localhost equals Windows.
A remote Worker cannot remedy unsupported filesystem primitives on the central host.

## Optional manual end-to-end smoke

This uses real inference and is **not part of pytest**. Ensure only one executor
owns the database. Configure an explicitly selected Ollama Worker with native tool
support; for the current deployment this may be `local-4080` using `gpt-oss:20b`,
provided those are your actual configuration values. Install the model and check
reachability yourself. Register the authorized repository on the central host and
optionally refresh its Python index. Observe the Windows limitation above first.

From a supported central host, the optional script is an actual external SDK MCP
client which launches the server, discovers its surface, delegates to Repo Explorer,
polls with a 180-second deadline, prints the durable result, then reads its recorded
telemetry after the server exits:

```sh
python scripts/smoke_mcp.py --database-url sqlite:///agentforge.db --workers workers.local.toml --project-id '<registered UUID>' --worker-id '<configured Worker ID>' --task 'Inspect the repository entry point, use source tools, and cite paths and lines.'
```

Run with the installed venv interpreter. Success shows the explicitly selected
Worker, completed final answer, positive tool-call count for a source-tool request,
and telemetry with actual known/unknown coverage. A failed result is still durable;
inspect its safe reason. Missing telemetry is reported without inventing values.
For native Windows, first use your normal external director to verify discovery
and asynchronous Task operations; repository execution remains blocked as documented.

To repeat with a remote Ollama Worker, add another entry to the **same central
Worker file**, with a reachable LAN/VPN endpoint, installed model, explicit tool
support and factual deployment label. Restart the MCP server to reload configuration,
verify it through `list_workers`, then run the same interaction with that Worker ID.
Project registration and files stay on the central host; no remote checkout is needed.
Allow the configured inference timeout and RuntimeLimits to cover your request.
The director decides whether a result warrants another Worker; the server never
substitutes one. A cloud protocol other than Ollama is discoverable as configuration
but cannot execute until a matching Provider adapter exists.
