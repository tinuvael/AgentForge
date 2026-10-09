# AgentForge Companion alongside Codex

AgentForge Companion is a local loopback panel designed to run alongside Codex.
Open it in a narrow browser window (roughly 380–600 px) beside your Codex client.
It observes Tasks and provides cancellation and bounded review; the external
Director still submits work and judges Council results.

## Codex capability discovery

Discovery on **2026-10-09** used the installed **codex-cli 0.159.0-alpha.3**:
`codex --version`, `codex --help`, `codex plugin --help`, `codex mcp --help`, and
`codex mcp add --help`. The installation provides a compiled CLI binary, with no
public panel SDK/source bundled alongside it. We did not inspect private runtime
interfaces or use internal hooks. Public documentation was fetched that day:

- [Codex plugins](https://developers.openai.com/codex/plugins/): plugins can bundle
  skills and MCP servers. A plugin command/directory is not itself a panel API.
- [Codex MCP](https://developers.openai.com/codex/mcp/): stdio and Streamable HTTP
  servers, tools, configuration, and OAuth. MCP transport support does not establish
  support for rendering an MCP UI resource.
- [MCP UI](https://developers.openai.com/plugins/build/chatgpt-ui): optional iframe
  components use the MCP Apps bridge in ChatGPT and compatible hosts. The guide
  explicitly keeps tools usable in clients that do not render components.
- [Plugin extensions](https://developers.openai.com/plugins/build/extensions):
  sidebar apps, conversation panels, file viewers and deep links are described
  as **ChatGPT** surfaces. They do not establish a custom Codex CLI panel API.
- [Codex app-server](https://developers.openai.com/codex/app-server/): a backend
  for building one's own Codex client; not an extension surface for inserting
  AgentForge panels into an existing client. The CLI marks it experimental.

The distinctions matter: ChatGPT Apps/plugins, MCP protocol extensions, Codex
MCP client support, and Codex UI rendering are separate capabilities. We found
**no documented suitable native embedded panel in the inspected Codex CLI**.
No native Codex embedding was physically verified. This is a scoped discovery
result, not a claim that every current/future OpenAI client lacks visual plugins.
The selected implementation is the functional local web fallback. It uses public
MCP and HTTP behavior, without DOM injection, private APIs, IPC patching, or a
new frontend framework. Ordinary local browser URLs are the supported operator
entrypoint; no automatic Codex URL-opening callback is assumed or installed.

## Launch with one executor

First follow [operator setup](operator-cli.md): configure Workers, explicitly
upgrade the database, and register your Project. Replace the old MCP launch
arguments with:

```sh
agentforge mcp --database-url sqlite:///agentforge.db --workers workers.toml \
  --companion
```

Then open **http://127.0.0.1:8765/companion** manually. Optional flags:
`--companion-host 127.0.0.1` (explicit loopback IP only, including `::1`) and
`--companion-port 8765` (1–65535). The port is fixed unless configured; a bind
collision fails startup safely. The server does not launch a browser or write
its URL to stdout. IPv6 URLs use brackets, e.g. `http://[::1]:8765/companion`.
The retained `python -m agentforge.mcp.server` supports the same flags.

For the Codex CLI, configure its supported stdio server launch:

```sh
codex mcp add agentforge -- agentforge mcp \
  --database-url sqlite:////absolute/path/agentforge.db \
  --workers /absolute/path/workers.toml --companion
```

Use absolute configuration/database paths because the MCP working directory can
vary. Supply `--coding /absolute/path/coding.toml` only when opting in to coding.
Codex owns the MCP child process; opening the browser does not create an executor.
For long `watch_task` requests, configure Codex's MCP `tool_timeout_sec` as
appropriate; cancellation of a watch request only disconnects the observer.

One process constructs **one Application and one TaskEngine**. MCP's lifespan
starts and closes that Application. HTTP borrows the already started Application
on the same event loop and never starts/closes/disposes it. The existing database
owner guard remains intact. HTTP uses one Uvicorn server with no independent signal
handler, access logs, or logging configuration. MCP stdout is exclusively JSON-RPC;
sanitized operational diagnostics go to stderr.

**Do not also launch `agentforge web` or another MCP executor on that database.**
The existing owner guard detects duplicate owners within a process; it is not a
distributed/process lock. The one-executor rule remains an operator requirement.
MCP-only works without `--companion`. Dashboard-only remains
`agentforge web ...`; `/companion` is also available within that dashboard's
existing Application when MCP is not running. No second HTTP-only launch mode is
introduced. The existing dashboard routes share the same local operator boundary.

Closing a browser tab releases its subscriptions without cancelling Tasks.
On MCP EOF or process interruption the Application closes its observer, stops
execution, and disposes owned Providers/database; HTTP drains and stops with it.
Retained interrupted-Task recovery semantics apply at the next startup.

## Observation and controls

The main view prioritizes active Tasks, grouped running then queued, with bounded
pages (default up to 25 per state). It refreshes local authoritative HTML every
three seconds to discover new submissions. Detail views use SSE hints projected
from **Application.watch_task**, the Issue #35 observation contract. Councils reuse
TaskObserver's bounded multi-Task subscription. There is no new event store,
Provider polling, model text streaming or persistent browser session.

SSE refresh/resync hints replace the entire safe HTML snapshot. Reconnection
resynchronizes; terminal snapshots use durable history. Bounded queue overflow
and timeline truncation request resync. The latest 100 execution events are shown,
with explicit truncation. Event labels use safe step/tool-name/outcome metadata.
No raw model messages, reasoning, arguments, results, searched text, source bodies,
exception messages or private workspace paths are displayed in progress.

Each Task shows Project, Agent, Worker/model, state, elapsed waiting/execution time,
current status, observed tool attempts and step, where available. Elapsed time
counts from creation while waiting, then from start during execution; it freezes
at finish. The browser advances elapsed display between snapshots, without claiming
an ETA or completion percentage. Counts become unknown when live truncation would
make them incomplete; terminal execution summaries provide durable totals.

Browser cancellation uses the bundled HTMX script (JavaScript must be enabled)
and a CSRF-protected POST to normal AgentForge cancellation. A queued Task
can cancel immediately. Running cancellation is cooperative at execution boundaries;
remote inference may continue briefly. No force-kill claim is made.

Telemetry is normally available at terminal persistence. Model/tool counts, prompt,
completion and total tokens, explicit TTFT, generation duration, compatible observed
tokens/sec, total duration and partial observation coverage use unchanged telemetry
semantics. **Unknown is —**. Partial observed token sums are labelled separately
from complete totals; no totals/throughput are synthesized from missing data.
Worker identity/model is configuration, not a health claim. Opening the panel never
runs diagnostics/probes or recommends routing.

## Coding and Councils

Coding summaries use the Application cached coding-summary projection over existing
coding contracts, without reading a worktree or generating a diff: workspace state,
branch/base, changed files, bounded diff stat, writes/bytes, validation outcomes
and durations, and inspection availability. Validation stdout/stderr are excluded
from Companion projections. File/diff observations may lag until terminal persistence or another explicit
inspection; inspection eligibility reflects retained state and verifies current
ownership on demand. Main-page coding summaries collapse to keep the view
compact. **Inspect bounded current diff** explicitly calls the existing inspection
contract; the main page and Task detail do not load diffs. Diff truncation is labelled.
There is no commit, push, merge, PR, cleanup or filesystem browsing control.

Council views show Project/Agent, participant Workers/models/states, terminal state,
completed/failed/cancelled counts and participant Task links. Participants remain
independent. The Companion neither ranks answers nor selects/synthesizes a winner.
The home page lists the latest five Councils; any retained Council has a stable link.

Stable routes contain UUIDs, never private filesystem paths:

- `/companion`
- `/companion/tasks/<task_id>`
- `/companion/tasks/<task_id>/diff`
- `/companion/councils/<council_id>`

These links can be copied into a Codex conversation or opened manually. Future
supported native integrations can link to exact views without changing execution.

## Security and limitations

Combined mode accepts loopback IP binds only. Existing Host allowlisting, signed
CSRF cookie/token, same-origin POST checks, HTML escaping, no-store responses and
CSP apply to both surfaces. There is no CORS wildcard, general REST API, arbitrary
SQL/filesystem endpoint, environment view, Provider endpoint/secret view or automatic
Worker probe. Templates use typed Application/service projections; they do not
query repositories/SQLite, execute Git, or call Providers.

This is a trusted local operator surface, with no separate authentication platform.
It inherits the dashboard's trust in local users and processes. Explicit terminal
answers and requested diffs are bounded, escaped **untrusted content** and may
contain content intentionally produced by a Task or repository; the safe progress
projection is not a general content/secret classifier. Do not expose the HTTP port
publicly. Standalone dashboard bind behavior remains unchanged.

Limitations: no native Codex embedding, automatic browser opening, task composer,
live token measurements before persistence, replay, ETA, routing or Council judge.
The home page is a three-second snapshot view; Task/Council detail has live SSE.
No physical Windows/GPU/Ollama acceptance is claimed by the offline tests.

Validation includes offline scripted execution, real ASGI disconnects, coding
repositories and validators, plus real combined stdio/loopback startup/shutdown
via `scripts/verify_companion.py`. The clean installed-wheel smoke in
[development](development.md) runs that same combined-process check and checks
packaged Companion templates/assets alongside retained MCP/dashboard modes.
