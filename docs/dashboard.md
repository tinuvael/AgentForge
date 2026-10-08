# Operator dashboard

Install Python 3.12+ and the package, configure Workers using the existing TOML
format, and migrate the explicitly selected database with
`agentforge db upgrade --database-url sqlite:///agentforge.db`
(see [MCP setup](mcp.md) for database configuration). Then run:

```sh
agentforge web --database-url sqlite:///agentforge.db --workers workers.local.toml
```

Open `http://127.0.0.1:8765`. The defaults bind **127.0.0.1**, port **8765**, with
one executor slot. `--concurrency 1..32` controls the existing TaskEngine;
`--host` and `--port` configure Uvicorn. Database URL and Worker file are required;
there is no automatic database migration, Project discovery, Worker health probe,
or model/endpoint selection. No Node/npm build or network asset fetch is needed.

The operator CLI and retained module entrypoint work on native Windows with the same Python dependencies.
No POSIX signal, `/proc`, Unix socket or native Windows integration service is
required by the dashboard. Repository execution still uses the shared native
Windows NTFS or POSIX security backend and its documented platform limitations.

Project administration stays in the [operator CLI](operator-cli.md). Stop the
executor before registration/removal, Index refresh or database upgrades.
No new dashboard configuration forms or destructive actions are introduced.

## Pages and operations

| Route | Behavior |
| --- | --- |
| `/` | Package version, queried database/TaskEngine status, Project/Worker/Agent counts, durable state counts, latest 10 submissions, first 20 telemetry Worker groups in identifier order |
| `/workers` | Paginated configured IDs, Provider/model, deployment label, context window, declared tools/streaming capabilities and separate persisted health/probe observations |
| `/projects` | Paginated Registry names/IDs/roots and cached Index checkpoint timestamp/observed HEAD; no source reads or recursive inspection |
| `/tasks` | Bounded history with state, Project UUID, Agent ID and Worker ID filters; deterministic descending creation time then Task ID |
| `/tasks/{task_id}` | Identity, state, historical Provider/model, lifecycle timestamps, request, final answer, safe termination category, telemetry and metadata timeline |
| `/tasks/{task_id}/fragment` | Same diagnostic snapshot as HTML, refreshed using HTMX |
| `/tasks/{task_id}/events` | SSE refresh/resync/terminal/shutdown hints with empty JSON payloads |
| `POST /tasks/{task_id}/cancel` | Confirmed, CSRF-protected cancellation through shared Application/TaskEngine |

Lists default to 25 rows, maximum 100, with offset 0..1,000,000. Parameters and
UUIDs are validated; malformed input renders a fixed safe error. Task list queries
join Project names and existing terminal runtime telemetry in one compact query;
they never load request/answer/execution JSON. The overview uses SQL aggregates and
bounded observations, without ranking, scoring, recommending or routing Workers.

Project roots follow the existing trusted local operator discovery contract. Live
Git status and branch are **not probed**. HEAD is explicitly the cached observation
at indexing, not current Git state, and the Index may be absent or stale. Missing
roots and unavailable Git do not break list rendering. In particular, Windows Git
inspection builds an authorized private source snapshot, so it is deliberately not
invoked to render this page. The dashboard never recomputes an Index.

Workers are inference targets, including single/multiple local targets, LAN/VPN
machines and cloud endpoints. Configured capabilities do not prove
availability. Endpoints and all Provider options are omitted, including credentials,
URL userinfo and private endpoint paths. No Workers are probed on page refresh.

## Composition and lifecycle

`web.app.create_app(application_factory)` owns one shared `Application`
in its ASGI lifespan. Construction, startup, routes and shutdown run on TaskEngine's
owning thread/event loop. MCP and web use the same Registry, Index, repository tools,
Worker configuration, Agent definitions, Provider/runtime, TaskRepository/TaskEngine
and TelemetryService composition. Read projections live in `application.dashboard`;
HTTP handlers perform validation, rendering and existing application calls.

**Run only one process per database.** The existing guard rejects a duplicate
TaskEngine owner within a process; it is not a distributed lease. Separate MCP and
web CLI processes must not execute against the same database simultaneously. A
combined transport deployment would need to inject one already-owned Application
and coordinate its lifecycle; no combined-process entry point ships.
Use one Uvicorn process, without reload/multiple workers. The dashboard has no task
submission/orchestration endpoint; directors can use the existing Python service
in-process, and historical/queued work is observed through the same database.

Startup applies interrupted-running recovery and queued work resumption.
Shutdown shields executor cleanup and database disposal. Open SSE streams have a
five-second Uvicorn graceful shutdown budget; disconnected/cancelled responses
release their subscriptions before lifespan cleanup. Queued Tasks survive shutdown;
active executor work fails as `executor_cancelled`, or cancels if a committed
explicit cancellation request already won. No remote inference kill is claimed.

## Live observation

Dashboard timelines and Director `watch_task` progress use the same safe
`TaskObserver`/`TimelineEvent` projection. Coding Tasks include actual workspace
provisioning and validation lifecycle metadata, without paths, commands or captures
in that timeline. Existing Task/Council SSE still sends empty reload hints; its
bounded queues, authoritative reconnect and HTML refresh behavior are unchanged.
Directors watch individual Council participant Tasks. See
[MCP Task progress](mcp.md#live-safe-task-progress) for the separate request-scoped
adapter; this does not introduce a combined MCP/web launcher.

A small optional `ExecutionObservations.on_trace` callback publishes the runtime's
already-recorded trace metadata to TaskEngine's in-process observer. Observer failure
cannot change the recorded trace or stop execution. Durable Task state and terminal
result remain authoritative, with no new migration, telemetry store or event broker.

Each executing Task retains the latest **100** explicitly projected metadata events.
Only active executor slots (at most 32) hold these buffers; they are discarded at
terminal persistence. Persisted terminal traces are projected with the same allowlist
and rendered at most 100 events. Truncation is visible. Each subscriber queue holds
at most **16** notifications, with at most **128** subscribers in the process. A
capacity rejection returns a safe 503 without registering a subscriber.

Publishing uses non-blocking queue operations. On queue overflow, pending hints are
discarded and a `resync` hint replaces them; terminal/shutdown hints take priority.
A dropped buffer prefix also triggers resync. A new/reconnecting browser always
reloads a snapshot; there is no Last-Event-ID replay guarantee. SSE sends only named
hints plus `{}`, never accumulated trace/tool payloads. A 15-second comment keepalive
supports idle queued/model requests without polling. Multiple tabs are independent;
disconnect, terminal delivery, pre-iteration disconnect and shutdown clean up queues.
There is no polling task per UI element and no browser dependency for execution.

A small plain JavaScript EventSource listener coalesces hints over 200 ms and uses
HTMX to reload the whole diagnostic fragment. Task history filtering/pagination and
cancellation also use HTMX. Links, forms and confirmation remain functional without
JavaScript; live refresh requires JavaScript/EventSource. Browser state does not
establish execution state. Synchronous repository tools can delay rendering until
the next event-loop yield; this preserves the current runtime's execution model.

## Cancellation and local security

Cancellation uses POST only. The operator must check a labeled confirmation box;
the server also requires `confirm=yes`. Queued cancellation prevents execution.
Running cancellation records a distinct request timestamp and signals the existing
cooperative token. Terminal Tasks remain terminal, including repeated POSTs.
Cancellation does not force-kill remote inference or claim to interrupt a tool
mid-call. HTMX receives the authoritative fragment; ordinary POST redirects to detail.

The cookie/form CSRF token has a random 256-bit nonce signed with a per-web-instance
HMAC key. The server validates signature and constant-time cookie/form equality,
confirmation and bounded form bodies. Cookies use HttpOnly, SameSite=Strict and
Secure when the request scheme is HTTPS. Cross-site Fetch Metadata and mismatched
Origin headers are rejected. Restart invalidates old tokens; reload before retrying.
There are no database-backed browser sessions or full identity/authentication layer.

Trusted Host validation defaults to explicit loopback hosts to resist DNS rebinding.
Security headers prohibit framing, inline/evaluated scripts, external asset loading
and MIME sniffing; HTML and diagnostics use no-store/no-referrer. HTMX evaluation
and script-tag processing are disabled; HTMX history snapshots are not stored in
browser localStorage. History restoration requests return a complete HTML page.
Jinja autoescaping and text-only request/
answer rendering treat model/Project strings as untrusted HTML. Safe error pages
and logs omit exception repr, SQL, tracebacks and backend response bodies. CLI
access logging is disabled; generic third-party diagnostics are filtered.

To bind intentionally to a LAN address:

```sh
agentforge web --database-url sqlite:///agentforge.db --workers workers.local.toml --host 192.168.50.2 --port 8765
```

The CLI warns that there is **no authentication layer**. Protect access with a
trusted network/VPN or authenticated reverse proxy. When binding `0.0.0.0`, or when
using a proxy DNS name, supply each actual browser host explicitly using repeatable
`--allowed-host dashboard.example.internal`; wildcard hosts are rejected. Configure
proxy scheme/host forwarding correctly for HTTPS cookies and Origin checks. Do not
expose this trusted control surface to an untrusted/public network. Loopback alone
is not the CSRF defense.

## Telemetry and privacy boundary

Observed telemetry values are displayed directly, with `—` for NULL. Zero is shown
only when observed. Terminal coverage is pending/recorded/unavailable; live timeline
metadata does not fabricate interim token accounting. Complete token totals and
observed partial sums are labeled separately, including covered-turn counts.
Throughput uses only the service's compatible backend generation-duration semantics,
never total Task runtime or request latency. TTFT stays unknown unless explicitly
observed; no latency-derived estimate is made. Worker summaries expose runtime and
throughput observation counts and complete-token accounting coverage.

The timeline allowlist is **step, event kind, tool name, success, fixed safe error
code, duration and termination reason**. Both live and durable projections omit
arguments (even redacted ones), tool call IDs, tool results, source bodies, model
reasoning/thinking, raw Provider usage/timing JSON and backend diagnostics. Unknown
trace error strings become `internal_error`. SSE never serializes internal objects.
Database/root security identity internals, Agent system prompts, credentials and
Worker options are absent from the UI. Request and normal completed final answer
are intentionally visible to the trusted operator, as text, capped at 32,768 and
65,536 characters with visible truncation. Normal model answers are untrusted and
can quote repository content; no automatic secret redactor is claimed. Internal
source/tool payloads never appear merely because the runtime observed them.

## Validation and limitations

Tests use real migrated temporary SQLite, httpx ASGI requests, raw ASGI stream/
disconnect messages, scripted Providers and observer tests. No live Ollama/GPU,
network, browser automation or external database is required. Native Windows tests
retain their existing platform skips; Linux mocks/regressions do not establish
native Windows integration evidence.

The dashboard does not provide authentication, cross-process subscriptions, a combined
MCP/web launcher, task submission UI, active Worker probes, live Git inspection,
time-range filters, retention, distributed leases, event replay, Worker rankings,
routing or a frontend build. Overview recent activity is recent submissions with
current lifecycle; it is not a separately invented audit-event log. Live telemetry
aggregates become available only at the existing terminal checkpoint.

## Councils

See [Council operations](councils.md) for durable independent execution on 2–16
explicit Workers, ordered per-participant results, partial failures and cancellation.
MCP adds `delegate_council`, `get_council`, `cancel_council`. The dashboard navigation
adds Council history/detail with Task links and live refresh using the same bounded
TaskObserver. The external Director remains the judge; telemetry stays per Task.


With explicit `--coding` configuration, Task detail also displays private-path-free
coding branch/base/state, changed files, bounded current diff, actual validations
and observed write counters. Source/output stays escaped plain text. “Remove
workspace” is a terminal-only POST with confirmation and existing CSRF checks;
review/export uncommitted changes first. The task branch is retained and no
push/merge controls are provided. See [coding](coding.md) for trust/recovery limits.

## Worker diagnostics

The Workers page separates TOML Configuration from Last observed diagnostics.
It reads the small durable diagnostic checkpoints, including unknown/partial
metrics, configured capability labels, health/model presence, each explicit probe
outcome and completion/last success/failure timestamps. Endpoint class is conservative
literal address scope; hostnames stay unknown and no endpoint URL/path is displayed.
A configuration change suppresses earlier target evidence.

Use the CLI instructions on the page for explicit cheap health or one synthetic
inference. There are no dashboard probe buttons or inference on GET, page load or
refresh. Remote/cloud probes send fixed synthetic data and can incur cost/retention.
Task telemetry remains separately available on overview/detail. See
[Worker diagnostics](worker-diagnostics.md) for metrics and limits. Existing trusted
local exposure, Host/CSRF protections and escaping apply unchanged.
