# AgentForge architecture

## Status and purpose

Phase 01 established the architectural contract and package skeleton. Phase 02
implements Provider contracts, validated Worker configuration and the Ollama HTTP
adapter, including health, generation and streaming. Phase 03 implements the
Project Registry, SQLAlchemy persistence and the initial Alembic migration.
Issue #14 adds a deterministic Python Project Index and compact repository map.
Phase 04 adds bounded, read-only repository tools. Phase 05 adds generic Agent
behavior and bounded in-process execution, starting with `repo_explorer`. Durable
Tasks, telemetry, application endpoints and dashboard behavior remain **planned**.

AgentForge is a generic agent execution/runtime platform. It supplies projects,
providers, workers, agents, tasks, tools, telemetry, an MCP interface and a web
monitoring UI. It is not an intelligent task router in v1.

## System boundary and director

An external strong model such as Codex/Astra/Sol/Claude is the director/orchestrator.
It chooses the project, agent and worker, supplies the request, judges whether the
result is sufficient, and decides whether another worker should be consulted.
AgentForge validates and executes explicit requests and returns results and
execution evidence. It must not silently select a different worker or judge which
model answer is best. Validation rejects invalid or unauthorized requests; it is
not a routing decision.

```mermaid
flowchart LR
    D[External director] -->|Explicit execution request| M[Planned MCP adapter]
    M --> E[Planned application / Task Engine]
    E --> P[Registered project and scoped tools]
    E --> A[Agent behavior]
    A --> W[Explicitly selected worker]
    W --> V[Provider protocol adapter]
    V --> I[External inference service]
    E --> T[Task state and telemetry]
    T --> U[Planned monitoring UI]
    E -->|Result and evidence via MCP| D
```

Inference services, registered repositories and the director sit outside the core
runtime boundary. Repository content is external data, even when stored locally.
The dashboard observes execution; it does not become the director.

## Domain contract

| Concept | Meaning | Example |
| --- | --- | --- |
| Provider | Protocol/backend used to communicate with an inference service | Ollama, llama.cpp, OpenAI-compatible HTTP |
| Worker | Concrete configured inference target: provider, endpoint, model and machine context | Ollama at a configured local endpoint, `gpt-oss:20b`, RTX 4080 |
| Agent | Behavior layered on the selected worker | `repo_explorer`, `code_reviewer`, `test_triage`, `general` |
| Project | Registered external repository/workspace with context and access boundaries | A repository identified in the Project Registry |
| Task | Durable execution request binding project, agent, worker and request | Review a change using explicit registered IDs |
| Council | Future independent executions of the same problem on explicitly selected workers | Return every worker's result to the director |

Provider handles protocol details, Worker identifies where and which model runs,
and Agent defines behavior. Agents must not embed provider endpoints or choose
workers autonomously. Multiple workers may share a provider; a provider is not a
singleton model or machine. Machine context describes the target deployment, not
a hardware scheduling subsystem.

The eventual first deployment is RTX 4080 → Ollama → `gpt-oss:20b`. It is one worker
configuration, not a core assumption. Later configurations may use Ryzen AI Max+
395 with a large local model, other local inference servers, free or paid cloud
models, or arbitrary OpenAI-compatible endpoints.

## Components and repository layout

| Package under `src/agentforge/` | Responsibility |
| --- | --- |
| `core/` | Provider/Worker inference domain contracts; future application coordination and Task Engine |
| `projects/` | Project Registry: stable project identity, repository/workspace location, context and permitted operations |
| `index/` | Deterministic Python structure, explicit refresh, graph queries and bounded textual maps |
| `providers/` | Ollama inference adapter; translate calls and failures for the backend |
| `workers/` | Validated configuration loading for concrete inference targets |
| `agents/` | Agent behavior, bounded in-process execution and typed Project-bound tool wrappers |
| `tools/` | Repository operations constrained by project roots and permissions |
| `telemetry/` | Execution events, timing, errors and available usage observations |
| `db/` | SQLAlchemy persistence and Alembic migrations, using SQLite initially |
| `api/` | FastAPI transport adapter to application operations |
| `mcp/` | MCP Python SDK adapter exposing explicit operations to the director |
| `web/` | Jinja2 templates and HTMX monitoring interactions |

`tests/` holds pytest tests; `docs/` holds durable architecture documentation.
Packages other than `core/`, `providers/`, `workers/`, `projects/`, `index/`,
`tools/`, `agents/` and `db/` remain placeholders. Alembic configuration and revision
scripts live in `alembic.ini` and `migrations/` at the repository root.

### Provider and Worker foundation (Phase 02)

`core.inference.Provider` is a single structural Python protocol:

- `health(worker) -> WorkerHealth` is async and observes backend connectivity and
  configured model availability separately. It is a snapshot, not persisted state
  or a guarantee that the next generation succeeds.
- `generate(worker, request) -> GenerationResult` is async.
- `stream(worker, request)` returns an async context manager containing an async
  iterator of `GenerationChunk` values. Content is a delta, and a terminal chunk
  has `done=True` and any available final metadata.

All operations require an explicitly supplied Worker. There is no provider
registry, worker selection, routing or fallback. Future callers can accept the
protocol without importing the concrete Ollama adapter.

`core.worker.Worker` is a validated configuration model containing ID, provider
name, model, HTTP(S) endpoint, optional context window and deployment label,
configured streaming/tool-use capabilities, timeout and provider options. Unknown
tool-use support is `None`; streaming defaults to disabled until configured.
Phase 05 uses the configured tool capability: explicit `False` rejects a tool-using
Agent; `None` permits an attempt without claiming observed support.
Endpoints cannot embed credentials, queries or fragments. `WorkerHealth.available`
requires both backend connectivity and observed model availability.

`GenerationRequest` contains conversational messages, an optional prepended system
instruction, optional temperature, provider options and optional timeout override.
Phase 05 adds normalized structured tool calling, described below. Multimodal
input and reasoning channels remain unsupported. Results contain content, model
identity, optional finish reason, normalized token usage and optional
provider-specific usage/timing dictionaries for backward compatibility. Raw Ollama
token counts retain their API names, and duration values retain their API names and nanosecond units;
missing observations remain absent rather than estimated.

`providers.ollama.OllamaProvider` uses httpx with `/api/chat` for generation and
newline-delimited JSON streaming, and `/api/tags` for health/model presence checks.
Each operation owns and closes its HTTP client; streaming also owns the response
and iterator. Incomplete or malformed responses raise `InvalidProviderResponse`;
connectivity failures raise `BackendUnavailable`, timeouts raise `ProviderTimeout`,
and HTTP/model rejections raise `ProviderRejected` with an optional HTTP status.
Error messages omit backend bodies, endpoint secrets and input payloads. Health
reports safe error codes instead of raising backend failures; invalid adapter/Worker
pairings are rejected before HTTP work.

Callers must consume streams inside `async with`. Exiting the block closes the
HTTP request on completion, early break, caller error or cancellation. Cancelling
the executing asyncio task propagates `CancelledError` and closes local resources.
Ollama offers no task-ID cancellation primitive here: closing the request does not
guarantee that remote generation has stopped. No Task Engine is implemented.

The Worker timeout defaults to 120 seconds; request overrides apply to generation
and streaming. It is a total wall-clock execution budget as well as an HTTP I/O
timeout, with connection attempts capped at 10 seconds. For streaming, this budget
includes caller processing within the context block. Health is bounded by the
smaller of five seconds and the Worker timeout.

`workers.config.load_workers(path)` reads one explicitly supplied TOML file into
`WorkersConfig`, validating each Worker and rejecting duplicate IDs/unknown fields.
Multiple Workers can share one provider implementation. There is no automatic
config discovery, environment loader, plugin framework or application wiring.
`config/workers.example.toml` defines the illustrative `local-4080` target; it is
not loaded by default. Worker options are merged with request options, then a known
Worker context window sets Ollama `num_ctx`, and explicit request temperature takes
precedence. The model and endpoint always come from the supplied Worker. Secrets
belong in local secret configuration; authentication integration is deferred.

Tests use httpx mock transports and fake byte streams. A suite-wide guard rejects
live network connections and DNS resolution, so no GPU, Ollama or Internet is
needed to run them.

### Project Registry and persistence (Phase 03)

`projects.models.Project` is immutable configuration: a generated UUID, a
human-readable name, a canonical absolute local root and a UTC creation timestamp.
Names may repeat; canonical roots may not. Identity does not depend on a mutable
name or path. A normal directory is a valid project; Git is optional. Core contains
no project-specific assumptions.

`projects.service.ProjectRegistry` exposes synchronous, transport-independent
`register_project(name, root_path)`, `list_projects()`, `get_project(id)`,
`remove_project(id)` and `inspect_project(id)`. IDs accept UUIDs or UUID strings.
Removal deletes registration and its cached index, never repository files.
Configuration remains retrievable/removable when its directory is unavailable;
inspection validates that the root still exists.
Important failures have explicit `ProjectError` subclasses, including invalid
paths/names, duplicate roots, missing IDs, unsafe candidates and storage failures.
SQLAlchemy errors and database statements are not exposed as service errors.

Relative registration paths use the registry's explicit `base_directory`, or the
working directory captured at construction. Registration follows symlinks and
requires an existing directory, so aliases of the same directory collide. The
stored canonical root is the security boundary. `resolve_path(id, candidate)`
and `projects.paths.resolve_project_path(root, candidate)` resolve existing
candidates and check `Path.is_relative_to` against the resolved root. Absolute
paths, `..`, common-prefix siblings and symlinks receive the same containment
check. A stored root replaced by a symlink to another location is rejected.
Validation alone is a point-in-time check. Phase 04 adds `open_root(id)` for
descriptor-anchored access and persists new registrations' directory device/inode
identity. Migration `0003_project_root_identity` leaves these fields null for
legacy registrations: live Registry inspection, Index refresh/status scans and
repository tools fail closed until those projects are removed and re-registered.
Configuration retrieval/removal and cached Index queries remain available.
Migration never observes or authorizes a replacement directory. Creation semantics
for nonexistent paths remain unimplemented.

`inspect_project` returns `ProjectInspection(project, git)`. `GitMetadata` is a
fresh, nonpersisted observation with an observation timestamp, discovery status,
repository/worktree root, optional branch and optional HEAD commit. Status is
`repository`, `not_repository` or `unavailable`; unavailable discovery does not
assert that the directory is non-Git. Detached HEAD has no branch; an unborn
repository has no commit. Git failures/timeouts are best effort and do not prevent
registration. `inspect_project` first opens the identity-checked registered root
through `open_root`; boundary failures propagate Project errors, not best-effort
Git metadata. Inspection uses bounded, read-only `rev-parse`/`symbolic-ref` calls
with the verified descriptor inherited as Linux `/proc/self/fd` cwd, a sanitized
Git environment, disabled optional locks and no network or repository mutation.
Without descriptor-backed Git cwd, Git is observationally `unavailable`; no
pathname fallback may inspect a replacement. Separate reads are not an atomic
snapshot of a concurrently changing repository. An observed Git root may be an
ancestor of a registered subdirectory; it never expands the approved boundary.

`db.database` owns explicit SQLAlchemy 2 engine/session construction;
`db.models.ProjectRecord` owns the `projects` table. `db.projects.ProjectRepository`
maps records to domain values and owns short-lived sessions/transactions, a unique
canonical-root constraint and error translation. The service currently uses this
small concrete storage adapter; domain models, path helpers and Git inspection
have no SQLAlchemy dependency. No generic repository framework is introduced.
SQLite is the initial backend; UUID and timezone-aware timestamp columns use
SQLAlchemy types. SQLite timestamps are interpreted as UTC when read.

Schema changes are explicit Alembic operations, never registration/startup side
effects. From the repository root in the development venv:

```sh
alembic upgrade head
alembic revision --autogenerate -m "Describe the schema change"
alembic check
```

`alembic.ini` defaults to the ignored local `agentforge.db`; set `sqlalchemy.url`
in a local Alembic configuration for another database. Pass the same URL to
`create_database_engine`, then wire `create_session_factory(engine)` →
`ProjectRepository` → `ProjectRegistry`. Dispose the engine when its owner shuts
down. The initial revision creates only project configuration. Tests migrate
temporary SQLite databases and verify upgrade/downgrade and database reopening.

Project-specific configuration stays with registry entries, separate from generic
runtime behavior. Architecture summaries remain deferred.

### Deterministic Project Index (Issue #14)

`index.service.ProjectIndex` consumes the existing `ProjectRegistry` and
`db.index.IndexRepository`. Wire both repositories with a session factory for the
same migrated database. The service is synchronous and transport-independent:
`refresh_index(id)`, `get_index_status(id)`, `find_symbol(id, query)`,
`get_symbol(id, symbol_id)`, `get_dependencies`, `get_dependents`,
`get_related_symbols`, `get_relationships` and
`render_project_map(id, focus=None, max_tokens=3000)`. Index operations themselves
involve no HTTP/MCP adapter or Worker; the Phase 05 Agent wrappers consume these
public queries for structural navigation.

The boundary is Python bytes → built-in AST → small immutable extraction records
→ SQLite → queries/maps. `index.python_parser` owns definitions, line locations,
lexical containment and import/call facts; it never executes source. Its
`parse_python(relative_path, bytes) -> ParsedFile` boundary allows another parser
later without a plugin framework or storage redesign. `index.scanner` uses shared
`projects.filesystem` descriptors and `projects.exclusions` for safe reads/traversal.
`db.index` owns transaction-scoped replacement and link
resolution; `index.render` owns deterministic relevance and bounded rendering.
No LLM, embeddings, vector database or graph library builds structural facts.

Alembic revision `0002_project_index` follows the shipped `0001_projects` revision.
Four small tables store project snapshot metadata, eligible files, symbols and
relationships. Files carry observed and last successfully parsed SHA-256 hashes;
raw source/bodies are not persisted. Module symbols act as containment roots and
import targets; only unambiguous, unshadowed module-level definitions are eligible
definition import targets. Definition IDs hash the relative path, kind, lexical
qualified name and start line, so duplicate and nested definitions stay distinct.
IDs can change when definitions move. IDs are scoped to a Project, with no
cross-project semantic identity. Module names follow paths relative to the
registered root, with package `__init__.py` mapped to its directory. A root
initializer uses the display namespace `__root__`, without assuming an absolute
package name; its relative imports of indexed child modules can still link.
The index does not infer Python installation layouts, `sys.path` or `src/` roots.

Refresh explicitly scans `.py` files one at a time, compares content hashes and
reuses unchanged extraction records. New/changed files replace their structure;
deleted files are removed. Import links are recomputed from cached facts against
the complete current symbol set, including unchanged importers. A fresh registry
Git observation records HEAD when available, but never determines freshness:
dirty working trees and non-Git projects use the same hash checks.
`get_index_status` performs an explicit live hash scan and reports changed paths,
counts, snapshot time, observed HEAD and parse failures. Other queries read cached
structure; callers check status or request refresh when they need current data.

One database transaction covers the complete refresh. Filesystem, unexpected
parser or storage failures roll back all changes. Invalid Python syntax/encoding
is a per-file failure: its observed hash and a safe diagnostic are stored, while
its previous valid symbols/relationships remain available, flagged `stale` and
marked in maps. A newly invalid file has no structural records. Unchanged invalid
files retain their diagnostic without reparsing; edits retry parsing. Status keeps
reporting these failures, so a partial index is never presented as fully current.
SQLite connections enable foreign keys and transaction control covering reads;
cascades clean file-owned facts and all index tables when the registry removes a
Project. Resolved target IDs are checked and rebuilt transactionally rather than
defining a generic graph ORM.

The Registry's `open_root(project_id)` is authoritative for both Index filesystem
operations and repository tools: it validates the canonical root and its persisted
device/inode identity. `ProjectIndex._scan` keeps that context open while its scanner
consumes the verified root descriptor, never reopening a root pathname. Refresh
consumes the scan, including root exit validation, inside the database transaction
so replacement during parsing rolls back changes. `inspect_project` checks the
same identity before refresh's Git observation. Legacy NULL identity rows require
re-registration for live refresh/status scans; cached symbols, relationships and
maps need no live root authorization and remain queryable.
Traversal skips **all** symlinks (including internal aliases), special
files and fixed cache/build/vendor/IDE/secret directories, including `.git`, virtual
environments, `site-packages`, `node_modules`, `dist`, `build`, `vendor`,
`third_party` and `.secrets`; no configurable ignore engine is added. POSIX
no-follow descriptors anchor root ancestors, directory traversal and regular-file
reads, closing the validation/I/O symlink race. Unsupported descriptor capabilities
fail closed.
Root/directory replacement and changes during a file read abort refresh. The
repository is strictly read-only, and index operations never access the network.
As with Git inspection, a changing filesystem is not an atomic source snapshot;
explicit hash checks/refresh detect subsequent edits.

Relationships retain their kinds and direction: `contains`, `imports`, `calls`.
Dependencies are outgoing resolved edges, dependents incoming resolved edges, and
related symbols return their union as typed edges. `get_relationships` also exposes
unresolved textual imports/simple calls with a null target. Imports link only
unambiguous root-relative indexed modules/definitions; external imports, wildcards,
unknown exports and ambiguous names are not guessed. Call links describe syntactic
lexical targets, not guaranteed runtime dispatch: unique plain local definitions
and straightforward `self.method()` within a plain class without bases, metaclass,
class decorators or custom attribute lookup. Parameters, assignments,
imports, duplicate/conditional/decorated definitions and explicit receiver/method
rebinding block uncertain links. Arbitrary `obj.method()`, inherited dispatch,
imported-alias calls, lambdas/comprehensions, dynamic exports, monkeypatching and
full type inference are deliberately unsupported. No general reference analysis
or graph path operation is implemented. `find_symbol` returns all case-insensitive
exact simple/qualified matches, falling back to qualified-name substrings; it never
selects one ambiguous definition silently.

Maps contain paths, classes/functions/methods and start lines, without bodies or
AI summaries. Focus ranks exact names, qualified names, substrings/path matches
and direct import/call neighbors. General maps use relationship degree, top-level
definition counts and shallow paths before stable lexical tie-breaks. Rendering
uses whole structural lines, with necessary parent context, and a strict UTF-8
byte cap of `3 * max_tokens`; `ceil(bytes / 3)` is the documented approximate token
estimator, not a model-tokenizer guarantee. Zero or too-small budgets return an
empty map; negative/noninteger budgets are rejected. Future repository tools and
local agents may request exact source regions from these paths/line spans. Future
architecture summaries may consume these deterministic facts, but summaries,
visualizations, routing and execution remain separate, unimplemented capabilities.

### Tasks and repository tools

The future Task Engine will validate explicit bindings, persist execution requests
and lifecycle state, coordinate execution, and make outcomes retrievable. Detailed
states, cancellation, retries and concurrency policy belong to their implementation
issues. Infrastructure errors must be reported to the director; automatic model
fallback would violate explicit worker selection.

`tools.service.RepositoryTools(ProjectRegistry)` supplies synchronous,
transport-independent `list_files`, `read_file`, `search_code`, `git_grep`,
`git_status` and `git_diff`. Every operation requires a registered Project UUID;
there is no arbitrary root/path API, generic command runner, shell, write tool,
MCP wrapper or execution loop inside the service. Phase 05 wraps these methods
without duplicating their implementations. Tools do not require a refreshed index.
The Index supplies symbols, relationships and maps; tools supply exact source and Git state.

Filesystem authorization remains the registry's canonical Project root, using
`open_root(id)` and shared no-follow descriptors for root ancestors, traversal
and regular-file reads. Paths must be project-relative; absolute paths, `..`,
backslashes, drive/pathspec syntax and control characters are rejected. Tools
reject all symlinks (including internal aliases), directories as file reads and
special files. File changes during reads and directory/root replacement fail
closed, including early traversal termination at a result limit. Device/inode
identity catches ordinary root replacement across service/database reopening.
Containment uses path components, never string-prefix comparisons. POSIX
no-follow/descriptor capabilities are required; unsupported systems fail closed.
Privileged mount manipulation and inode reuse are beyond this filesystem boundary.

Automatic listing/search reuse the Index's fixed generated/cache/vendor directory
exclusions. There is no configurable ignore engine. They additionally omit a small,
case-insensitive sensitive-path policy: `.env`, all `.env.*` (including examples),
`.secrets`, `.ssh`, `.aws`, `.gnupg`, `.git`/`.hg`/`.svn`, `.netrc`, `.npmrc`,
`.pypirc`, `credentials.json`, common `id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519`
key names, and `.pem`/`.key`/`.p12`/`.pfx` suffixes, anywhere under the root.
Explicit reads may inspect otherwise excluded generated files, but sensitive
paths are always denied by every tool, without content in errors. This is
conservative defense-in-depth, not secret classification or DLP.

File reads accept optional inclusive 1-based start/end lines and return requested
and returned ranges, text and a truncation flag. Decoding is strict UTF-8 with
control-character rejection (except tab/CR/LF); no encoding guessing, tokenizer,
base64, media extraction or execution occurs. A bounded prefix is inspected,
so this is not a claim that an unseen tail is textual. Listings and searches have
stable lexical path/line ordering. `search_code` is literal, optionally casefolded,
and supports directory scope and a `fnmatchcase` glob over relative paths;
non-text files are skipped and counted. Snippet truncation is explicit on each
match; result truncation means the requested scan/output could not be completed.

| Budget | Default | Hard upper bound |
| --- | --- | --- |
| Returned UTF-8 bytes (all potentially large tools) | 64 KiB | 1 MiB |
| File listing / Git status entries | 1,000 | 10,000 |
| Returned file lines | 1,000 | 10,000 |
| Search / Git grep matches | 100 | 1,000 |
| Matching-line snippet bytes | 500 | 2,000 |
| Inspected source/index blob prefix per file | 2 MiB | fixed 2 MiB |
| Files / bytes inspected by filesystem search | 100,000 / 64 MiB | fixed |
| Changed paths considered by a diff | 100 | fixed |
| Lines compared per file in an unstaged diff | 10,000 | fixed |
| Git metadata capture / stderr capture | 64 KiB / 8 KiB | fixed |
| Git subprocess timeout | 2 seconds | fixed per subprocess |

Entry/match byte budgets account for paths as well as content. File range bounds
are limited to 1,000,000. Truncation is returned whenever a result, scan or diff
limit prevents completeness; incomplete oversized diff inputs are skipped rather
than treated as complete files. UTF-8 truncation never emits a split character.
Filesystem scans process one file at a time; directory names are sorted in memory.
Separate filesystem and Git observations are not an atomic snapshot.

Git tools require a Git worktree and Linux `/proc/self/fd` to pin subprocess cwd.
Absence, unavailable Git, timeout and backend failure are separate domain errors.
A private backend uses fixed explicit argv, `shell=False`, bounded concurrently
drained stdout/stderr, process-group termination and sanitized shared Git
configuration. Inherited `GIT_*` redirection is removed; global/system config,
optional locks, network transports, lazy fetch, pagers, fsmonitor, hooks, external
diff/textconv and submodule recursion are disabled. Configured clean/smudge/process
filters are discovered by name and overridden before worktree status. Git metadata
must remain stable during an observation; these calls are not an OS process sandbox
against another process concurrently rewriting Git configuration.

`git_grep` deliberately searches regular-file **index contents** with `--cached`,
not untracked or unstaged text, to avoid worktree symlink races. It accepts literal
queries and safe relative paths only, with no caller Git options/pathspec magic.
`git_status` parses NUL-delimited porcelain v1, disables rename expansion, and
returns branch/detached state plus staged, unstaged, untracked and conflict facts.
`git_diff(staged=True)` uses bounded Git patches with rename expansion disabled;
unstaged diffs compare validated indexed blobs against descriptor-safe working
files with stdlib `difflib`. Binary changes get a textual unsupported marker,
metadata-only changes a marker, and conflict/oversized input omission marks the
result incomplete. Symlink reads fail closed; submodule content is omitted.
There is no historical revision API, network access or repository mutation.

For registered `/repo/allowed` inside Git root `/repo`, every Git request has a
literal project-relative scope. Captured root-relative names are converted by
component containment, filtered by exclusion/sensitive policy and returned as
Project-relative paths. Grep/diff receive only eligible scoped paths; rename
expansion is disabled so a move across the boundary cannot reveal sibling content.
Git discovery never authorizes `/repo/secret`. Synthetic tests cover all three
Git tools on this subdirectory boundary and verify byte-for-byte read-only behavior.
Public errors reuse Project identity/path failures and add invalid argument,
missing file, unsupported text, sensitive path, I/O, non-Git, unavailable Git,
timeout and safe backend failure types; OS/SQL/Git stderr details are not exposed.

### Agent Runtime and Repo Explorer (Phase 05)

`agents.models.Agent` defines behavior (`id`, name, description, system prompt,
allowed tool names and default `RuntimeLimits`). It contains no endpoint, model,
Provider or Worker selection policy. Agent definitions and Workers are supplied
separately to `agents.runtime.AgentRuntime`, along with explicit Provider and tool
mappings and the existing Project Registry. There is no plugin loader. The caller
must supply `project_id`, `agent_id`, `worker_id` and `task` to `run()`; no Worker
argument has a default. The runtime looks up exactly that configured Worker and
its Provider. It never probes health for selection, routes, scores, retries on a
different model, or falls back when inference fails.

Execution is async and in-process, without durable Tasks. It uses non-streaming
`Provider.generate()`: system prompt + original task → assistant text and/or tool
calls → sequential validated tool results → next model turn → final answer.
Multiple tool calls in a turn retain their order and correlation IDs. Entire-turn
permission, ID uniqueness and tool-count checks happen before executing any tool.
An assistant turn with text and tools continues the loop; nonblank text without
tools completes it; an empty response terminates as `invalid_response`.
Streaming remains available on Providers; live Agent streaming and scheduling are
deferred until lifecycle adapters need them.

The inference contract adds `ToolDefinition(name, description, parameters)` with
machine-readable JSON Schema, `ToolCall(id, name, arguments)` with JSON object
arguments, assistant `Message.tool_calls`, and tool-role Messages with
`tool_call_id` and `tool_name`. `GenerationRequest.tools` and
`GenerationResult.tool_calls` use these normalized types. `TokenUsage` contains
optional observed input/output counts. The runtime retains one optional usage
observation per successful model turn, rather than fabricating totals when some
counts are missing. Existing raw Provider usage/timing fields remain compatible.

Ollama translates definitions into native `type: function` chat tools and parses
native `message.tool_calls[].function` objects. It never parses textual pseudo-tool
commands. Native Ollama generally omits call IDs and correlates tool results by
`tool_name`; the adapter creates unique local IDs when absent and preserves any
supplied IDs in normalized results. It sends assistant function objects and native
tool-role messages with `tool_name`. IDs remain intact inside the generic runtime;
Ollama's native wire correlation is isolated in the adapter, including sequential
results for repeated calls to the same tool. Model-specific reasoning fields are
ignored, not treated as final answers or commands. Malformed function objects are
safe Provider failures. Tool-use quality depends on the configured model/Ollama
version; no `gpt-oss` workaround contaminates the runtime.

`agents.tools.repository_toolset()` explicitly maps twelve read-only tools:
`get_project_map`, `find_symbol`, `get_symbol`, `get_dependencies`,
`get_dependents`, `get_related_symbols`, `list_files`, `read_file`, `search_code`,
`git_grep`, `git_status`, `git_diff`. Each has its own strict Pydantic argument
model, disallows extra keys and validates safe syntax before service calls.
The Project UUID is bound by the execution request, never accepted in model tool
arguments. Only `Agent.allowed_tools` are advertised and authorized; unknown or
disallowed tools terminate as `tool_not_allowed`. No dynamic attribute lookup,
filesystem opening, shell, arbitrary Git, subprocess or network operation is
implemented in the wrappers. They reuse Project Index and RepositoryTools.

The Registry's identity-checked `open_root` is verified before model operations,
before/after authorized tool operations and before accepting a final answer.
RepositoryTools still owns descriptor-anchored filesystem/Git access. Cached
Index calls also require live root authorization in Agent execution. Index output
is filtered with RepositoryTools' sensitive-path policy so cached symbols cannot
bypass denied-file access; map rendering accepts an optional caller-side path
filter without changing existing unfiltered Index callers. Structural lookup
lists have explicit result counts and truncation metadata.

| Runtime limit | Default | Hard upper bound |
| --- | --- | --- |
| Model turns (`max_steps`) | 12 | 100 |
| Single overall deadline | 120 seconds | 600 seconds |
| Attempted authorized tool calls | 24 | 200 |
| Serialized individual tool result | 12,000 UTF-8 bytes | 65,536 bytes |
| Accumulated serialized tool results | 48,000 UTF-8 bytes | 524,288 bytes |
| Approximate conversation contribution | 24,000 tokens | 200,000 tokens |

The caller can supply validated per-execution limits; otherwise the Agent defaults
apply. Conversation capacity is also capped by a known Worker context window.
The deterministic approximation is `ceil(serialized UTF-8 bytes / 3)`, including
system prompt, original task, all assistant text, tool calls/arguments, tool
results, correlation fields, JSON framing and advertised schemas. It is a
conservative size heuristic, not a tokenizer guarantee. The runtime checks the
initial request and each appended contribution. It never silently discards or
summarizes evidence. Source bytes are additionally bounded by existing service
limits. Wrappers request at most half the remaining per-result/aggregate budget
for source output, reserving room for JSON escaping and metadata; the runtime then
checks the actual complete serialized result. Oversized JSON is rejected rather
than clipped. Less than 256 bytes remaining ends execution before another tool.
Repeated reads cannot grow conversation or accumulated output without bound.

Limit termination names are distinct: `max_steps`, `max_tool_calls`,
`tool_result_limit`, `tool_output_limit`, `context_limit`, `timeout` and
`provider_timeout`. One monotonic deadline includes the whole interaction;
inference receives `min(Worker timeout, remaining deadline)` and is additionally
wrapped in `asyncio.timeout`. The synchronous Index/RepositoryTools calls cannot
be interrupted mid-call: cancellation and elapsed time are checked at boundaries,
and Git retains its existing two-second per-subprocess timeout. A long synchronous
scan may therefore return after the Agent deadline, at which point its result is
rejected. No background tool work or executor pool is introduced.

`CancellationToken` wraps a thread-safe event. It is checked before/after model
calls, before/after tool calls and between iterations, returning structured
`cancelled` state. Token cancellation during an awaited inference operation is
observed when that operation returns or times out; callers needing immediate
interruption may cancel the asyncio execution task. `CancelledError` propagates
and Provider resource cleanup remains intact. Phase 06 can connect Task
cancellation to this token/task boundary without replacing the runtime.

Recoverable errors are bounded JSON objects with fixed safe codes: invalid
arguments or safe-path syntax, denied sensitive paths, missing files/symbols,
unsupported text/binary, repository I/O, non-Git, unavailable Git, Git timeout and
Git backend failure. The model may recover with another permitted query. Unsafe
identity/descriptor failures raised during service access are terminal
`security_error`; storage/unexpected service faults are terminal `tool_error`.
Provider faults are terminal with no fallback. Errors never serialize exception
messages, traceback, SQL, backend bodies or Git stderr. Invalid configuration,
cancellation and global limits also terminate. The registered root remains
fail-closed even when only cached navigation is requested.

`ExecutionResult` returns final answer (only on completion), Agent/Worker/Project
IDs, state, termination reason, step/attempted-call counts, accumulated output
bytes, observed usage and an in-memory ordered trace. The trace records model and
tool boundaries, call IDs, authorized names, validated argument shape with free
text redacted, success/fixed failure codes, result byte sizes, elapsed durations
and termination. It deliberately omits source bodies, assistant prose, raw invalid
arguments and backend diagnostics. No result/trace is persisted or emitted to a
metrics subsystem. This is execution evidence, not Phase 07 telemetry.

`repo_explorer` requires inspecting evidence before repository claims, treats the
Index as cached navigation rather than source truth, asks for exact source/tests
with focused queries, distinguishes evidence from hypothesis/inference and
acknowledges insufficient evidence. It cannot claim to inspect unseen code or
invent paths/symbols/tests. Answers should cite repository paths and relevant line
ranges. Source/model output is untrusted and cannot expand permissions. There
are no write/shell/test-execution tools, external network queries, delegated
Agents or Worker-selection capabilities. The instructions do not embed this
architecture document.

Runtime tests use scripted Providers, injected clocks and synthetic projects.
Ollama protocol tests use httpx mock transports. The existing suite-wide socket
and DNS guards cover these tests: no automated test runs Ollama, uses a GPU or
accesses live networking. See [the manual smoke instructions](manual-repo-explorer.md)
for the opt-in real Ollama path. Phase 06 owns durable execution, Task history,
scheduling and lifecycle integration; Phase 07 owns telemetry; Phase 08 owns MCP.

### Telemetry, MCP and dashboard

Telemetry will record task identity, selected project/agent/worker, execution
events, timings and failures, with usage data only where providers expose it.
Do not assume all providers expose identical token or cost information. Retention,
storage schema and event delivery are future decisions.

MCP is the director-facing boundary. Future tools will accept explicit identifiers
and requests and return task results/status and relevant evidence. The MCP adapter
must reuse application behavior rather than implement its own task runtime.
No MCP tool names, server transport or authentication mechanism is fixed here.

The dashboard will monitor projects, workers, tasks and telemetry using Jinja2
and HTMX. It consumes shared application operations rather than accessing inference
services directly. HTMX is a future browser asset, not a Python dependency;
asset delivery is deferred. No React/Node stack or dashboard behavior is included.

### Council

Council is a future application operation: the director explicitly selects several
workers, each executes the same problem independently, and all results are returned
with worker identity and execution evidence. AgentForge does not rank answers,
seek consensus or choose a winner. No Council implementation is part of Phase 01.

## Dependency direction and extension points

The intended source dependency direction is inward toward project-agnostic domain
and application contracts:

```mermaid
flowchart TD
    API[API / MCP / web adapters] --> APP[Application coordination]
    APP --> DOMAIN[Core domain contracts]
    INFRA[Providers / DB / telemetry adapters] --> DOMAIN
    FEATURES[Agents / workers / projects / tools] --> DOMAIN
```

Core domain contracts must not import API, MCP, web or concrete inference adapters.
Application coordination uses capabilities defined by actual use cases; outer
adapters supply implementations when wiring is introduced. Runtime calls can go
outward to an adapter without reversing source dependencies. Avoid speculative
interfaces before a feature needs them, and avoid circular imports between areas.

Extension points are additional provider adapters, worker configurations, agent
behaviors and permission-scoped tools. New projects are registry entries, not core
code changes. Design these seams incrementally with tests when their behavior is
known; a plugin loader or generalized dependency injection framework is not needed
for the bootstrap.

The chosen stack is Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic,
SQLite initially, MCP Python SDK, httpx, Jinja2, HTMX, pytest and ruff. Dependencies
are declared now, but adopting a library does not imply its runtime is implemented.

## Security principles for future implementation

- Enforce project-root containment, including resolved paths and symlinks; do not
  allow repository content or model output to expand permissions.
- Separate untrusted instructions/data from director authorization. Agent behavior
  and tool policy must not treat prompt injection as permission to act.
- Validate project, agent and worker identifiers and tool permissions before work.
  Restrict configured inference endpoints to administrator-approved destinations.
- Keep credentials in local environment/secret configuration, never repository
  source or project content. Redact secrets from errors, task output and telemetry.
- Apply least privilege to files, commands and network access. Add isolation,
  timeouts and resource limits alongside the features that need them.
- Protect API/MCP/dashboard access before remote exposure. Transport and
  authentication decisions are deferred, not implicitly solved by localhost.
- Treat local databases and runtime logs as private runtime state. Define retention
  and output limits when persistence and telemetry are implemented.

Some principles guide future components; registered project and repository-tool
boundaries are implemented as described above.

## Future execution flow

1. The director discovers registered projects, agents and workers through the
   future interface and chooses explicit identifiers.
2. It submits a request binding project, agent and worker, with permitted tool scope.
3. Application coordination validates the bindings and authorization and persists
   a durable task before execution.
4. The agent executes its behavior using the selected worker's provider and approved
   project tools; state and telemetry capture execution progress and errors.
5. AgentForge returns or exposes the task result, selected identities and evidence.
   The dashboard monitors the same task state through shared application behavior.
6. The director evaluates sufficiency and may explicitly request another execution
   or a future Council run. AgentForge does not make that decision itself.
