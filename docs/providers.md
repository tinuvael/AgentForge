# Providers, connections and Workers

A **Provider** implements an inference protocol/backend. A **Provider connection**
names a trusted operator-configured base URL and optional authentication settings.
A **Worker** supplies a stable ID, connection reference, model identifier, explicit
capabilities and deployment label. The external director selects Worker IDs.
AgentForge does not route, rank, automatically probe capabilities, retry inference or substitute
another Worker. A Council runs only its explicitly selected participants.

## Connections and compatibility

`ProviderConnection` keeps endpoints and authentication inside protocol adapters.
A small static factory creates one adapter per named connection; multiple Workers
can reference it. Inline Ollama Workers remain supported intentionally.
There is no dynamic discovery or Provider plugin loader.

Preferred TOML has `[[providers]]` connections and `[[workers]]` referencing their
IDs through `provider`. The loader resolves this into Worker `provider` (protocol
type) and `provider_connection` (connection ID). Runtime lookup uses the reference,
or the type for inline Workers. Durable Tasks/Councils/telemetry and discovery
continue using protocol type and configured model; no connection credentials or
endpoints are persisted or projected. The response model is only a transient
Provider observation and cannot rebind execution identity. Provider connection configuration is not persisted in database schema.

For inline Ollama, use `config/workers.example.toml`:

```toml
[[workers]]
id = "local-4080"
provider = "ollama"
endpoint = "http://localhost:11434"
model = "gpt-oss:20b"
supports_tools = true
supports_streaming = true
context_window = 32768

[workers.options]
temperature = 0.1
```

Named connections and inline Workers may coexist. Connection IDs and Worker IDs
must each be unique. When mixing inline Ollama Workers, reserve the connection
ID `ollama` for the inline binding. Unknown references/types, conflicting inline
endpoints, URL credentials/query/fragment, unsupported schemes, invalid settings
and unavailable required environment authentication fail safely at startup.
Programmatic injected Providers remain supported for generic callers.

## Configuration examples

Local compatible server without authentication:

```toml
[[providers]]
id = "local-compatible"
type = "openai_compatible"
base_url = "http://127.0.0.1:8000/v1"

[[workers]]
id = "local-model"
provider = "local-compatible"
model = "operator-configured-model"
supports_tools = true
supports_streaming = true
```

Authenticated endpoint and a LAN endpoint:

```toml
[[providers]]
id = "remote-compatible"
type = "openai_compatible"
base_url = "https://example-compatible-provider.invalid/v1"
api_key_env = "AGENTFORGE_REMOTE_API_KEY"
# Opt in only if this backend supports it:
# stream_usage = true

[[providers]]
id = "lan-compatible"
type = "openai_compatible"
base_url = "http://ai395.local:8000/v1"

[[workers]]
id = "remote-model"
provider = "remote-compatible"
model = "operator-configured-model"
supports_tools = true
supports_streaming = true
context_window = 16384

[[workers]]
id = "lan-model"
provider = "lan-compatible"
model = "operator-configured-model"
supports_tools = false
supports_streaming = false
```

Set `AGENTFORGE_REMOTE_API_KEY` in the server process environment using your
operator secret provisioning mechanism. TOML contains only the environment
variable name; plaintext `api_key` settings are rejected. The resolved token stays
in the adapter's private `SecretStr`, never on a Worker, in Task rows, telemetry,
MCP or HTML. Adapter errors contain fixed diagnostics, not backend bodies,
request headers or environment values. Reconfigure/restart to rotate the token.
Bearer credentials are sent to the configured origin only; use HTTPS for remote
authentication. HTTP sends context and credentials without transport encryption.

See [the mixed configuration example](../config/workers.providers.example.toml).
Multiple Workers can reference the same connection with different models.
Capabilities are explicit configuration: omitted context/tools stay unknown,
streaming defaults to disabled. Provider type does not establish tool, streaming
or reasoning support. Context window limits runtime context; it is not sent as
an OpenAI-compatible `num_ctx` parameter. Verify examples against your model.

## Explicit heterogeneous execution

Use the usual Application/MCP Task arguments, with no protocol-specific fields:

```python
app.delegate_task(
    project_id=project_id,
    agent_id="repo_explorer",
    worker_id="lan-compatible-model",
    task="Inspect repository entry points.",
)
app.delegate_council(
    project_id=project_id,
    agent_id="repo_explorer",
    worker_ids=("local-4080", "lan-compatible-model"),
    task="Inspect repository entry points.",
)
```

Participants execute independently through their own adapters. Provider failure
fails that participant safely, without replacing it or disturbing the other
participant. Council order and existing partial-failure semantics are preserved.
MCP `list_workers` and dashboard Workers show the same generic IDs, Provider type,
model, deployment, context/tools/streaming facts, without endpoints/auth/options.

## Ollama protocol and limits

Ollama uses `<base_url>/api/chat` for generation and newline-delimited streaming,
and `/api/tags` for explicit health/model checks. Model names without a tag also
match `:latest`. Runtime does not health-probe or replace a Worker before execution.
Worker/request options are Ollama generation options; context window maps to
`num_ctx`. Native tool names/arguments normalize into structured calls, with local
correlation IDs when Ollama omits IDs. Tool results use native `tool_name`.

The [operator CLI](operator-cli.md#workers) reuses TOML parsing/factory validation
for offline `worker config-check`/`worker list` and existing Provider health for
an explicit `worker check <id>`. Ollama checks model availability; compatible
Providers truthfully return `not_probed`. A separate operator-only diagnostics
service records optional health and explicitly requested synthetic inference
observations; see [Worker diagnostics](worker-diagnostics.md). Normal discovery
and Task execution do not probe.

Each operation owns its HTTP client and response. Bodies, including model lists,
are limited to 2 MiB; stream lines to 256 KiB and total stream bytes to 4 MiB,
including blank lines. Byte limits apply before JSON parsing. Identity encoding is
requested; compressed responses and redirects are rejected, and HTTP error bodies
are not read. Duplicate JSON keys, nonfinite numbers and malformed/deeply nested
JSON fail with fixed diagnostics. A terminal `done` record is required; a final
record without a newline is accepted. These are bounded small text/tool responses,
not arbitrary bulk generation. Limits apply to direct Provider callers too.

Reported token counts and nanosecond timings normalize into observed counts and
seconds. Missing fields remain unknown. Native private `thinking` is carried only
in ephemeral assistant history for protocol continuity. Direct streams must stay
inside `async with`; completion, early break, timeout and cancellation close them.

## Runtime completion and output limits

Both adapters preserve backend finish reasons in `GenerationResult`. Ollama's
native `done_reason="length"` and Chat Completions `finish_reason="length"` mean
the generation reached its output cap, not a complete answer. AgentRuntime fails
that turn/Task with safe reason `output_limit`, discards its answer and executes
none of its tool calls. It does not retry, continue or invent missing content.
The Director chooses any next request. This applies through TaskEngine to durable
Task state, independently of Provider type.

Ordinary `stop` answers still complete and valid tool-call turns still proceed.
Ollama responses with an absent `done_reason` remain supported, as do normalized
responses with an absent finish reason from programmatic Providers. The shipped
compatible adapter already requires a nonempty finish reason; omission is still
an invalid Provider response. This correction does not expand that wire subset.
See [Ollama completion reasons](https://github.com/ollama/ollama/blob/main/llm/server.go)
and [Chat Completions finish reasons](https://platform.openai.com/docs/api-reference/chat/object).

## OpenAI-compatible protocol subset and limits

Direct httpx implements text-only `POST <base_url>/chat/completions`. Include `/v1`
in configuration if the server requires it; the adapter does not append another
`/v1`. System/user/assistant/tool history, optional prepended system instruction,
function schemas, multiple assistant function calls, JSON-object arguments and
correlated tool results are supported. Provider-issued IDs are preserved exactly.
Calls missing IDs, with duplicate IDs, invalid JSON/non-object arguments or
unsupported types fail closed. Generic runtime permission checks still precede
all tool I/O. Legacy `function_call` and multi-choice completions are unsupported.

Worker/request generation options are restricted to `temperature`, `top_p`,
`max_tokens`, `max_completion_tokens`, `seed`, `stop`, `presence_penalty` and
`frequency_penalty`. Request options override Worker options; explicit request
temperature takes precedence. Options cannot override model, messages, URL,
headers, authentication or number of choices. The backend may reject options it
does not implement. Finish reasons are preserved without vendor interpretation.

Streaming uses a small byte-bounded SSE parser, not EventSource or an SSE package.
It accepts LF/CRLF `data:` frames, multi-line data, comments/keepalives and framed
`data: [DONE]`. Text/private reasoning are deltas; indexed tool-call ID/name/JSON
fragments are assembled, validated and emitted as complete calls in the terminal
chunk. Usage-only frames are supported. A finish reason followed by `[DONE]` is
required; a truncated/disconnected stream fails safely. The runtime continues
using non-streaming `generate()` for tool execution.

`stream_usage = true` requests `stream_options.include_usage`; it defaults off
because some compatible servers reject this extension. Reported usage is consumed
even without requesting it. SSE lines/events are limited to 256 KiB, total stream
bytes to 4 MiB, decoded non-streaming bodies to 2 MiB, and tool calls to 200.
Bounds include ignored SSE fields/comments. The adapter requests identity encoding
and rejects compressed responses before reading them. HTTP error bodies are not
read at all.
JSON/schema errors never expose the raw payload. HTTP 3xx/4xx map to
`ProviderRejected` with status, 5xx to `BackendUnavailable`, transport disconnects
to `BackendUnavailable`, timeout to `ProviderTimeout`, malformed/truncated bodies
to `InvalidProviderResponse`. Runtime/Task durable codes remain the existing
`provider_error`/`provider_timeout` categories.

Only HTTP/HTTPS base URLs from trusted configuration are accepted, without
userinfo/query/fragment. Redirects are disabled on every request, so bearer tokens
are never automatically forwarded to another origin. The adapter is not a proxy:
Tasks, model outputs and tools cannot choose the destination. Each request has
one attempt, a wall-clock budget and HTTP I/O timeout (Worker default 120 seconds,
request override supported), with connect timeout capped at 10 seconds.

Optional string `reasoning_content` or `reasoning` fields normalize into the
existing private reasoning channel. Absent reasoning stays absent. It is never
used as an answer, tool authorization, trace or durable/public output. The
compatible adapter does not serialize private reasoning into visible assistant
history or invent a vendor-specific reasoning continuation protocol. Ollama keeps
its native private `thinking` continuation behavior unchanged.

This is a bounded Chat Completions subset, not universal OpenAI compatibility.
No Responses API, multimodal messages, legacy function calls, discovery/model
listing, OAuth, embeddings, files, audio or image APIs are implemented. Compatible
Provider `health()` returns `not_probed` without network activity; no backend or
model availability claim is made. Model/tool compatibility must be configured by
the operator. Some models require private reasoning continuation formats that are
outside this subset. Adapt deployment/configuration accordingly.

## Telemetry and lifecycle

Only reported nonnegative integer `prompt_tokens`, `completion_tokens` and
`total_tokens` are normalized. Missing counts remain unavailable, including streams;
text length never estimates tokens. Per-Task total uses reported totals when all
turns report them, otherwise uses the sum of complete input/output counts.
Reported total-only observations can supply total without claiming input/output
coverage. Extra usage fields are ignored. No backend generation duration is
invented: generation/load/prompt durations and tokens/sec remain null for this
adapter. Runtime request and Task/queue timers retain their existing definitions.
Task telemetry TTFT remains null: ordinary runtime calls are non-streaming.
Explicit diagnostics can measure client first visible content delta through scoped
streaming; headers/empty frames/private reasoning do not qualify. This is distinct
from backend generation timing and exact token arrival.

The Application owns factory-created adapters; injected adapters retain caller
ownership. OpenAI-compatible clients are lazy and reused per connection across
Workers and concurrent requests. Each response is scoped to its operation; an
early streaming break, error, deadline or executing asyncio task cancellation
closes it. Direct callers must use `async with provider.stream(...)` and call
`await provider.aclose()` when finished. Closing a compatible Provider cancels and
awaits its active caller tasks, then closes its client. No background reader runs.
Application shutdown first cancels/awaits TaskEngine execution, then closes all
owned clients and the database. Startup failure closes resources; partial
composition cannot orphan clients because constructors do not open them.

Ordinary Task cancellation remains cooperative at runtime boundaries: an in-flight
`generate()` can finish or reach its timeout before the token is observed.
Application shutdown/asyncio cancellation closes active requests promptly. Closing
HTTP does not guarantee the remote backend stops inference; no remote cancellation
API is assumed. Ollama retains its scoped client-per-operation lifetime, avoiding
an unrelated rewrite of its native protocol or resource ownership.

## Repository data egress

A remote/LAN/cloud Worker receives whatever model context and repository tool
results AgentForge sends during that explicit execution. The Provider receives no
direct filesystem access, but repository content can leave the central machine
through HTTP context/tool results. Cloud Workers do **not** keep repository data
local. Select Workers explicitly with this trust boundary in mind. Registering a
Project does not send it to all Workers. There is no DLP policy engine.

## Adding a third Provider

Implement the generic `Provider` contracts (`health`, `generate`, scoped `stream`)
and normalize messages, tool correlation, private reasoning, factual usage/timing
and fixed safe errors inside the adapter. Keep connection/auth settings isolated.
Add the type to `ProviderConnection` validation and `PROVIDER_TYPES`, extending
connection settings only for a concrete need. Factory constructors must remain
resource-free/lazy; resource-owning implementations supply `aclose()`. Add offline
MockTransport tests and repeat the heterogeneous integration tests. Runtime,
TaskEngine, telemetry, Council, MCP and dashboard must need no Provider-specific
branches. No new schema is needed for connection secrets.

`GenerationRequest.max_output_tokens` is an optional normalized output budget.
Ollama maps it to `num_predict`; compatible generation maps to `max_tokens`, or
`max_completion_tokens` when that option is present, with one effective cap.
It overrides configured/request option budgets without altering model/connection.
Diagnostics omit Worker options and therefore use the portable `max_tokens`
variant. Providers may reject unsupported parameters; diagnostics do not retry.
Ollama health additionally exposes backend reachability separately from availability.
A received error/invalid body proves contact, network failure reports unreachable,
and timeout remains unknown. Compatible health makes no reachability claim.
