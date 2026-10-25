# Capability evidence, data egress and resource policy

The external Director chooses the Project, Agent and Worker. AgentForge advises
and enforces authorization; it never routes, retries inference or replaces a
Worker. `suggest_workers` performs bounded database/configuration reads only.
It sends no inference, health request, repository content or synthetic probe.

## Compatibility and security design (#51)

The existing Worker configuration remains the registry. Named `ProviderConnection`
IDs identify destinations; adapters still implement protocol mechanics. Worker IDs,
`supports_tools`, `supports_streaming` and `context_window` retain their meanings.
There is no duplicate model registry, plugin loader, scheduler or quality score.
All current entries have `kind = "inference_worker"` and transport
`text_and_tools`. Executable specialist Tools (ComfyUI/Blender) need separate
contracts in #52. Declaring `image` or `3d` does not enable execution or transport.
Required vision/image/3d/video/speech operations are excluded by advisory selection
because their transport is not implemented in these adapters.

Project egress policy is durable operator authorization. Task restrictions are
intersected with it, never treated as authorization. TaskEngine validates all
Council bindings before creating any participant; AgentRuntime authorizes before
each Provider call. Application, stdio MCP, HTTP Gateway, dashboard and isolated
coding share those checks. Coding uses the original Project's policy, including
when its tools operate on an isolated worktree. Local outage produces the original
failed Task; it cannot cause a cloud request. No prompt, tool argument, repository
instruction or model answer can modify Project policy or connection trust.

Gateway grants add client restrictions. Access to a Project does not authorize
all its Workers or any cloud connection. `suggest_workers` requires its own exact
tool grant, a Project grant and Agent grant. It filters Worker IDs **before** loading
evidence or pagination. Hidden Workers, endpoints, credentials, other Projects,
requests, raw responses and private reasoning are not returned. Remote suggestions
also omit aggregate connection usage counters, which can include other Projects. Discovery never
allows clients to edit operator policy.

## Declarations versus observations

Example Worker fields (illustrative declarations only):

```toml
[[workers]]
id = "engineering-model"
provider = "windows-inference"
model = "<operator-model-id>"
supports_tools = true
supports_streaming = false
context_window = 32768
[workers.capabilities]
coding = true
reasoning = true
vision = false
```

Capability keys are `coding`, `reasoning`, `tool_calling`, `vision`, `image`, `3d`,
`video`, `speech`. Use `supports_tools` for `tool_calling`; a duplicate capability
key is rejected. Omitted support is unknown. Streaming stays disabled by default
for compatibility; context capacity is declared, never measured automatically.
No ability is inferred from a model name, Provider protocol or machine label.

Each discovery descriptor has effective `support` (supported/unsupported/unknown),
a `declaration` with source/provenance and an `observations` tuple. Configuration
is `declared`, never measured quality. Synthetic tool diagnostics retain their
actual timestamp, `measured` source and **synthetic_tool_probe** provenance with
an explicit one-exchange limitation. Positive probes never promote unknown support
or establish universal capability. A conflicting failed probe makes effective
support unknown, preserving both pieces of evidence. Other diagnostic failures
are availability/failure evidence, not proof of an unsupported capability.

Availability is unknown until explicitly observed. Suggestions exclude observed
unavailability and identify its timestamp; even observed success is historical,
not a live guarantee. A Director may deliberately submit the same authorized
Worker to check recovery; ordinary submission does not health-probe. A newer
health observation supersedes older connection failure evidence. No model quality
benchmarking or automatic expiry/reset of evidence is implemented.

## Destination trust

Named connections accept `trust = "local"`, `"cloud"` or `"unknown"` (default).
Inline legacy Ollama accepts the same field on its Worker and uses stable identity
`inline:<worker-id>`; named connection IDs must not collide with an inline
Worker identity. Named connections own their trust, not individual Workers.
Only literal loopback IP addresses and exact `localhost` retain an implicit local
compatibility classification. Protocol type and names such as `local`, `.local`
or `ollama` do not establish trust. Unknown non-loopback destinations fail closed,
even if an operator accidentally lists their ID in a cloud allowlist.

`trust = "local"` is the operator's explicit assertion that the endpoint and its
backend processing remain inside the authorized local network. It includes approved
LAN/Tailscale inference on Windows/i5/other hosts; local does not mean same process.
Verify actual host ownership, routing, DNS, TLS and backend forwarding/retention
before declaring it. Protect configuration and DNS/firewall policy. AgentForge
validates typed metadata, exact connection bindings and URL/authentication safety;
it cannot attest a service's physical location, DNS behavior or absence of upstream
forwarding. No DNS or inference probe is made to determine locality. Use HTTPS for
confidential LAN context; plaintext HTTP still exposes it to the trusted network.
Adapters reject redirects and do not forward credentials to another origin.

## Project authorization and migration

Stop executors, back up private database/workspaces, install the reviewed package,
then explicitly upgrade to `0003_capability_policy`. Released migrations
`0001_initial` and `0002_gateway_client` are unchanged. Existing Projects migrate
to `local_only`; no Project becomes cloud-authorized. Existing loopback Workers
and their IDs/configuration still work. Existing LAN deployments must explicitly
classify their connection as local after validating trust. Remote/cloud workloads
require operator authorization, regardless of earlier client grants.

```sh
agentforge db upgrade --database-url sqlite:///private-state.db
# A registered sensitive engineering Project stays local-only:
agentforge project egress '<engineering-project-uuid>' --mode local_only \
  --database-url sqlite:///private-state.db
# Only a separately registered disposable test Project may use these connections:
agentforge project egress '<test-project-uuid>' --mode cloud_allowed \
  --allow-connection cloud-a --allow-connection cloud-b \
  --database-url sqlite:///private-state.db
```

`cloud_allowed` permits local destinations plus exactly its authorized cloud
connection IDs. Empty cloud allowlists authorize no cloud egress. Unknown IDs do
not match another connection. Reusing an ID for a different destination is an
operator security decision: review every Project allowlist before doing so.
The trusted Python equivalent is `projects.set_egress_policy(id, EgressPolicy(...))`.
These administrative operations follow registration's stop-executors rule and
are absent from MCP/Agent tools. There is no DLP/content classifier.

Task/Council arguments optionally include:

```json
{"task_policy": {"egress": {"mode": "local_only"}, "max_requests": 2, "max_tokens": 4096}}
```

Omitted egress inherits the Project. A Task cloud allowlist must be a subset of
its Project's cloud authorization, even when submitting to a local Worker. Task
restrictions and connection budgets both apply; a larger Task number cannot raise
a connection's cap. Every Council participant retains the same Task restriction.

## Advisory selection

```json
{
  "project_id": "<registered-project-uuid>", "agent_id": "repo_explorer",
  "required": ["coding"], "preferred": ["reasoning"], "require_tools": true,
  "min_context_window": 16384, "task_policy": {"egress": {"mode": "local_only"}},
  "limit": 25, "offset": 0
}
```

Call `suggest_workers` with this typed request. Candidates are in stable Worker ID
order, with `eligible`, `hard_exclusions`, unknown capabilities, supported
preferences, declarations, scoped measurements, availability and resource evidence.
Unknown required support/context is an explicit exclusion; optional preferences
never relax requirements. No arbitrary task text is accepted by this operation.
If a page contains no eligible Worker, the response says so without relaxing policy;
`next_offset` may identify another page. Suggestions reserve nothing. The Director
still calls `delegate_task`/`delegate_council` with exact IDs and bounded request text.

## Request budgets, tokens and cooldowns

Connections may have `[providers.budget] max_requests = 100` and/or
`max_tokens = 50000`. These are **lifetime operator budgets for that connection ID**,
shared by its Workers and Projects, not a free-tier allowance or reset window.
Each attempted inference is charged atomically before I/O. Short SQLite writer
transactions serialize admission; no transaction spans inference. Task restrictions
count only that Task's attempts/observed tokens. Synthetic operator inference probes
use the same persistent connection admission; health/discovery are not inference.
Explicit diagnostics still send only their fixed synthetic payload, never Project data.

Tokens are counted from normalized reported total or complete input+output counts,
using the larger when they conflict. Missing counts, failed/interrupted requests,
or out-of-bound counters make usage unknown and block further token-budgeted
admission. A connection token budget permits one in-flight inference at a time,
conservatively reserving its remaining allowance. Unfinished reservations survive
crash/restart and block further token-budgeted admission pending operator
reconciliation. Request limits remain charged even on failure. There is no automatic
reset, refund, polling, blind replay or reservation expiry.

Remaining tokens cap the requested output without increasing a configured output
cap. Exact input usage is unavailable before inference; backends can ignore output
caps, and one response may exceed the remaining allowance. Subsequent requests are
denied. A budget check **does not guarantee zero billing or an exact cost ceiling**.
Provider quota/credits, pricing and billing exposure remain unknown. After uncertain
usage, the operator must reconcile backend accounting before deliberately provisioning
a new budget/connection identity; do not simply rotate identities to bypass limits.

HTTP 429 retains `provider_error` on the actual Task and records a fixed sanitized
`rate_limited` observation. A valid standard Retry-After creates a cooldown of at
most 86400 seconds, using #48 normalization; missing/invalid/out-of-range headers
remain unknown. Cooldown denies new attempts, never schedules retries. Original
Provider failures are preserved if evidence storage itself fails; charged unfinished
reservations remain conservative. Suggestions expose relevant failure and timestamps
without backend bodies, secret headers, endpoints or prompts.

## Identity and remaining acceptance

New Tasks persist connection ID, model, protocol, policy and a private configuration
fingerprint. Claiming queued Tasks preserves submitted attribution. Restart with a
changed target/configuration fails safely instead of executing under the old label.
Old historical Tasks/Councils retain their identity; unknown historical connection
IDs remain null. Connection credentials/endpoints are not persisted. Fingerprints
exclude credential values; rotation under the same environment name is not detectable.
There is no automatic fallback or linked retry lifecycle. A Director can explicitly
submit another authorized Task; linked retry workflows remain #53.

Deterministic CI validates protocol/policy behavior without credentials or GPUs.
Physical RTX 4080 model/tool capability, authorized cloud quota observations,
Windows Gateway/Mac-local privacy, outage behavior and truthful identity/telemetry
remain unchecked in [the existing #31 register](https://github.com/tinuvael/AgentForge/issues/31).
