# Independent Councils

A Council durably groups ordinary Tasks executing the **same request independently**
on **2–16 distinct Workers explicitly supplied by the caller**, in caller order.
The external Director chooses participants and evaluates every returned opinion.
AgentForge does not select, replace, rank, vote, judge, synthesize or seek consensus.
There are no debate rounds or participant-to-participant context. Each AgentRuntime
invocation builds its own message history and observations.

## Submission, persistence and execution

`Application.councils` is the shared transport-independent `CouncilService` with
`submit`, `get`, `list`, `cancel`, `detail` and Task membership lookup. Application
also exposes `delegate_council`, `get_council`, `cancel_council` for MCP. Web and MCP
use this service rather than duplicating validation or cancellation rules.

Submission validates the entire bounded request, registered Project, actual Agent,
every explicit Worker and all runtime/tool/Provider bindings before any insert.
Request text reuses the Task contract: nonblank, at most 32,768 characters.
There is no discovery, default Worker, availability probe or replacement. A Worker
that fails at execution retains its identity and failure outcome.

Migration **`0007_councils`**, after `0006_windows_root_identity`, adds:

- `councils`: `council_id`, historical `project_id`, `agent_id`, `request`, UTC
  `created_at`. No Project FK, consistently with Task history.
- `council_participants`: Council FK, Task FK, explicit `worker_id`, `ordinal`.
  `(council_id, ordinal)` is the primary key; `(council_id, worker_id)` and `task_id`
  are unique. Ordinals are constrained to 0–15. FKs have no cascading deletes:
  referenced Tasks cannot disappear accidentally, and deleting membership or
  downgrading the Council migration never deletes Task/telemetry history.
- Creation time/ID index for deterministic descending history; the participant
  primary key supports ordered membership reads and unique Task index supports
  membership lookup.

`TaskEngine.submit_council` validates all bindings and calls
`TaskRepository.add_council`. One short transaction creates the Council, every queued
Task and every membership row. Failed inserts/flushes roll back everything. Failed
commits invalidate the connection so pending rows cannot later be committed by a
pooled checkout. Notifications and monotonic queue origins are registered only after
commit. No session spans inference. As with ordinary storage, a lost connection
**after** a database has actually committed can leave the caller uncertain whether
submission took effect; there is no distributed exactly-once or idempotency key.

Every participant is claimed by the existing TaskEngine executor loops. Configured
concurrency (1–32) remains the only execution bound: eight participants with
concurrency two execute at most two Tasks concurrently, sharing capacity with all
ordinary Tasks. No Council execution loop or unbounded gather exists. Provider/model
snapshots, runtime limits, read-only tool permissions, final results and telemetry
follow ordinary Task semantics. Caller order determines presentation, not scheduling
or priority; normal Task creation-time/ID queue ordering still applies.

## Outcomes, cancellation and recovery

Council snapshots derive participant lifecycle/results from Tasks in a consistent
short read transaction. There is no duplicated Council execution state/result JSON.
`terminal` means all participants are completed, failed or cancelled; it makes no
claim that the Council succeeded. `participant_counts` reports all five Task states.
Mixed outcomes preserve every successful answer and safe failure category separately.

`cancel` calls `TaskEngine.cancel_task` for every observed nonterminal participant.
Queued cancellation prevents claim; running cancellation persists a cooperative
request and signals the ordinary runtime token. Remote inference may continue.
Already terminal Tasks stay unchanged. A durable running cancellation beats a later
completion checkpoint; completion committed first remains terminal. Individual Task
cancellation immediately appears in Council snapshots. Repeated Council cancellation
is safe. Cancellation is a series of normal Task transactions: if storage becomes
unavailable midway, earlier requests remain durable and the Director can retry.
It is not an atomic all-participant cancellation transaction.

On restart, membership and queued Tasks survive. Existing exclusive TaskEngine
startup marks orphaned running Tasks failed with `execution_interrupted`; it never
replays them. Queued participants execute normally on their explicit Worker IDs,
with the existing execution-target snapshot refresh at claim. Changed/missing Worker
configuration has ordinary Task semantics, with no fallback. One process owns a
Task database; there is no distributed lease or cross-process observation.

## MCP usage and privacy

Using the actual MCP tool contracts:

```json
{
  "project_id": "<registered UUID>",
  "agent_id": "repo_explorer",
  "task": "Review the repository architecture",
  "worker_ids": ["local-4080", "home-i5", "ai395"]
}
```

Pass this to `delegate_council`, which returns promptly after durable submission;
it does not await inference. Poll `get_council` or call `cancel_council` with
`{"council_id": "<returned UUID>"}`. These tools are discoverable through the MCP SDK;
`describe_capabilities` advertises the three operations and maximum participants.

The snapshot contains Council identity, Project/Agent IDs, creation time, terminal
flag, state counts and ordered participants. Each participant exposes only Worker/
Task IDs, state, configured Provider/model, completed final answer, safe reason/error
code, cancellation-request timestamp and telemetry coverage status. **Request text
is omitted from MCP**, consistently with Task snapshots. Request and normal
final answers are intentionally stored private runtime text; final answers are
intended opinions, not scrubbed arbitrary model text.

No Council surface projects model reasoning, trace/tool arguments or results, source
bodies from traces, raw Provider responses, raw exceptions/SQL, endpoint URLs,
credentials or Worker options. Council reads extract only the final-answer field
from Task result JSON, not full trace JSON. MCP errors retain the existing fixed
machine-actionable categories (`invalid_arguments`, Project/Agent/Worker not found,
`invalid_execution_binding`, `storage_unavailable`, `service_unavailable`,
`internal_error`), adding `council_not_found` and core `invalid_council`. Tool schema
validation failures use `invalid_arguments`, as ordinary Task calls do.

## Dashboard and observation bounds

The navigation links to `/councils`: compact history with Project name/ID, Agent,
participant count, UTC creation time, terminal flag and state counts. Default page
size is 25, maximum 100, offset 0–1,000,000. History orders by creation time then
Council ID descending and never loads request/result/trace JSON. Offset pagination
is deterministic for fixed history; new submissions may shift pages.

`/councils/{id}` displays request text and participants in caller order with ordinary
Task links, states, configured targets, safe outcomes and independent telemetry.
Every participant remains in ordinary Task history; Task detail links back to its
Council. Request and answer text are autoescaped, capped at 32,768/65,536 characters
with visible truncation. No answer is scored or highlighted as preferred.

Live detail uses the existing TaskObserver through `subscribe_many`, sharing **one
16-hint queue per Council connection** across at most 16 participant IDs. The global
**128-subscriber** cap includes both Task and Council pages; registration state is
bounded by 128 × 16 Task references. There is no separate Council event registry,
trace store or durable event system. Nonblocking publication drops pending hints on
overflow and requests resync. SSE contains only `refresh/resync/terminal/shutdown`
and `{}`, with 15-second idle comments. A single participant's terminal notice
becomes refresh until all participants are terminal. Connection/reconnection reloads
the authoritative fragment; disconnect, pre-iteration cancellation and shutdown
release all registrations. The shared browser script coalesces refreshes, and a
slow page never blocks execution.

“Cancel remaining participants” requires POST, explicit checked confirmation and
the existing signed double-submit CSRF/Origin/Fetch Metadata checks. It calls the
same Council service and normal Task cancellation path. Local trusted operator and
single-process hosting limitations from [the dashboard](dashboard.md) still apply.

Telemetry remains independently queryable terminal Task telemetry. Council UI shows per-
participant coverage, runtime, token accounting and observed throughput with unknown
values preserved. Only participant state counts are aggregated. There is no combined
heterogeneous throughput, performance score or Worker ranking.

## Validation and non-goals

Offline tests use migrated SQLite, scripted Providers, actual MCP SDK clients and
HTTP/ASGI streams. They cover atomic failures including commit, membership constraints,
2–16 participants, same-request independence, bounded shared capacity, mixed outcomes,
queued/cooperative cancellation and race orderings, restart recovery, historical
Project removal, migration upgrade/downgrade, safe errors, escaping and bounded live
updates. Native Windows integration retains its platform skips on non-Windows hosts.

Council does not provide a judge, consensus, synthesis, automatic routing or fallback,
benchmarks, another Provider, write-capable Agent, worktrees, generic shell,
distributed queue, remote repository sync or authentication platform.
