# Gateway transport and security decision (#46)

Reviewed 2026-10-25 against the installed MCP SDK 1.30.0 source, official transport
specification and current official Codex documentation. Keep `mcp>=1.30,<2`:
1.x remains a supported maintenance line; migrating to SDK 2.x is unnecessary here.

## Direct authenticated Streamable HTTP

Use the official `StreamableHTTPSessionManager` with the existing low-level Server
and typed tool contracts. Do not build JSON-RPC, SSE framing or separate HTTP
business handlers. The standard endpoint is `/mcp` (POST/GET/DELETE), using SDK
initialization, credential-bound sessions, cancellation notifications and request
progress. Independent Codex CLI/IDE clients support an HTTP `url` and
`bearer_token_env_var`. This permits operator-provisioned opaque bearer credentials
without an OAuth authorization server. This deployment does not advertise an
OAuth login/refresh flow or claim that static tokens implement the full MCP OAuth
authorization-server profile. OAuth-only hosted clients are not supported by this
credential mode. Tokens are never used as Provider credentials.

In SDK 1.30, `Server.run()` enters lifespan for **each session**. The HTTP adapter
therefore explicitly borrows an Application owned by the outer ASGI lifespan.
Only the Gateway starts/closes its TaskEngine. The SDK manager owns and bounds
sessions; an authenticated request's SDK `RequestContext.request` supplies current
credential identity. Do not read inherited session authentication ContextVars.
Each operation reloads private policy; watches revalidate while waiting and before
output. The SDK's authenticated session owner includes a credential fingerprint,
so a rotated credential requires a new session even when its stable client ID is
unchanged. Closing/deleting/expiring a session only releases transport/observers.

The Task's submitting client is committed with submission, including all Council
participants in their existing transaction. No post-commit in-memory ownership
map can leave an unowned/exposed Task after a crash. SQLite migration
`0002_gateway_client` retains old history with null ownership. SDK sessions are not
Task identity or persistence. Session loss/restart means reinitialize and retrieve
the durable ID; there is no SSE event replay or hidden submission replay.
An ambiguous failed submission can have committed: the operator can recover its ID
from Companion. Idempotency keys/automatic submission retries are not implemented.

Authorization lives in `application.access.ClientApplication`, a scoped projection
calling the same Application operations as stdio/web. It filters discovery before
paging; checks Project-to-Worker egress grants, Agents, tool, durable owner/shared
permissions and coding grants before access; and redacts coding captures when
workspace inspection is absent. This introduces no routing, inference adapter or
second scheduler. A scoped status cannot disclose other clients' private counts.
The trusted local operator/stdio path retains full access and must remain private.

Bounds cover SDK sessions/expiry, HTTP request size/deadlines/connections,
Task admission and progress handlers/delivery. Active Tasks use existing execution
limits. Default settings and failure semantics are in the [operator guide](gateway.md).
These are bounded service capacities, not per-client billing or fair scheduling.

## Concurrent policy publication

Policy readers use immutable file snapshots; native Windows opens with
FILE_SHARE_READ | FILE_SHARE_DELETE and denies in-place WRITE access. Identity is
rechecked after reading so a held old handle cannot authorize from stale policy
after replacement; at most four raced snapshots are retried before default-deny.
Modern native NTFS publication uses FileRenameInfoEx with REPLACE_IF_EXISTS and
POSIX_SEMANTICS, which permits replacement while old readers retain their handles.
MoveFileEx plus DELETE sharing alone does not provide this behavior. The database
owner's directory pins allow WRITE sharing for child-file publication while still
denying DELETE; DB/sidecar pins, identity checks and repository/coding handles
retain their protections. This permits policy beside an owned DB without relaxing
its replacement/ancestry boundary. The already
flushed temporary file is renamed and FlushFileBuffers confirms publication via
its write-through handle. Unsupported platforms fail closed; no in-place fallback.
This preserves live per-request/per-operation checks and stream revalidation.

Administrative writes use a permanent OS-locked sidecar plus expected-snapshot
comparison. A stale add/rotate/revoke or full replacement conflicts explicitly;
commands never re-enable a disabled identity during rotation. This is cooperative
local-operator serialization, not a distributed policy store. Direct edits must
use the same CAS API, not a concurrent external text editor. Readers never take
an administrative lock. Files are flushed before same-directory atomic publication
and the platform durability barrier; no plaintext bearer enters policy/logs.

An unpublished generated output is removed on failure. After publication, a
failed durability acknowledgement is reported separately and its recovery token is
retained. Rolling back could undo a revocation already seen by readers. There is
no claim of a transaction spanning credential output and policy through a crash.
See [operator recovery and exact bounds](gateway.md).

## Network and operator UI separation

Prefer loopback HTTP for Windows Codex and private HTTPS via Tailscale Serve to
the same loopback endpoint for MacBook Codex. Remote direct binds require TLS and
exact Host values. Explicit trusted TLS termination is allowed only for a loopback
upstream. Reject unlisted Origins, duplicate security headers and query strings;
ignore forwarded headers and disable Uvicorn proxy trust. No anonymous MCP calls,
credential-bearing URLs, public/unspecified listener defaults or trailing-path
redirects are allowed.

Companion borrows the same Application on a separate enforced-loopback port.
Dashboard routes and its unauthenticated operator controls are never mounted on
MCP ingress. Do not reverse-proxy that operator port remotely. The database kernel
lock still rejects separately launched web/stdio/Gateway executors before recovery.
The service is a single Uvicorn worker; reload/multi-worker hosting is unsupported.

## Optional OpenAI Secure MCP Tunnel

The official tunnel can make private MCP reachable by supported OpenAI-hosted
surfaces through an outbound connection. It has separate Platform permissions and
runtime credentials and transports MCP payloads through OpenAI. That changes the
data-egress boundary and needs an explicit operator decision. It is optional and
unnecessary for direct Windows/MacBook Codex CLI/IDE HTTP clients.

If added later, use the **HTTP upstream to the already-running Gateway**. A tunnel
configured with a stdio command could spawn a conflicting executor against SQLite.
Do not assume the tunnel alone forwards this deployment's per-client bearer
identity: verify upstream credentials and authorization for each hosted consumer.
No tunnel client, credentials, paid requests or Platform resources are provisioned
by Issue #46; direct private networking remains the supported primary path.

## References and validation boundary

- [MCP Streamable HTTP specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
- [Official Python SDK 1.x](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
  and [bounded-session security fix](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-84m7-p3x7-pcfv).
- [Codex MCP](https://developers.openai.com/codex/mcp/) and
  [CLI HTTP credential option](https://developers.openai.com/codex/cli/reference/).
- [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

`tests/test_gateway.py` runs `scripts/verify_gateway.py` in a separate interpreter
with real TCP listeners and independent official SDK clients. It replaces only
inference responses with scripted Ollama HTTP transport, retaining the real
Provider, Application, tools, engine, migrations and ownership. The installed-wheel
smoke additionally launches the real CLI against a loopback scripted HTTP Ollama
server, checks authentication/discovery/analysis and graceful restart. Hosted
Linux/Windows results are distinct from pending physical Windows/MacBook,
Tailscale, GPU/Ollama and Xcode acceptance. Native macOS execution remains #45.

Policy publication references: [FILE_RENAME_INFORMATION](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information),
[SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle)
and [FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers).
