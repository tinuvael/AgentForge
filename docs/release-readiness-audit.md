# Release readiness audit — 2026-10-27

Scope: [#72](https://github.com/tinuvael/AgentForge/issues/72), after merged
[#71](https://github.com/tinuvael/AgentForge/pull/71). Physical acceptance remains
**OPEN** in [#31](https://github.com/tinuvael/AgentForge/issues/31); no physical row
was completed. This is fixture/application compatibility evidence, not a promise
that the owner's first GPU launch will succeed.

Starting SHA: `01314210f8875610711805850d9c5d8e85708beb`. Dedicated branch:
`audit/release-readiness-20261027`, created directly from fetched `origin/main`.
The release revision is the PR head containing this report: resolve it with
`git rev-parse HEAD`; its exact final SHA and final-head CI links are recorded in
the PR description. A report cannot embed its own containing commit's hash.
Existing unrelated local `reports/` were preserved. No main update, merge or
automatic merge was performed.

## Environment and baseline

Local: Intel x86_64 macOS 26.6.2 / Darwin 25.6.0, local APFS, CPython 3.13.2,
SQLite 3.45.3, Apple Git 2.50.1. A fresh external venv installed development tools
with binary dependencies and **no version constraint**. Exact resolved versions:

```text
agentforge 0.1.0; pip 26.2.2; alembic 1.20.1; anyio 4.15.1;
fastapi 0.162.0; starlette 1.10.0; pydantic 2.14.1; pydantic_core 2.50.1;
SQLAlchemy 2.1.6; mcp 1.30.0; httpx 0.28.1; httpcore 1.0.9;
Jinja2 3.1.6; uvicorn 0.54.0; cryptography 48.0.1; cffi 2.1.1;
PyJWT 2.15.1; pydantic-settings 2.16.1; sse-starlette 3.6.0;
pytest 9.1.1; ruff 0.17.3; build 1.6.1; hatchling 1.32.4 (isolated build).
```

Full starting-SHA suite, from a source archive with no `.git` dependency:
**1,741 passed / 29 skipped / 1 failed**, 774.62 s. The failure was
`test_git_inspection_is_read_only_and_bounded`, reproduced alone (1 failed).
Its macOS test shim removed the descriptor launcher before executing Git, so it
inspected cwd; it only appeared correct when cwd happened to be a Git repository.
The corrected test preserves argv and asserts the actual fixture root (1 passed
from outside any Git checkout). No production fallback was introduced.

An earlier sandbox-limited run was stopped after **554 passed / 10 skipped /
3 failed** in 502.23 s: all three failures were loopback bind denial in the
Companion/Director helpers. Its interrupted teardown also raised a pytest stash
error. These are recorded as execution-environment failures, not release passes.
The full immutable baseline above used the necessary loopback/process permissions.
The existing deliberately invalid SQLite-URI test emits one SQLAlchemy warning.

Available locally: native macOS/APFS, Git, Python, Codex configuration parser,
loopback HTTP/TLS and subprocesses. No Windows OS, RTX 4080, NVIDIA driver,
Ollama model, real ComfyUI service or owner's Mac/Windows client connection was
available. Official Blender **4.5.9 LTS**, build `8bf95cbd38d1`, was temporarily
downloaded, checksum verified and run headlessly on CPU. Native Windows/Linux
and the other Python versions are exercised by hosted CI, separately from #31.

## Findings and corrections

| Impact | Baseline trigger / consequence | Root and correction | Regression evidence |
| --- | --- | --- | --- |
| Startup/functionality | Current Windows Git installed under `ucrt64`; read-only Git tools fail as unavailable. Old CI hid this with PortableGit 2.51.2. | `tools.windows_git` omitted the new binary location. Add it alongside existing layouts, retaining pinned no-follow ancestry and direct binary execution. CI uses current runner Git. | New cmd/bin/direct × three-layout cases: **3 failed / 6 passed before**, all nine pass after; actual Windows jobs exercise current Git. |
| Install | Ordinary fresh pip selects `cryptography 50.0.2` sdist on Intel Mac and fails building OpenSSL bindings. | Transitive distribution selection, not a missing AgentForge dependency. Document/enforce supported binary resolution; no arbitrary cryptography pin, constraint or compiler installation. | Uncached ordinary install fails; fresh binary-only install and pip check pass. Both wheel and directly named sdist now install in separate external environments on every CI platform. |
| Lost recovery | CLI/browser Clear removes the only Director journal binding for approval/paused/recovery-required workflows. CLI JSON omits workflow IDs. | Task-only checks missed workflows. Shared core Clear guard retains all unfinished workflow identities; CLI includes bindings. Resume refreshes stale cached outcomes. | **5 failed before / 5 passed after** across real browser routes, CLI clear and CLI output; terminal clear remains allowed. |
| Advertised validation | Default pytest creates bytecode/cache; successful first invocation cannot certify unchanged final inputs. | Strict snapshot correctly detects generated files. Cache-free pytest/ruff examples keep the boundary intact; document coverage/build/Xcode output handling. | Real copied-venv pytest with the shipped argv: exit 0 but certification fails before; first-run certification and no caches pass after. Source/private/ignored changes still invalidate evidence. |
| Advertised deployment | Mac-local example leaves private Windows inference trust unknown; default policy refuses Tasks. | Explicit operator trust declaration was missing. Add `trust="local"` to that named connection. | Baseline parsed as unknown; actual documented TOML now passes default Project admission with no network call. |
| Verification | Git inspection test changes the production command and depends on cwd being a Git checkout. | Preserve original descriptor command and assert exact root, maintaining every existing boundary assertion. | Baseline source archive fails, corrected external-cwd test passes. |

Stale operator claims about no HTTP support, in-process-only ownership and missing
Mac execution support were corrected or marked historical. No Provider, scheduler,
deployment architecture, source freshness exclusion or automatic retry was added.

## Subsystem coverage map

“Suite” below means exercised production behavior through the named tests; a mock
transport alone is not presented as device evidence. Existing tests were retained.

| Subsystem / public entrypoint | Inspected and exercised evidence | Remaining boundary |
| --- | --- | --- |
| Projects / `project add/list/index/inspect`, repository tools | Registry identity → native filesystem → AST index/Git; `test_projects*`, `test_index`, `test_repository_tools`, native Windows/APFS suites; committed disposable Project via CLI | Owner paths/ACLs/filesystem and actual projects |
| Agents / discovery, `delegate_task` | Shipped definitions, runtime schema/tool/context/output limits, General/Explorer/Coder; `test_agent_runtime`, `test_general_agent`, finish reasons, MCP/cloud acceptance | Real model tool quality/context/offload |
| Providers/Workers / config, health/probes, suggestions | Both shipped HTTP protocols, bounded JSON/SSE, authentication, errors/429/budgets, diagnostic persistence; `test_ollama`, `test_openai_compatible`, `test_provider_http`, diagnostics/policy suites | Actual installed models, backend/API compatibility and performance |
| Tasks/Councils/telemetry / submit/get/watch/cancel | Durable lifecycle, atomic cancellation/completion, independent participants, unknown metrics, disconnects and unavailable observers; task/Council/observation/telemetry suites and actual SDK clients | Long-running real inference, workstation sleep/power loss |
| Coding / workspace/diff/validation/cleanup | Git isolation, primary invariants, named validator process trees, latest failed/unfinished invocation, edits/reverts/private/out-of-band changes, final workflow recheck; coding and workflow-validation suites | Actual trusted project validators/Xcode; output-changing checks need reviewed output locations |
| SQLite / `db upgrade/status`, executor lifespans | Packaged five-revision chain, head/base/head, populated upgrades, commit failure invalidation, independent process owners, force-kill/restart and aliases; migrations/commit/ownership suites | Owner backup/recovery practice; SQLite remains local, never SMB |
| Gateway / authenticated `/mcp` | Actual TCP/TLS/official SDK, scoped separate identities, ownership, policy CAS, revocation/watch rechecks, egress denial; gateway/access/policy suites | Real tailnet/TLS/firewall/credential transfer |
| Global Codex / install/dry-run/inspect/remove | Temporary profiles, backups, preserved unrelated config, duplicate binding checks, stdio and HTTP registration; integration/delegation suites and native PowerShell setup | Fresh real CLI/IDE model sessions and instruction loading on each device |
| Director / CLI, journal, `/director` | Reservation before I/O, lost response/crash/resume accounting and legacy journals; one-use approval, stale/multi-tab/session/CSRF controls; Director/web suites and actual CLI over authenticated MCP | Local model planning quality; real Windows/Mac browsers |
| Specialists/artifacts / ToolJobs, inspect/retrieve | Provenance, client/producer identity, expiry/corruption/quotas, atomic publication, interrupted process cleanup, ComfyUI receipt reconciliation and local-only cancellation; specialist suites | Real ComfyUI GPU service/PNG and Windows Blender/GUI exchange |
| Blender / packaged script and adapter | Actual 4.5.9 CPU invocation → normal eight-vertex/36-index export → strict format validation → private publication → restart. Captured exporter profile replaces fake-only adapter acceptance | Windows binary/plugins/driver/GUI. `reference_blockout` is a cuboid, not reconstruction or assessed printability |
| Workflows / submit/get/pause/resume/cancel, operator review | Key conflicts, deterministic child intent/linkage, process deaths, receipts, dependencies, reviews, retries, shared concurrency one, queue-inclusive UTC/monotonic deadlines; workflow suites | Physical long-running applications, owner-approved resource switching |
| Dashboard/Companion / routes, forms, SSE | Actual ASGI/HTTP, escaped bounded long text, correct unknown/offline states, CSRF/Host/Origin, stale approval rejection, refresh/reconnect; web/Companion/Director suites | Owner browsers, touch/keyboard and device rendering |
| Packaging / CLI/modules, resources and examples | sdist → wheel; fresh independent installs, all imports, console scripts, migrations, templates/static/Blender data, sanitized examples, pip consistency; installed helpers copied outside checkout and developer PATH removed | New future Python/platform distributions beyond tested 3.12–3.14 |

High-risk crash boundaries are exercised by existing process-death/transaction
fault tests, not inferred from happy-path unit coverage. Workflows/ToolJobs reuse
idempotency identities after lost responses. Ordinary Task submission has no
idempotency key: an ambiguous response requires observation/operator reconciliation,
never a blind replay. Approval cannot be supplied by model tools. A client/watch
disconnect does not cancel accepted work; local ComfyUI cancellation does not prove
remote termination. Error-path tests retain a usable service or report it unavailable.

## Reproducible release evidence

- `python -m pytest -ra --tb=short --junitxml=...`: complete local result recorded in
  the PR; exact platform skips remain in JUnit. Full hosted regression and installed
  acceptance run on Ubuntu 24.04, Windows 2025 and macOS 15 × Python 3.12/3.13/3.14.
- `ruff check .`, `ruff format --check .`, `git diff --check` and committed
  `scripts/ci_check_whitespace.py --base 01314210f8875610711805850d9c5d8e85708beb`
  check the actual PR range, not merely a clean worktree.
- `python -m build` builds sdist and wheel from it. `ci_verify_wheel.py` installs
  each independently with uncached binary dependencies; helpers/config examples,
  cwd and package imports live outside source. Every job uploads exact environment,
  dependency freeze, pytest/skip evidence and separate wheel/sdist results.
- `verify_workflows.py` follows CLI migration/registration/configuration → one
  Gateway + Companion → authenticated discovery/Task/get/watch/cancel → artifact
  workflow → Explorer/Coder/required validator and real CSRF scope review → actual
  Local Director CLI/native inference HTTP → retained results after restart, with
  concurrency one. Primary Git/source bytes stay unchanged. A repeated approval
  returns 409. Fake outputs stay explicitly simulated.
- `verify_specialist_adapters.py --blender <4.5.9 executable>` passed the production
  adapter/process/artifact/restart path on CPU with a fixture ComfyUI transport.
  [Captured profile and checksum](../scripts/fixtures/README.md) permit reproduction;
  default CI uses executable fixtures and claims no real Blender invocation.

Follow the checked [fresh-Windows sequence](first-launch.md), then #31's physical
acceptance. Final-head check results and immutable hosted artifacts are linked from
the review PR; hosted Windows is never counted as the owner's workstation acceptance.

## Deployment conclusions and remaining acceptance

| Scenario | Verified software boundary | Still requires owner |
| --- | --- | --- |
| A. Windows Codex → Windows Gateway → local Ollama | One executor; authenticated client, current Windows Git, installed-package/config/HTTP fixtures | Windows Codex tool choice, actual Ollama tools, NVIDIA driver/VRAM/offload |
| B. Mac Codex → same Windows Gateway | Independent scoped client sessions/TLS, shared durable results, no client executor | Real Mac/tailnet/HTTPS/secret transfer and CLI/IDE |
| C. Local Director → existing Gateway | Actual CLI + native scripted inference + authenticated MCP/workflows, separate journal | Actual local model quality, Windows CLI/browser behavior |
| D. Optional Mac-local → Windows inference | APFS/descriptor/isolated coding tests, explicit private-host trust; separate DB/IDs/history | Physical MacBook project, Windows inference networking, Swift/Xcode/simulator/device |
| E. Application host specialists/workflows | Fake artifact chain; real adapter contracts and actual macOS Blender CPU export | Real ComfyUI/SDXL generation, Windows Blender import/export/GUI, memory switching |

No network SQLite, cross-host uncommitted-file access, remote execution implied by
Worker location or GPU guarantees are supported. Real model downloads, paid calls,
GPU use, device destruction and physical acceptance were not performed. Keep every
applicable W/L/D/C/U/R/S/F acceptance row in #31 open until measured on those devices.
