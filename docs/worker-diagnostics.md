# Worker diagnostics

Diagnostics describe what an explicitly selected Worker did during a controlled,
point-in-time observation. They supply evidence to the operator, never routing,
ranking, fallback, recommendations or model-quality scores. Startup, configuration
listing, MCP discovery and dashboard loads never contact inference Providers.

## Four separate sources of evidence

| Source | Meaning |
| --- | --- |
| Configuration | TOML declares model, deployment, context window, tools and streaming. |
| Health | A cheap explicit Provider check observes backend/model availability where supported. |
| Diagnostic performance | One explicit synthetic inference observes generation, optional tools/streaming and factual metrics. |
| Task telemetry | Ordinary useful Agent work has its own observations, coverage and history. |

Configured context is never a measured context limit. Configured tool/streaming
support does not establish observed success. One successful call does not guarantee
future reliability. Diagnostic generation does not prove the backend served the
configured model identity: compatible servers can alias or substitute models.
The configured model remains the target identity; only Ollama's model list currently
establishes model presence. Backend response model names are not retained.

## Operator commands

```sh
agentforge worker list --workers workers.local.toml
agentforge worker check local-4080 --workers workers.local.toml
agentforge worker check local-4080 --workers workers.local.toml --database-url sqlite:///agentforge.db
agentforge worker probe local-4080 --workers workers.local.toml --database-url sqlite:///agentforge.db
agentforge worker probe local-4080 --kind tools --workers workers.local.toml --database-url sqlite:///agentforge.db
agentforge worker probe local-4080 --kind streaming --workers workers.local.toml --database-url sqlite:///agentforge.db
agentforge worker diagnostics local-4080 --workers workers.local.toml --database-url sqlite:///agentforge.db
agentforge worker diagnostics --all --limit 25 --offset 0 --workers workers.local.toml --database-url sqlite:///agentforge.db
```

`list` and `config-check` stay offline. `check` never generates; its existing
`available`/`unavailable`/`not_probed` JSON status is preserved and enriched with
completion time and a typed observation. Without a database it is transient;
`--database-url` records it. Ollama checks `/api/tags` for backend availability
and configured model presence, with a five-second maximum. Reachability is distinct
from availability: a received rejection/invalid response proves contact, while a
transport failure reports unreachable and a timeout stays unknown.

OpenAI-compatible health stays **not_probed** with unknown backend/model fields;
there is no universal `/models` or `/health` request. A successful generation probe
does not relabel this separate health observation. `diagnostics` reads bounded
persisted snapshots only. `--all` is passive, paginated and never benchmarks Workers.

`probe` performs at most one inference, defaulting to `generation`; each kind is a
separate explicit action. It requires an already migrated database. Success,
not_probed and not_applicable exit 0; failed health/probes exit 7. Existing invalid
configuration, missing Worker, storage and schema exit codes remain 2, 5, 3 and 4.
Probe failure is structured JSON with an allowlisted category, never backend text.
No prompt, tool definition or Project argument can be supplied to the command.

## Fixed probe contract and bounds

- Generation/streaming send `Reply with the word agentforge.` with temperature 0
  and a 32-token output budget. Any nonempty generation verifies generation;
  there is no answer grading. Streaming requires visible output and a terminal
  chunk. Tools in the generation/streaming response are invalid.
- Tools send `Call diagnostic_echo exactly once with value 'agentforge'.` with
  temperature 0, a 64-token budget and one fixed schema. Exactly one native call
  must have that name and the exact arguments `{"value": "agentforge"}`. The call
  is validated internally; no dispatcher, external tool or second inference runs.
- Tools require configured `supports_tools = true`; streaming requires configured
  `supports_streaming = true`. Otherwise the explicit action reports not_applicable
  with unknown success fields, no request and no measured request duration.
- A probe has a wall-clock budget of min(30 seconds, Worker timeout), at most
  16 KiB of normalized output including private reasoning and tool-call data, and
  at most 256 stream chunks. Shipped Providers additionally enforce their bounded
  wire parsers and HTTP timeouts. Token budgets are protocol requests; a backend
  may ignore them, so local deadlines and response bounds still apply.
- Worker generation options are omitted from probes, including arbitrary stop
  strings. The normalized `GenerationRequest.max_output_tokens` maps to Ollama
  `num_predict` and compatible `max_tokens`. A compatible deployment requiring
  only `max_completion_tokens` may reject the fixed probe. Unsupported backend
  parameters fail safely rather than retrying/adapting automatically.

**Remote/cloud probes cause data egress and can incur cost/retention.** Only the
fixed synthetic message/schema and configured inference target/settings are sent;
no Project registration, repository, Agent prompt, source, Task request or tool
result enters diagnostics. Provider authentication follows the existing connection
contract. Choose each remote action deliberately. This tiny probe does not establish
general model quality, context capacity, sustained throughput or tool reliability.

## Metrics and failures

Observations reuse `TokenUsage` and `GenerationTiming` from the Provider boundary.
Raw compatibility dictionaries are ignored. Token counts are only model-reported,
never estimated from text; missing input/output/total counts stay NULL independently.
Request duration measures the wall-clock Provider operation, including network,
backend, parsing and stream cleanup; it is not backend generation duration.
Backend output duration is `GenerationTiming.output_seconds`, excluding prompt
processing, as in Task telemetry. Output throughput is observed output tokens /
positive backend output duration; absent counts/duration and zero duration yield
NULL. Observed zero tokens with positive duration yield zero throughput.
Compatible Providers supply no trustworthy backend duration, so their throughput
remains unknown even with usage. No request-latency denominator is substituted.

Streaming TTFT measures request start to the first **nonempty visible content
delta** delivered through the actual streaming path. Empty frames, headers,
private reasoning, tool fragments and reported backend durations do not count.
It is a client content-delta observation, not exact tokenizer-level first-token
timing. Non-streaming TTFT stays unknown. Only terminal normalized usage/timing
is consumed; interim counters are not summed. A failed stream can retain an
actually observed first-content time without claiming generation success or
complete usage. `unavailable_metrics` explicitly lists missing metric coverage.

Safe failure categories are backend_unavailable, model_unavailable, timeout,
invalid_response, rejected, tool_call_failed, stream_failed and diagnostic_failed.
No arbitrary exception code/message, backend body, response text, hidden reasoning,
API key, credential environment value/name or endpoint URL/path is projected or
stored. Cancellation propagates and closes locally owned HTTP streams; it does
not establish that remote inference stopped. An interrupted operation records no
completed check; the previous completed observation remains authoritative.

## Storage and service boundary

The first supported `0001_initial` schema baseline creates
`worker_diagnostic_observations` directly. It stores one
latest factual observation plus last success/failure timestamps per Worker/probe
kind: at most four rows per Worker identity. No prompts/responses or Task rows
are stored. Each write updates only the selected Worker's selected probe kind;
unrelated checkpoints survive commands using narrower configurations. Observations
for Workers absent from the currently loaded configuration may remain stored but
are not surfaced by `get`/`list`. Passive reads never mutate storage. There is no
automatic retention/pruning policy or accumulating observation history; distinct
Worker identities can retain dormant checkpoints. A private SHA-256 configuration
fingerprint suppresses prior-target snapshots when model, endpoint, capabilities,
options or connection settings change. Last timestamps reset on replacement.
Credential rotation under the same environment name is not detectable; observations
are historical evidence, never a credential freshness guarantee.

Transactions are short and never span inference. SQLite atomic upserts ensure the
latest completion wins across processes. One service rejects overlapping actions
for the same Worker; independent CLI invocations can each explicitly perform one
bounded request. No distributed lease, persistent lock or automatic retry is added.
The CLI composes diagnostics/storage/Providers without starting a TaskEngine.
Concurrent checkpoint writes alongside one executor are supported; schema upgrades
still require stopping executors. Injected Providers remain caller-owned; Application
and CLI close owned Providers after cancelling diagnostics.

`WorkerDiagnosticsService.get/list/check/probe` return typed Pydantic configuration,
observation, history and page contracts in `workers.diagnostics`. Application exposes
`worker_diagnostics` for operator adapters and future Companion integration. This
is a reusable Python boundary, not an active MCP tool or general REST API. MCP
`list_workers` remains configuration-only. Historical Task telemetry stays in its
existing store/service; its timings/counts are never combined with probe results.

The dashboard Workers page separates Configuration from Last observed diagnostics,
including partial/unknown metrics and last success/failure times. It only reads
these checkpoints and explains the CLI actions. There are no GET/POST probe controls,
background polling/probing or new exposure/authentication rules. Existing Host,
CSRF, escaping and local trusted-operator protections remain in force.

Offline tests cover adapters via MockTransport, synthetic Providers, HTTP closure,
timeouts/cancellation, checkpoints/restart/configuration replacement, JSON/exit
codes and dashboard rendering. Native Windows/Ollama/GPU acceptance, Companion,
Director live progress, general Agents and routing/ranking are outside this change.
