# MCP for external directors

AgentForge is the execution/control plane. A trusted local MCP director (Codex,
ChatGPT/Astra, Claude, or another MCP client) chooses **Project, Agent, Worker and
request explicitly**. AgentForge validates that binding and submits a durable
Task. The director evaluates results and decides when to watch, poll, cancel, or explicitly
submit another Task or an independent Council. There is no routing, Worker ranking,
fallback or judging. Councils require 2–16 explicit participants.

## Installation and startup

Requires Python 3.12+ and the official Python MCP SDK (`mcp>=1.30,<2`). AnyIO
(`>=4.7,<5`) supports lifespan cancellation shielding and the server entry point.
The low-level SDK Server provides protocol handling, tool registration, stdio and
lifespan. MCP uses SDK JSON-RPC over stdio; optional `--companion` additionally
serves the existing FastAPI dashboard/Companion HTTP and SSE surface on loopback.

From the checkout, install in a venv. Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

On POSIX, use `.venv/bin/python` in place of `.venv\Scripts\python.exe`.
Before startup, explicitly upgrade the selected database. The packaged migration
command works from a source checkout or wheel; pass **the same database URL** to
MCP. Source contributors can also use root `alembic.ini` for Alembic operations:

```powershell
.venv\Scripts\python.exe -m agentforge.db.migrate --database-url sqlite:///agentforge.db
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
Projects come only from the existing durable ProjectRegistry. The shipped read-only
Agents are General Agent (`general_agent`) and Repo Explorer (`repo_explorer`);
explicit coding configuration adds `coder`. Ollama and OpenAI-compatible Chat
Completions Providers are shipped. No dynamic Provider or Agent loading framework
is introduced. Unsupported Provider bindings
are rejected before creating Tasks.

For an initially empty registry, use the trusted [operator CLI](operator-cli.md)
with an explicit path you intend to authorize:

```sh
agentforge project add /absolute/path/to/authorized/project --name Example --database-url sqlite:///agentforge.db
agentforge project index <PROJECT_UUID> --database-url sqlite:///agentforge.db
```

On Windows use an ordinary drive-absolute path such as `'C:\Projects\AgentForge'`;
dot components (`.`/`..`) and `./...` are rejected. These are operator
commands; no MCP caller can supply or register a host root. Registration and Index
refresh are explicit. Startup/discovery never scans project files. Repo Explorer's
cached map can be empty or stale; its allowlisted source tools provide evidence.
`agentforge mcp` shares the supported module entrypoint's arguments and lifecycle.
The existing interpreter-based client configuration below remains supported.

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
cross-process enforcement; a second process can start and recovery can interrupt
the first owner's live Tasks and affect coding workspace state. Do not connect two
directors by launching two processes against the same database. Local stdio inherits
the trust of the launching user. There is no HTTP MCP listener. Optional combined
HTTP exposes trusted operator routes on loopback only, with no authentication layer.

## Public typed tool contracts

The SDK `tools/list` response provides JSON Schema for every input and output,
generated from explicit Pydantic models in `application/contracts.py` and
`councils/models.py`.
Unknown arguments are rejected. UUIDs are JSON strings with `format: uuid`.
IDs are strict nonempty strings of at most 100 characters. Requests are strict
nonblank strings of at most 32,768 characters.

| Tool | Input object | Successful output |
| --- | --- | --- |
| agentforge_status | `{}` | `Status`: availability, version, database availability, TaskEngine availability, registered Project/configured Worker/Agent counts, queued/running Task counts |
| describe_capabilities | `{}` | `Capabilities`: responsibility, explicit selection rule, Agent/Worker discovery tool names, Task/Council operation names, Council participant maximum, central repository boundary and optional isolated-write operations, telemetry availability, fixed limitations |
| list_projects | `{limit?: integer=100, offset?: integer=0}` | `ProjectsPage`: `projects` and nullable `next_offset` |
| list_workers | `{limit?: integer=100, offset?: integer=0}` | `WorkersPage`: `workers` and nullable `next_offset` |
| list_agents | `{limit?: integer=100, offset?: integer=0}` | `AgentsPage`: `agents` and nullable `next_offset` |
| delegate_task | `{project_id: UUID, agent_id: string, worker_id: string, task: string}`; **all required** | `TaskSnapshot`: durable queued identity/snapshot; does not wait for inference |
| get_task | `{task_id: UUID}` | `TaskSnapshot`: persisted current state and completed answer |
| watch_task | `{task_id: UUID}` | `TaskProgress`: bounded safe metadata; with request `_meta.progressToken`, progress until terminal/shutdown/unavailable; without a token, latest snapshot promptly |
| cancel_task | `{task_id: UUID}` | `TaskSnapshot`: resulting persisted state/cancellation request |
| delegate_council | `{project_id: UUID, agent_id: string, task: string, worker_ids: string[]}`; **all required**, 2–16 distinct Workers | `CouncilSnapshot`: durable identity and ordered queued participants; returns promptly |
| get_council | `{council_id: UUID}` | `CouncilSnapshot`: current participant outcomes/answers, state counts and terminal flag |
| cancel_council | `{council_id: UUID}` | `CouncilSnapshot`: cancel remaining participants through ordinary Task semantics |
| get_coding_workspace | `{task_id: UUID}` | `CodingResult`: identity, state, branch/base and bounded factual observations; private workspace paths omitted, validation captures untrusted |
| get_coding_diff | `{task_id: UUID}` | `CodingDiff`: bounded current diff, changed paths, statistics and truncation |
| cleanup_coding_workspace | `{task_id: UUID, workspace_id: UUID}` | `CodingResult`: explicit terminal-workspace removal; discards uncommitted edits, retains the branch |

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
are **omitted**, including endpoint paths, userinfo and API keys. MCP has no
health-probe argument and never claims a configured Worker is online.

`AgentInfo` contains `agent_id`, `name`, `description` (at most 2,000 characters),
`allowed_tools`, `workspace_mode`, and `limits`: `max_steps`, `timeout_seconds`, `max_tool_calls`,
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
- nullable bounded `coding_result`, including workspace state, change counters and
  validation observations for coding Tasks;
- nullable `final_answer`, present only for completed Tasks;
- `telemetry_status`: `pending`, `recorded`, or `unavailable`.

States are `queued`, `running`, `completed`, `failed`, `cancelled`. The actual
completed answer is preserved according to the existing runtime/Task contract;
MCP neither silently truncates it nor attaches unrelated history. Requests,
execution traces, per-turn history, raw provider responses and private reasoning
are omitted. Coding results intentionally include bounded validation output; see
[coding behavior](coding.md) for its exposure and limits. The final answer can
contain repository evidence chosen by the Agent; it remains untrusted model
output, not authorization.

Terminal Task telemetry is recorded by the same TaskRepository checkpoints. MCP exposes
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
the existing Python service before retrying blindly; MCP has no idempotency key
or Task-history MCP endpoint. `get_task` works across requests and service restarts
using durable storage, not an MCP in-memory store.

Queued cancellation is immediately terminal and prevents execution. Running
cancellation records a durable cooperative request and may still return `running`;
poll until terminal. The token is checked at runtime boundaries, not a remote
inference force-kill. Terminal cancellation leaves the Task unchanged; committed
cancellation wins a late completion race and discards that answer. Closing local
HTTP resources does not guarantee remote generation stopped.

## Live safe Task progress

`delegate_task` still commits and returns the queued identity promptly. Start a
separate `watch_task` request to observe that Task; `get_task` polling remains fully
supported and provides the final answer. A watch is a read operation and does not
start, select, retry or cancel execution. Council participants are ordinary Tasks:
use participant `task_id` values from `get_council` to watch them individually.
There is no additional Council progress protocol or local judging.

### Official SDK capability discovery

The installed official MCP Python SDK **1.30.0**, within the declared
`mcp>=1.30,<2` range, supports `Server.request_context` in low-level `call_tool`
handlers and `context.session.send_progress_notification`. The context is
request-local and supplies `meta`, `session` and `request_id`. Standard progress
is associated with the client's **outstanding request** by
`params._meta.progressToken` (string or integer, including zero), not a Task ID
argument. Progress has no separate client capability flag. The public
`send_notification` API uses standard typed MCP notifications; AgentForge does
not introduce arbitrary notification methods or a custom JSON-RPC extension.
No experimental SDK Task API is used.

`ClientSession.call_tool(progress_callback=...)` supplies a token and dispatches
notifications to that callback while the request is outstanding. It removes the
callback when the request ends. Standard progress has no durable replay, resume
cursor or reconnect guarantee. These semantics require a separate watch request
rather than sending progress after `delegate_task` has returned. In-memory official
SDK integration tests prove context/token isolation, delivery, terminal response
and protocol cancellation against 1.30.0.

### Watching and the safe contract

Python SDK client example (inside an initialized `ClientSession`):

```python
import json


async def observe(progress, total, message):
    update = json.loads(message)
    # Replace your previous metadata view with this bounded snapshot.
    print(update["task_id"], update["state"], update["timeline"])


submitted = await client.call_tool("delegate_task", binding)
task_id = submitted.structuredContent["task_id"]
watched = await client.call_tool(
    "watch_task", {"task_id": task_id}, progress_callback=observe
)
answer = await client.call_tool("get_task", {"task_id": task_id})
```

`notifications/progress.params.message` is a JSON-encoded `TaskProgress` object,
also used as the watch's normal typed tool result. `progress` increases once per
delivered metadata update within this watch; it is **not** a step count, completion
percentage or replay cursor. `total` is omitted because completion work is unknown.
Other clients supply `_meta.progressToken` on the `tools/call` request, outside
`arguments`, and handle the standard notification. Clients without a token receive
the current safe metadata promptly, with no subscription or notifications. Clients
that send a token but ignore notifications still wait for the watch to return.
AgentForge exposes supported MCP progress semantics usable by compatible Directors;
**Codex progress display has not been verified**. The Codex Companion UI is #32.

`TaskProgress` contains only:

- `task_id`, current `state`, fixed `reason`/`error_code`,
  `cancellation_requested` and `terminal`;
- `observation`: `snapshot`, `refresh`, `resync`, `terminal`, `shutdown` or `unavailable`;
- `resync_required`, `truncated`, and a replacement `timeline` of at most 100
  safe events. Each event contains `step`, `kind`, bounded `tool_name`, optional
  `success`, allowlisted `error_code`, observed `duration_seconds` and fixed `reason`.

The timeline is the existing TaskObserver projection, also used by the dashboard.
Running events include model/tool request and response/result metadata. Tool names
are permitted; arguments and bodies are absent. Coding Tasks add
`workspace_provisioned` after creation/binding (step 0), `validation_started` just
before an authorized validator invocation, and `validation_completed` when a run
is captured. Validator success means exit zero without timeout or cancellation;
it is separate from `tool_result.success`, which indicates that the tool returned
normally. Missing durations stay null. A spawn/provisioning failure does not
fabricate successful lifecycle events. No new database schema is needed.

### Bounds, reconnect and lifetime

Every connection begins with an authoritative reload and `resync_required=true`.
The shared observer limits all dashboard/Director subscribers to 128, with 16
queued hints per subscriber; subscriber exhaustion returns safe
`service_unavailable`. Slow readers never block execution. Queue overflow discards
hints and requests `resync`; dropping a timeline prefix sets `truncated=true` and
also requests resync. Replace the local view on resync; `get_task` can obtain the
normal authoritative result. Notifications can coalesce factual transitions, so
they are not a complete event history. Reconnect by starting a new watch for the
same Task ID. There is no unbounded event log or durable replay.

A terminal Task returns immediately without allocating a subscriber (with a token,
one terminal progress notification is also sent). When an active watch observes a
terminal checkpoint it sends the safe terminal snapshot and returns it, projecting
the durable bounded result trace. If execution failed without a durable result,
the prior ephemeral timeline may be unavailable. Observer shutdown closes the
watch with `observation="shutdown"`, resync required, and the latest readable
state; it need not be terminal. Queued Tasks survive executor shutdown and local
active execution is cancelled under the existing TaskEngine policy. Reconnect or
poll after restart for authoritative state.

An executor becoming unavailable before terminal persistence closes all active
and queued watches with `observation="unavailable"`, `resync_required=true` and
the latest readable durable state. A running or queued row is not fabricated as
terminal. New nonterminal watches return `service_unavailable` after observer
closure. If even the snapshot cannot be read, the adapter returns the existing
safe storage error. A normal `terminal` observation always means durable terminal
state; local executor exit alone cannot produce it.

Use standard `notifications/cancelled` with the **watch request ID** to stop a
watch; this releases its subscription and does not call `cancel_task`. Client
transport disconnect also cancels outstanding handlers and releases subscribers.
The Python SDK's local call timeout/coroutine cancellation alone does not send a
protocol cancellation notification in 1.30.0: send one explicitly, or close the
session/transport. Choose the client's request timeout to cover the desired watch
lifetime. Cancelling the actual Task remains the separate `cancel_task` operation:
queued Tasks become terminal immediately; running Tasks can report a cancellation
request before reaching their eventual cancelled checkpoint.

### Privacy and limitations

Progress omits requests, final/generated assistant text, chain-of-thought,
reasoning, raw messages, tool arguments/call IDs/results, search/path arguments,
source/file/diff contents, validation argv/stdout/stderr, workspace paths, Provider
URLs/options, credentials/environment, exception strings/repr/tracebacks and SQL.
Unknown trace error strings map to `internal_error`. Final answers and intentional
coding inspection data continue through their existing normal contracts. No
token streaming, remote MCP server, generic subscription API or persistent event
store is added. Observation failure cannot fail or delay Task execution.

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
across calls on the owning thread/event loop. Startup applies recovery:
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
localhost or reads Ollama-specific response fields. Protocol handling stays in
concrete Providers. See [Provider guidance](providers.md).

The public surface consists of the fifteen tools in the contract table. Coding
operations are advertised even when coding is disabled, and then return safe errors. There are no generic file-read,
shell, arbitrary Git, SQL, environment, direct Provider HTTP, or registration tools.
The director delegates to an Agent whose allowlisted tools are enforced by
AgentRuntime with server-side Project binding. Registered-root containment,
root identity checks, exclusions and source budgets remain unchanged.

**Native Windows:** repository tools and Index use opened local NTFS handles,
with registered volume/file identity, no reparse traversal and pinned ancestry.
Windows Git runs against an isolated bounded snapshot and validates subproject paths.
Unsupported filesystems/path constructs fail closed. No WSL or remote Worker
filesystem access is required. See [the security model](windows-repository-security.md)
and [native Windows acceptance](native-windows-smoke.md).

## Optional manual end-to-end smoke

This uses real inference and is **not part of pytest**. Ensure only one executor
owns the database. Configure an explicitly selected Ollama Worker with native tool
support; for the current deployment this may be `local-4080` using `gpt-oss:20b`,
provided those are your actual configuration values. Install the model and check
reachability yourself. Register the authorized repository on the central host and
optionally refresh its Python index. Observe the platform restrictions above first.

From a supported native Windows or Linux central host, the optional script is an
actual external SDK MCP client which launches the server, discovers its surface, delegates to Repo Explorer,
polls with a 180-second deadline, prints the durable result, then reads its recorded
telemetry after the server exits:

```sh
python scripts/smoke_mcp.py --database-url sqlite:///agentforge.db --workers workers.local.toml --project-id '<registered UUID>' --worker-id '<configured Worker ID>' --task 'Inspect the repository entry point, use source tools, and cite paths and lines.'
```

Run with the installed venv interpreter. Success shows the explicitly selected
Worker, completed final answer, positive tool-call count for a source-tool request,
and telemetry with actual known/unknown coverage. A failed result is still durable;
inspect its safe reason. Missing telemetry is reported without inventing values.
The script also prints sanitized model tool-call names/results after shutdown.
Add `--verify-source <public relative UTF-8 source file>` for central read/search and
path-escape checks, plus a requirement for a successful model read/search call.
Follow the linked native Windows acceptance procedure for setup and actual tests.

To repeat with a remote Ollama Worker, add another entry to the **same central
Worker file**, with a reachable LAN/VPN endpoint, installed model, explicit tool
support and factual deployment label. Restart the MCP server to reload configuration,
verify it through `list_workers`, then run the same interaction with that Worker ID.
Project registration and files stay on the central host; no remote checkout is needed.
Allow the configured inference timeout and RuntimeLimits to cover your request.
The director decides whether a result warrants another Worker; the server never
substitutes one. Compatible remote/cloud Workers can execute through the compatible
adapter. Repository tool results and model context may leave the central host;
see [Provider protocol and data-egress guidance](providers.md).

## Councils and coding

See [Council operations](councils.md) for durable independent execution on 2–16
explicit Workers, ordered per-participant results, partial failures and cancellation.
MCP adds `delegate_council`, `get_council`, `cancel_council`. The dashboard navigation
adds Council history/detail with Task links and live refresh using the same bounded
TaskObserver. The external Director remains the judge; telemetry stays per Task.

Named connections support heterogeneous Ollama/OpenAI-compatible Workers
through the same generic tools. See [configuration and data-egress guidance](providers.md).


The optional `--coding` configuration adds `coder` to Agent discovery.
Normal `delegate_task` provisions an isolated worktree before inference. The
Task-addressed `get_coding_workspace` and `get_coding_diff` return bounded factual
coding evidence with private workspace paths omitted. Validation captures remain
untrusted text, not generally scrubbed host-path/secret content.
`cleanup_coding_workspace` explicitly removes
a terminal Task's matching workspace, discarding uncommitted changes and retaining
the branch. It is marked destructive and requires both Task/workspace IDs.
Disabled/missing/suspicious workspaces return safe errors. No shell, argv, root,
Git ref, commit, push, merge or lifecycle tool is available to Workers. See
[coding trust model and recovery](coding.md) before enabling trusted validators.

## Companion alongside Codex

Add `--companion` to the existing MCP launch to host the loopback web panel in
the same process/Application and TaskEngine. This exposes the full `/`, `/workers`,
`/projects`, `/tasks`, `/councils` and `/companion` HTTP surface and trusted
cancellation/workspace cleanup controls, on loopback IP binds only. There is no
authentication layer. Do not start a separate dashboard executor on that database.
Standalone `agentforge web` can be intentionally LAN-bound by the operator and
also serves Companion. See [Companion discovery, launch and limitations](companion.md).
