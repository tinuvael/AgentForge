# Cloud-first CI acceptance (#47)

The default validation needs Python 3.12+, Git and installable Python dependencies.
It needs no GPU, Ollama installation, AI account, API key, owner workstation or
private repository. Hosted success is engineering evidence for **Issue #47**.
It does **not** complete [physical acceptance #31](https://github.com/tinuvael/AgentForge/issues/31).

## Reused infrastructure and evidence

The initial audit found no GitHub Actions workflows. The existing suite already
blocked live sockets/DNS, migrated disposable SQLite databases, used temporary Git
repositories, tested both HTTP adapters with MockTransport, and tested real SDK
MCP sessions, TaskEngine recovery, coding isolation and dashboard/Companion ASGI.
`scripts/verify_install.py` already covered packaged imports/assets, repeatable
migrations, CLI/MCP subprocesses, offline inference and combined Companion
HTTP/stdio/EOF shutdown through `scripts/verify_companion.py`. CI reuses these
checks; it does not introduce another executor or installation smoke.

| Validation | Local / Codex | Hosted Linux | Hosted Windows |
| --- | --- | --- | --- |
| Full pytest regression suite, including synthetic inference and integration | Yes | Python 3.12, 3.13, 3.14 | Python 3.12, 3.13, 3.14 |
| Ruff lint/format, whitespace, sdist → wheel build | Yes | All matrix entries | All matrix entries |
| Fresh external venv, wheel-only install, pip check, installed smoke | Yes | All matrix entries | All matrix entries |
| Migration initialization, populated upgrade repeatability and disposable downgrade/re-upgrade | Yes | Full suite + installed smoke | Full suite + installed smoke |
| Native Win32 NTFS, Git subprocess, SQLite and Windows coding regressions | Native Windows only | Explicit skips | Runs; symlink privilege skips reported |
| POSIX descriptors, symlinks and race regressions | Supported POSIX hosts | Runs | Explicit skips |
| Real remote inference | Explicit free-endpoint opt-in | Separate protected manual job | Not run |
| Physical Windows workstation, installed Ollama/model, RTX 4080 | Owner acceptance #31 | Not run | Not run |

Full-suite execution requires a supported Linux or Windows backend. On native
macOS, portable HTTP/MCP/storage checks and installed smoke can run, but the
existing Git/coding security backend rejects operations needing Linux descriptor
cwd/mount identity. The new coding acceptance cases explicitly skip there; older
Git/coding regressions can fail. Native macOS support belongs to #45 and is not
implemented or hidden by this CI change.

`.github/workflows/ci.yml` runs on PRs targeting main, main/task-branch pushes and
manual dispatch. Each job has a 20-minute timeout; matrix failures do not cancel
other OS/Python evidence. Read-only repository permissions, immutable action SHAs
and checkout without persisted credentials apply. No default job references a
secret, uses a self-hosted runner or starts a real inference service. PR code runs
under `pull_request`, never `pull_request_target`. No privileged follow-on workflow
consumes PR artifacts.

Every matrix entry uploads a 14-day `acceptance-<os>-py<version>` artifact containing:

- `pytest.xml`: exact test cases, failures and skip reasons, including platform
  and symlink-privilege skips. This is a status report, not a line-coverage claim.
- `environment.json`: actual OS, Python, SQLite, Git, runner image and tested SHA.
  PR runs record the tested merge SHA as well as the checkout SHA.
- `dependencies.txt`, `wheel-dependencies.txt`: exact resolved development and
  clean installed-wheel versions. Dependency ranges remain in pyproject.toml;
  there is no universal lockfile or claim that two dates resolve identically.
- `wheel.json`: written only after installed verification succeeds, with wheel
  SHA-256 and external-install status; absence means unverified.
- `summary.md`: regression totals, wheel status and explicit hardware exclusion,
  also rendered in the Actions summary. Individual step failures remain the
  authority for lint, formatting and build status.

Only the report directory is uploaded. Temporary databases, Git roots, worktrees,
worker configuration, source/tool bodies and credential environments are not
artifacts. Environment collection uses an allowlist, never an environment dump.
JUnit failure output can contain synthetic fixture data; do not replace fixtures
with private projects or real secrets. The existing socket/DNS guard remains on.
The installed Companion smoke uses only a temporary loopback listener in a child
process; it requires host permission to bind loopback.

The network guard allows only synchronous private `socketpair()` construction on
the calling thread: Windows asyncio needs its internally created loopback pair.
Ordinary loopback/service connects and DNS remain rejected, with explicit guard
regressions. Large context-budget parameters use short explicit pytest IDs so
`PYTEST_CURRENT_TEST` stays below Windows' environment-variable length limit.

Hosted Windows is prepared with the official PortableGit 2.51.2 x64 archive,
verified against its [published SHA-256](https://github.com/git-for-windows/git/releases/tag/v2.51.2.windows.1).
The bundled runner Git 2.56 layout lacks the `mingw64`/`mingw32` binary locations
currently recognized by AgentForge. CI pins supported tooling instead of changing
production executable authorization; newer-layout support needs separate follow-up.
Only this ephemeral runner's PATH changes. TEMP/TMP are canonicalized before
testing because the default `RUNNER~1` spelling is intentionally rejected by the
backend. Installed smoke similarly canonicalizes its own synthetic root. This
does not authorize alias/reparse paths from Projects or models. Version reports
are refreshed after preparation so they record the Git actually under test.
PortableGit's hardlinked `git.exe` is replaced with an independent byte-identical
copy; CI verifies its link count is one and its SHA-256 is unchanged. The existing
runtime refusal of hardlinked executables and Project files remains enforced.
Fixtures requiring exact source bytes write LF explicitly, and sensitive-cache
tests seed real stale database rows rather than depending on platform scanner
policy. Native replacement-race assertions accept either identity rejection or a
safe I/O error when NTFS sharing prevents the injected replacement itself.

## What synthetic providers demonstrate

The existing `test_ollama.py` and `test_openai_compatible.py` exercise real adapter
serialization/parsing using controlled httpx transports: text, native structured
calls, fragmented/malformed streams, bounded bodies, unavailable transports,
timeouts, 400/401/403, 429 with Retry-After, 5xx, cancellation/resource closure and
incomplete streams. Error bodies/authentication/private reasoning cannot escape
into public diagnostics. Retry-After does not cause a retry or a long sleep.
`test_generation_finish_reasons.py` covers output-limit Task failures without
executing truncated tool calls. None of these tests demonstrates model quality,
backend-specific compatibility, actual GPU behavior or remote cancellation.

`tests/synthetic.py` reuses existing response/body helpers for a deterministic
scripted endpoint. `test_cloud_acceptance.py` replaces only the HTTP transport
through the existing Provider factory seam. Both shipped adapters run through
configured Workers, real Application/TaskEngine, SDK MCP discovery/submission,
read_file and (for the opt-in coder) apply_patch tool rounds. Tests assert queued
submission, execution timestamps, terminal results/history/telemetry, fresh
Application retrieval, dashboard/Companion pages, retained coding diff and unchanged
primary files/index/HEAD. Blocked HTTP bodies establish running cancellation and
executor shutdown persistence. A persisted interrupted-task fixture verifies
restart failure without replay while queued work executes and cancelled work stays
cancelled. Coroutines have a 30-second outer deadline and Workers a 5-second call
deadline; no service port or external network is needed. Existing coding tests
add real validators, dirty-workspace crash recovery and authorized cleanup.

Tests exercise application boundaries but do not claim a single subprocess crash
or physical end-to-end hardware acceptance. MCP task tests use the SDK in-memory
transport; installed smoke separately validates actual stdio and loopback HTTP.
The known single-executor-per-database limitation remains unchanged; cross-process
locking belongs to #44. Diagnostics, routing, quotas and fallback policy remain
outside this issue.

## Reproducing a CI failure

Use the failed job's OS and Python minor, its tested SHA, and its version report.
Start a fresh venv and follow [development setup](development.md). Run:

```sh
python -m pip install -e '.[dev]'
python scripts/ci_report.py environment
python -m pytest -ra --junitxml=reports/pytest.xml
ruff check .
ruff format --check .
git diff --check
python -m build
python scripts/ci_verify_wheel.py
python scripts/ci_report.py summary
```

Activate `.venv\Scripts\Activate.ps1` on Windows; use `source .venv/bin/activate`
on POSIX. Remove stale **generated** dist files before rebuilding if more than one
wheel exists. The wheel helper creates and deletes its venv outside the checkout,
removes source PYTHONPATH/PYTHONHOME and checks the installed package with the
existing script. It intentionally requires fresh dependency installation; use an
operator-prepared wheelhouse through pip's normal configuration when offline.
For the exact dependency set, remove the editable AgentForge entry and its comment
from the saved freeze report, install the remaining versions with `pip install -r`,
then install this checkout with `pip install --no-deps -e .`. Repeat with
`wheel-dependencies.txt` for installed-smoke dependency reproduction.

Focused failures can be reproduced with
`python -m pytest -ra tests/test_cloud_acceptance.py` or the exact JUnit node ID.
Run `python -m pytest -ra -m windows` on Windows for native regressions. Linux/macOS
skip counts do not establish native Windows execution. A Codex sandbox may require
approval for package downloads, Git metadata writes or the loopback smoke; report
those limitations instead of weakening authorization or calling them passes.

## Optional remote free-endpoint smoke

`live_tests/` is outside default pytest testpaths. Even explicit collection skips
unless `AGENTFORGE_LIVE_SMOKE=1`. This separate test submits one fixed synthetic
prompt through a shipped Provider, without a Project, tools, source or database.
It caps output at 16 tokens, HTTP time at 15 seconds and overall generation at
20 seconds, with no retries; it prints no response, endpoint, config or token.
Client caps cannot guarantee a remote server stops work after disconnection.

The manual `live-provider-smoke.yml` is disabled unless the repository owner sets
`LIVE_PROVIDER_SMOKE_ENABLED=true` and explicitly confirms a **non-billable** endpoint
at dispatch. It runs only on `tinuvael/AgentForge` main, serially, with a five-minute
job timeout. It has no push/PR trigger and uploads no artifacts. Before enabling,
configure the `live-provider-smoke` environment for main only, required owner review
and prevention of self-review. Reuse owner-provisioned test credentials there:

- Environment variables: `AGENTFORGE_SMOKE_PROVIDER` (`ollama` or
  `openai_compatible`), `AGENTFORGE_SMOKE_MODEL`.
- Environment secrets: `AGENTFORGE_SMOKE_BASE_URL` (HTTPS), optional
  `AGENTFORGE_SMOKE_API_KEY` for compatible authentication.

Credentials are scoped only to the smoke step after installation, never PR jobs.
Do not configure a paid endpoint or production credentials. This change neither
provisions secrets/environment protections nor invokes remote inference. If no
free reachable endpoint exists, leave it disabled. Real tool-capable model and
hardware acceptance still follows [Issue #31's procedure](native-windows-smoke.md).

## Extending this infrastructure

New issues should add regressions to the existing pytest suite and reuse the
Provider contracts, transport fixtures, migrated database and disposable Git roots.
Use windows/posix markers only for actual platform requirements; keep skip reasons
specific. Extend the scripted endpoint for concrete scenarios rather than creating
another runtime or adding routing policy. Keep hardware/live tests separate and
explicitly opted in. Add new installed resources to verify_install.py and update
the evidence table when a validation boundary changes. Dependency/action/matrix
updates must retain actual version records and review the full hosted results.
