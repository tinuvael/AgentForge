# Operator setup and administration

`agentforge --help` is the entrypoint for a trusted local operator. Use
`agentforge <group> <command> --help` for arguments. `python -m agentforge` is
equivalent. The CLI ships in the wheel and needs no repository-relative files.
All configuration and database inputs are explicit; there is no config discovery
or automatic TOML editing. See the [README quick start](../README.md#install-and-quick-start).

Administrative commands print JSON to stdout. Errors use fixed safe diagnostics
on stderr without traceback, SQL, backend bodies or credential values. UUIDs,
timestamps and paths are strings. JSON escapes terminal controls. Configuration
listing display strings are capped at 256 characters; typed diagnostic snapshots
retain configured identities. Use ordinary short identifiers. No debug
mode dumps internal exceptions. Service commands retain their existing safe
logging and MCP stdout stays reserved for the protocol.

## Database

```sh
agentforge db upgrade --database-url sqlite:///agentforge.db
agentforge db status --database-url sqlite:///agentforge.db
```

Operator database commands support persistent local SQLite URLs without query
options. Relative database paths use the process working directory. Absolute
examples are `sqlite:////home/operator/private/agentforge.db` on POSIX and
`sqlite:///C:/AgentForge/private/agentforge.db` on Windows. Quote paths containing
spaces. Create the parent directory yourself and protect private runtime storage.

Status opens existing files read-only and never creates a missing database. It
reports `current`, `uninitialized`, `unsupported`, `out_of_date` or `inaccessible`.
`current` means the recorded revision matches the packaged head; this is not a
full schema/data integrity audit. The first supported baseline and current head are
`0001_initial`, including Worker diagnostic checkpoints. Old development revisions
remain `unsupported`. Empty storage is `uninitialized`;
unstamped storage with existing tables/views is `unsupported` and cannot be
adopted. Upgrade uses packaged Alembic migrations, does not wipe/stamp storage,
and rejects unknown revisions and nonempty unstamped databases. The original
`python -m agentforge.db.migrate --database-url ...` remains supported.

Stop executors and back up private state before upgrade. MCP/dashboard startup
continues to require the current schema and never runs migrations.

## Projects

```sh
agentforge project add /absolute/path/to/project --name Example --database-url sqlite:///agentforge.db
agentforge project list --database-url sqlite:///agentforge.db --limit 25 --offset 0
agentforge project inspect <PROJECT_UUID> --database-url sqlite:///agentforge.db
agentforge project index <PROJECT_UUID> --database-url sqlite:///agentforge.db
agentforge project remove <PROJECT_UUID> --database-url sqlite:///agentforge.db
```

Replace `<PROJECT_UUID>` with an actual UUID; angle brackets are placeholders.
Registration authorizes exactly the supplied existing root through ProjectRegistry's
canonicalization and platform identity checks. No repository discovery, recursive
registration or implicit indexing occurs. Add returns `registration.project_id`.
Duplicate roots fail; display names need not be unique. Ordinary relative paths
and paths with spaces work through the existing platform backend. Windows retains
its fixed local NTFS, reparse/alias and root identity restrictions.

List reads registration and cached Index checkpoint metadata, without accessing
Project source or Git. Its default is 25 rows, maximum 100; `next_offset` indicates
another page. `indexed_at` and `observed_head` are cached indexing observations.
Inspect separates stored `registration` from explicit, timestamped `live_git`
observations. Root identity failures fail safely; unavailable Git stays
`unavailable` and does not establish that the root is non-Git. Index explicitly
refreshes the existing Python cache and reports counts including parse failures;
a completed refresh can retain prior valid structure for failed files.

Remove requires typing `remove` at a terminal prompt. Without a terminal, it fails
unless the operator supplies the intentional `--yes` flag. No source files are
deleted. Option abbreviations (including `--y`) are rejected. **Registration and
Index cache are deleted; durable Task/Council history,
telemetry and coding workspace ownership intentionally survive.** This is not a
Task deletion or coding worktree cleanup command. Removal remains possible after
a root disappears or is replaced. Stop the executor before registration/removal
or Index refresh; these commands do not acquire a cross-process execution lease.

## Workers

```sh
agentforge worker config-check --workers workers.local.toml
agentforge worker list --workers workers.local.toml
agentforge worker check local-4080 --workers workers.local.toml
```

Use the existing [inline Ollama example](../config/workers.example.toml) or
[named Provider/mixed example](../config/workers.providers.example.toml). The same
TOML loader and Provider factory binding validation used in Application composition
check references, supported adapters and required authentication environment.
Config-check/list make no network requests and do not establish availability.
Endpoints, authentication variables/values and generation options are omitted.
Listing capabilities repeats operator configuration, not measured compatibility.

Check selects exactly one configured Worker and invokes the existing health
contract with at most five seconds for the operation. Ollama requests only its
bounded model list; an absent model is unavailable. OpenAI-compatible health
returns `not_probed` without network activity, with availability fields unknown.
Exit zero for `not_probed` means the operation completed, not that inference works.
Health failure codes are allowlisted; arbitrary Provider diagnostics are omitted.
Owned clients are closed. Add `--database-url` to retain the health observation.
`worker probe <id> --kind generation|tools|streaming --workers FILE --database-url URL`
explicitly performs one small fixed synthetic inference and persists its factual
observation. `worker diagnostics <id>` or `worker diagnostics --all` reads current
configuration and separate persisted observations without Provider calls; both
require `--workers` and `--database-url`. See [Worker diagnostics](worker-diagnostics.md)
for fixed requests, unknown metrics, safe failures, remote data egress, bounds,
retention and concurrency. No ranking, task routing or fallback occurs.

## Coding and Agents

```sh
agentforge agent list
agentforge coding show --coding coding.local.toml
agentforge coding check --coding coding.local.toml --database-url sqlite:///agentforge.db
agentforge agent list --coding coding.local.toml
```

Agent listing uses the actual shipped definitions shared with Application
composition, including allowed tools, limits and workspace mode. System prompts
are omitted. `--coding` loads the explicit coding TOML to select the opt-in coder;
it does not imply that host checks passed. Custom programmatic Agents continue to
be supplied by embedding applications, outside this CLI.

Show validates TOML and displays paths, effective limits, validator names,
argument counts and timeouts. Validator argv is omitted because it can contain
private values. Check additionally requires a current database, uses the existing
CodingWorkspaceManager/private-parent checks and no-follow filesystem backend,
checks configured executable access/ancestry and overlap with registered roots.
It runs no Git/validators, creates no workspace and writes no configuration.
POSIX requires the existing owned 0700 workspace parent; Windows restrictions
remain the existing backend contract. Create a private user-owned ACL on Windows.
Checks do not certify validator safety, Git version, ancestor repository layout,
committed HEAD or future Task readiness. Task-time trust checks remain authoritative.
See [coding configuration and trust](coding.md#enable-and-configure).

## Services and ownership

```sh
agentforge mcp --database-url sqlite:///agentforge.db --workers workers.local.toml
agentforge web --database-url sqlite:///agentforge.db --workers workers.local.toml
```

Choose one service. Stop MCP before starting the dashboard against the same
database. Each owns the existing TaskEngine; there is no combined launcher,
daemon or cross-process lease. `--coding` and `--concurrency` retain their existing
meaning. Web additionally accepts the existing host/port/allowed-host options.
The original `python -m agentforge.mcp.server` and
`python -m agentforge.web.server` commands remain supported. Use absolute
interpreter/config/database paths for MCP client settings; see [MCP setup](mcp.md).

Project administration is exclusively an operator surface. No new MCP tool or
model filesystem authorization capability is introduced. Dashboard behavior and
its existing CSRF/confirmation controls are unchanged; it has no authentication.
Workers have no direct filesystem access. Explorer stays read-only and coder
stays isolated-write. External orchestration and explicit Worker selection remain.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Completed; inspect factual status (`not_probed` is not availability) |
| 1 | Unexpected operation/service failure or interruption |
| 2 | Invalid arguments/configuration or missing removal confirmation |
| 3 | Storage/file access unavailable |
| 4 | Uninitialized, out-of-date or unsupported schema |
| 5 | Project missing/already registered or Worker ID missing |
| 6 | Unsafe, missing or replaced Project root |
| 7 | Explicit Worker health/probe failed or model absent |

MCP/web preserve their original service exit behavior (startup failure 1).
No live Provider, cloud, GPU or native Windows acceptance is implied by offline
CLI tests. Director-facing progress events (#35), Companion (#32) and a general
Agent (#36) remain separate work. Shared service ownership
and a combined daemon also remain outside this feature.
