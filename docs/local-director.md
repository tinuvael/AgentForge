# Local Director operator guide

Local Director is an independent MCP client. Codex remains a separate CLI/IDE
Director. There is no Codex browser mode or subscription API dependency.

## Optional public web research

Set `web_research = true` at the root of the private Director TOML and restart
the Gateway at an idle boundary. The browser's **Allow public web research
using text from this request** checkbox is off by default for each message.
The one-shot CLI equivalent is `director run --web-research`. The permission
applies to the current request only. Approval and recovery do not renew it.

`web_search` sends an exact phrase from the current request to DuckDuckGo's
public HTML search and returns up to five result links. It uses no API key or
paid account. The HTML interface can be throttled, blocked, or changed by the
service. `web_read` accepts a public URL supplied by the operator or returned
by this request's search. It returns bounded text from plain text and HTML
pages, with the final source URL. Pages requiring JavaScript, sign-in or a
non-text format may be unavailable. The chat links to pages actually read.

Only opt in for content authorized for public disclosure. The search service
sees the selected phrase and the public website sees the page request. Local
repository files, previous messages, Worker replies and page text cannot supply
search phrases or add URLs to the request's authorized set. The network client
revalidates each redirect, rejects non-public DNS addresses, pins the validated
address for the connection and retains hostname verification for HTTPS. It
sends no credentials or cookies and applies byte, time and operation limits.
Retrieved text remains untrusted. The Gateway's Project and Worker policies
continue to govern delegated work independently.

## Prepare the Windows host

Install AgentForge in a Python 3.12+ venv. Follow [Gateway setup](gateway.md) to
upgrade private storage, register the Windows checkout, configure explicit
Workers and provision client credentials. Keep **one** Gateway running against
that database. Do not start standalone `web` or another executor on the same DB.

Start local llama-server with a GGUF model and template supporting native tool calls
(see [llama.cpp setup](llama-cpp.md)). The Director
and Workers may share the same RTX 4080 endpoint; memory contention and quality
require physical measurement under #31. No inference health/quality is inferred
from configuration. Windows cannot inspect uncommitted Mac-local files.

Provision a distinct `local-director` Gateway client, never reuse Codex's token:

```powershell
agentforge gateway client-add --auth C:/AgentForge/private/clients.json --client-id local-director --token-file C:/AgentForge/private/director.token
```

Use the existing private client policy procedure to grant only the required
Project UUIDs, Worker IDs, Agents and tools. The initial credential has no grants.
Typical tools: `agentforge_status`, `list_projects`, `list_agents`, `list_workers`,
`suggest_workers`, `delegate_task`, `get_task`, `watch_task`, `cancel_task`.
Optional Council tools are `delegate_council`, `get_council`, `cancel_council`;
participants consume the same request Task budget and retain independent results.
Coding additionally needs `coder`, `coding_submit = true` in JSON policy, and
explicit coding inspection/cleanup tools if desired. Keep `read_shared` and
`cancel_shared` false. Project egress policy independently limits every Worker;
client grants cannot override local-only Projects.

## Private Director configuration

Store TOML, credentials and journal **outside Git repositories**. On POSIX use
private mode 0700 directories/0600 files; on Windows restrict ACLs to the operator
account. The journal directory is exclusively owned by one Director process.
Use separate journal directories for independently running CLI and browser clients
and provision separate Gateway identities for them.

`limits.sessions` counts every conversation in the journal, including those
created under other browser identities. A new browser profile may show an empty
sidebar when the journal is full. To keep existing history, back up the journal,
raise `limits.sessions` (up to 64), and restart at an idle boundary. You can
continue a visible conversation without allocating a new journal entry.

Example `C:/AgentForge/private/director.toml` (replace placeholder paths/IDs):

```toml
gateway_url = "http://127.0.0.1:8766/mcp"
token_env = "AGENTFORGE_DIRECTOR_TOKEN"
journal = "C:/AgentForge/private/director-cli-history"
model = "local-model" # Same explicit alias as llama-server.

[inference]
id = "director-llama"
type = "openai_compatible"
base_url = "http://127.0.0.1:8080/v1"
trust = "local"

[limits]
turns = 12
calls = 96
tasks = 3
seconds = 240
sessions = 32
active = 2
```

The default example uses [llama.cpp](llama-cpp.md). For an authenticated local
compatible server, optionally configure
`api_key_env = "LOCAL_INFERENCE_KEY"`. Authenticated remote endpoints require
HTTPS. This release requires `trust = "local"` for **Director inference** so
repository material cannot reach a cloud Director outside Project egress policy.
This restriction is independent of explicitly authorized cloud Workers.
No automatic fallback exists. Endpoints/configuration cannot be edited in chat.

Load the token server-side without printing it:

```powershell
$env:AGENTFORGE_DIRECTOR_TOKEN = (Get-Content C:/AgentForge/private/director.token -Raw).Trim()
agentforge director run --config C:/AgentForge/private/director.toml "Inspect the registered project for serious bugs using local models."
agentforge director chat --config C:/AgentForge/private/director.toml
```

The CLI emits its session ID, bounded visible messages, progress, real Task IDs
and pending approval details. `/quit` exits interactive chat. Ctrl-C cancels the
Director's current wait; already-submitted Tasks remain authoritative at Gateway.
Use `director cancel --config ... --session UUID --task TASK_UUID` to prepare a
precise cancellation approval, then explicitly approve/reject its nonce:

```text
agentforge director approve --config director.toml --session UUID --nonce NONCE
agentforge director reject --config director.toml --session UUID --nonce NONCE
agentforge director resume --config director.toml --session UUID
agentforge director clear --config director.toml --session UUID
```

Approvals name the operation, Project, Task (when one already exists), Worker and
exact arguments. They expire after five minutes and are consumed before I/O.
Models cannot approve themselves. No global approval exists. Workers never commit,
push, open PRs or merge. Coding changes stay in isolated worktrees for review.

## Dashboard Web Chat

Provision a separate `web-director` credential using the procedure above. Copy
the example TOML to `director-web.toml`, give it a distinct token environment
variable and journal directory, and point it at the **same Gateway** being started.
Gateway refuses a chat configuration targeting a different host/port. Start the
existing Gateway with its loopback Companion and opt-in chat:

```powershell
$env:AGENTFORGE_WEB_DIRECTOR_TOKEN = (Get-Content C:/AgentForge/private/web-director.token -Raw).Trim()
agentforge gateway serve --database-url sqlite:///C:/AgentForge/private/tasks.db --workers C:/AgentForge/private/workers.toml --auth C:/AgentForge/private/clients.json --companion --director-config C:/AgentForge/private/director-web.toml
```

Open `http://127.0.0.1:8765/director` on Windows, or click **Local Director** in
the existing Dashboard. Click **New conversation**, select a Project and Worker
or leave explicit selection to the Director, then send a natural-language request.
Visible Worker choices are credential-scoped; candidate eligibility is checked for
the actual Project/Agent immediately before delegation. Inspect execution activity,
real Task links and evidence metadata. Worker execution and Director analysis are
separate entries; there are no fabricated percentages or time estimates.
Use a normal browser with a same-origin browsing context. Embedded browsers that
send `Origin: null` on form submissions are deliberately rejected.

Coding requests pause with the exact requested operation, Project and arguments.
**Approve this action** submits it once; **Reject** does not submit it. Approval
requires the browser's conversation cookie, a valid CSRF token, matching revision,
nonce and unexpired stored arguments. A refresh cannot approve anything. Browser
input cannot change pending action parameters. Coding also requires operator coding
configuration on Gateway and an isolated workspace. No automatic commit exists.

**Stop Director request** stops its inference/wait. Submitted Tasks continue; the
Task evidence card offers a separate cancellation approval. **Restore known Task
results** reads durable IDs without another delegation. Reopening a conversation
or refreshing only reads its saved state. **Clear this conversation** explicitly
removes its private transcript/pending approval, retaining Gateway history and
workspaces. Resolve active/ambiguous work first. Expired unsubmitted approvals can
be discarded by clearing the conversation. Browser identity lasts 30 days; clearing
cookies loses access to its private conversations. CLI cannot adopt browser-owned
transcripts. Keep host storage/backups private.

Progress uses bounded HTMX snapshot polling every two seconds while active.
There are no retained browser subscriptions/queues. Closing a tab releases HTTP
resources while the bounded Director request continues. Reconnect reloads a
replacement snapshot; it never replays a send. Each form has a revision guard to
reject duplicate/stale submissions. Gateway or model errors appear as fixed safe
codes; reload to reconnect, then restore known IDs. Plain text rendering escapes
model HTML and scripts; Markdown is displayed as text.

Chat is disabled without `--director-config`, unavailable in standalone `web`,
and never enabled on its unauthenticated LAN listener. Credentials stay server-side.
The full Dashboard remains a trusted operator interface, not a public multi-user
service. Browser cookies separate chat conversations within that trust boundary;
they do not make it suitable for public access. Keep each web credential narrowly
scoped without shared-history/cancellation grants.

## MacBook browser access

Use authenticated SSH forwarding to the Windows **loopback Companion**, over an
already trusted private transport. For example, after configuring and verifying
Windows OpenSSH key authentication and its host key:

```sh
ssh -N -L 8765:127.0.0.1:8765 operator@windows-private-host
```

Then open `http://127.0.0.1:8765/director` on the MacBook. The SSH login authorizes
access to this privileged operator UI. Tailscale membership alone is insufficient.
Do not publish/forward the dashboard through an unauthenticated reverse proxy or
public listener. Direct remote browser authentication is intentionally outside
this release. MCP bearer authentication remains separate from browser cookies;
never put the MCP token in browser JavaScript. Windows-hosted Projects and optional
Mac-local AgentForge instances keep separate databases and histories.

## Recovery and evidence

Known IDs are re-read from Gateway; resume cannot delegate new work. Task identity
and explicit Worker do not change. If a submission response is lost, the journal
records an ambiguous state and refuses replay. Inspect Gateway Tasks as an operator
to reconcile manually; do not infer that no Task exists from a network error.

The Director reads authoritative terminal results and source-tool observation
metadata. It can synthesize/cite Worker source references but cannot independently
re-read source through this contract. `source_tools_observed` proves successful
source-tool activity, not correctness of every Worker claim. Findings without that
evidence remain explicitly unverified in the Task evidence view.

History retains at most 24 visible messages, 16 Task bindings and 32 progress
entries per conversation, with a bounded private journal and explicit clear.
No chain-of-thought, bearer token or raw Provider response is intentionally stored.
Intentional user/assistant messages and approved Task arguments can contain sensitive
text: apply normal private host storage/backups. Task history remains at Gateway
when a conversation is cleared. Capacity limits fail closed rather than silently
discarding recovery identities.

CLI output includes retained workflow IDs and observed states. CLI and browser
Clear share one guard: submission ambiguity, unfinished Tasks or any workflow
outside `completed`/`failed`/`cancelled` prevents journal deletion. Approval, paused,
blocked and recovery-required workflows retain their identities. Resume refreshes
cached bindings after Gateway completion; investigate/cancel unresolved work before
clearing. Clear never cancels Gateway work. Ordinary terminal conversations remain
clearable, and stale browser revisions remain rejected.

## Outages and physical acceptance

If llama-server is unavailable or returns 429, the request stops with a bounded error;
there is no paid fallback or automatic inference retry. Gateway read loss has a
bounded reconnect allowance; mutations are never blindly retried. A lost Internet
connection or unavailable Codex does not stop local Gateway/llama-server. Configured cloud
Workers still need their own Internet/API access. Gateway restart interrupts work
according to the existing durable Task recovery rules.

Scripted acceptance does not establish model quality, actual GPU use, Windows
browser behavior or MacBook access. Keep those checks pending in
[physical acceptance #31](https://github.com/tinuvael/AgentForge/issues/31).

Specialist discovery is available through `list_specialist_tools` when Gateway
grants permit it. Definitions distinguish deterministic fakes from configured,
untested ComfyUI HTTP and local Blender adapters; discovery does not probe hosts. The
Local Director executes bounded specialist/inference chains through the shared
workflow catalog and reads job/artifact metadata. Direct specialist writes and
binary retrieval remain excluded. Scope review uses the trusted operator workflow
page; pause/resume/cancel and recovery retain real workflow/child identities.
See [workflow usage and acceptance](workflows.md).
See [ToolJob and artifact contracts](specialists.md).


## Workflow execution

Use `submit_workflow` for explicit small artifact/text chains. The Director records
its exact idempotent submission before I/O, observes `get_workflow`, persists IDs,
and returns actual outcomes. Workflow attempts count against Director task bounds.
The full declared attempt budget is reserved with the exact pending submission in
one atomic journal checkpoint before sending. Recovery reuses that reservation and
idempotency key, so response loss, process loss and repeated resume cannot omit or
double-charge it. Reservations remain charged when acceptance is uncertain. Resume
retains the request budget; only a new human request resets it. Older journals with
workflow bindings but no accounting evidence permit observation while conservatively
exhausting the remaining request credit. Pending legacy submissions are reserved
before retrying their same idempotent request. See [workflow accounting](workflows.md).
After uncertain transport submission, resume can recover the same immutable key
without creating another workflow. Models cannot approve steps or retrieve binary
artifacts. The chat links workflow review pages and restores known workflow results;
server step approval remains a trusted operator action. Workflow resume/cancel
controls use the existing Director one-use human approval. See [examples](workflows.md).
