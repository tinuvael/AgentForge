# Local Director architecture and security plan (#50)

Stage A adds a reusable bounded client, a private JSON recovery journal and CLI.
Stage B attaches a server-rendered chat to the existing dashboard. Both use the
same core and official MCP ClientSession over authenticated Streamable HTTP to
the persistent Gateway. Neither constructs an Application, TaskEngine or task
database. The Gateway remains the authority for grants, egress and task state.

The director reuses the shipped Ollama / OpenAI-compatible Provider protocols.
Inference configuration and credentials are operator-only. Initial director
inference must be explicitly trusted local: project material cannot accidentally
reach a cloud director outside the Gateway's worker egress checks. Workers remain
explicit selections, checked against authorized discovery and advisory candidates.

Each request has finite model, tool, task, context and wall-time budgets. Only
typed, allowlisted MCP operations are callable. Worker agents cannot delegate;
there is no nested director tool or scheduler. Journals retain bounded visible
messages, exact bindings/task IDs and recovery/approval checkpoints, excluding
opaque reasoning and protocol responses. Submission intent is flushed before
I/O; loss of a submission response becomes ambiguous and cannot be replayed.
Known IDs are reconciled by reads. A single process owns each private journal.

Coding submission, cancellation and cleanup require a precise expiring one-use
approval. Consumption is flushed before the action. Approvals are owned by the
conversation's CLI/browser identity and never supplied by model arguments.

Web chat is opt-in on the Gateway's loopback Companion only. Standalone LAN web
does not gain chat. It uses server-side credentials, independent browser cookies,
conversation ownership checks, CSRF, same-origin/Host checks, CSP and escaped plain
text. Bounded snapshot polling reuses HTMX and avoids unbounded subscriber queues;
refresh reads state and cannot submit. The director request survives tab closure;
dashboard shutdown closes only its clients, never the borrowed Gateway executor.
MacBook access uses authenticated SSH forwarding to the loopback listener. Direct
remote browser authentication is outside this issue and fails closed.

Stage A must pass scripted inference + real SDK/Gateway tests and installed-wheel
smoke before Stage B implementation. Stage B then tests real ASGI conversations,
authority, replay, reconnection and safe rendering. Existing nine-platform CI,
ownership/privacy regressions and physical acceptance in #31 remain required.
