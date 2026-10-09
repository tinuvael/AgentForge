# Pre-launch hardening validation (Issue #42)

Validation on 2026-10-09 used Linux x86_64, Python 3.12.14, SQLite 3.53.1 and
Git 2.52.0. The dedicated branch was created from freshly fetched `origin/main`
at **a9976bf5a720390b2586db86d66a91372ea08329**. This is offline release-candidate
evidence, not native Windows, real Ollama, RTX 4080 or gpt-oss:20b acceptance.
Physical acceptance remains [Issue #31](https://github.com/tinuvael/AgentForge/issues/31)
and must record its own exact installed versions and tested commit.

## Reproduction and corrections

- Real rollback-journal SQLite readers allow the Task write/flush but make COMMIT
  fail with `SQLITE_BUSY`. The regressions inspect SQLAlchemy's real DBAPI error
  and verify durable state through a separate sqlite3 connection. No commit mock,
  WAL, silent retry or external service is involved.
- Before correction, rejected Task submission, claim, queued/running cancellation,
  finish and recovery changes became durable after an unrelated successful write.
  A read-only pooled checkout could accidentally roll them back and mask the bug;
  the regression therefore deliberately performs a write as the next checkout.
- All seven explicit TaskRepository commit sites now use the same invalidating
  helper. The eight persistence regressions cover submission/Provider exclusion,
  lifecycle, telemetry atomicity, coding attachment, recovery, Council parents and
  memberships, later successful writes and returned connection ownership.
- Final PR review reproduced the same real COMMIT failure in ProjectRepository
  add/remove: a later successful write committed the rejected insert or cascaded
  deletion. Both commits now invalidate on failure without changing duplicate-root
  or IntegrityError classification. Two regressions independently inspect projects
  and all four index tables after the next successful write, with no checked-out
  connection leaks. Other database repositories use transaction contexts; no
  additional unprotected explicit commit sites were found or changed.
- A real terminal commit failure reproduced the active/queued watch hang. Three
  regressions cover Application, official MCP progress sessions and actual
  ASGI Task/Council SSE for both dashboard and Companion; a fourth covers executor
  loss during a failed claim before inference. Rows remain running/queued,
  safe `unavailable` closes watches, and subscriber counts return to zero.
- Ten tests use both shipped wire adapters through normalized results, AgentRuntime,
  TaskEngine and independently read durable state: stop, output-cap `length`, valid
  tools, truncated tools and intentionally supported/unsupported absent reasons.
  Truncated turns fail as `output_limit`; no answer, tool execution or retry follows.
- Windows lexical checks independently reject `.`/`./source` and accept ordinary
  drive-absolute paths. Documentation uses absolute Windows registration examples;
  filesystem/security code is unchanged. A LAN Host rendering regression reproduced
  Companion's false LOOPBACK claim and now verifies the neutral OPERATOR badge.

## Checks and installed artifact

| Check | Result |
| --- | --- |
| `python -m pytest -ra` | 1,326 passed, 20 skipped (native Windows) |
| Focused Task/Project SQLite, executor/watch, finish-reason, observer and Companion run | 65 passed |
| `ruff check .` | Passed |
| `ruff format --check .` | Passed |
| `git diff --check` | Passed |
| `python -m build` | sdist and wheel from sdist built |
| Full `scripts/verify_install.py` outside checkout in clean wheel venv | Passed |
| Clean installed `python -m pip check` | Passed |
| Real combined MCP stdio + Companion HTTP process smoke | Passed, including all six top-level HTTP routes, Host checks, clean stdout and EOF shutdown |
| Chromium/Playwright browser smoke against installed combined process | Passed at 420 and 1,280 px |

After the final ProjectRepository correction, the full installed verifier passed
again in a fresh wheel environment outside the checkout, including the real
combined MCP/Companion process smoke. Both new Project SQLITE_BUSY regressions
also passed against that installed wheel with independent durable-state reads.

The browser smoke checked the packaged routes/assets, neutral badge, completed
Task/Council views, escaped synthetic answer, absence of horizontal overflow,
expanded details preserved through HTMX refresh, CSRF rejection, concurrent MCP
status and EOF shutdown. It used Chromium **151.0.7922.173** and Playwright **1.62.0**,
with no Provider inference. Screenshots were inspected locally.

The execution-tool network-isolated sandbox stalled an existing threaded validation
subprocess test; rerunning with loopback/socket access completed. The repository's
autouse socket/DNS guard stayed enabled. The default suite still uses synthetic
repositories and scripted inference/MockTransport; only existing isolated smoke
subprocesses use real local sockets.

## Resolved versions

Runtime dependency ranges, including `mcp>=1.30,<2`, are unchanged. No constraints
mechanism or lockfile exists in the repository and none was added. SQLAlchemy
2.0.41 was selected in the validation environments to exercise the supported 2.0
line. These are observations of this run, not new runtime dependency pins.

Both source and clean-wheel validation used these exact runtime distributions:

```text
alembic==1.20.0
annotated-doc==0.0.5
annotated-types==0.8.0
anyio==4.15.1
attrs==26.1.0
certifi==2026.7.22
cffi==2.1.1
click==8.5.0
cryptography==50.0.2
fastapi==0.143.0
greenlet==3.5.6
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
httpx-sse==0.4.3
idna==3.20
Jinja2==3.1.6
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
Mako==1.4.3
MarkupSafe==3.0.4
mcp==1.30.0
opentelemetry-api==1.45.1
pycparser==3.1
pydantic==2.14.0
pydantic-settings==2.15.0
pydantic_core==2.50.0
PyJWT==2.15.1
python-dotenv==1.2.4
python-multipart==0.0.32
referencing==0.37.0
rpds-py==2026.9.1
SQLAlchemy==2.0.41
sse-starlette==3.5.0
starlette==1.7.0
typing-inspection==0.4.4
typing_extensions==4.16.0
uvicorn==0.54.0
```

The source dev environment additionally used build 1.6.1, iniconfig 2.3.1,
packaging 26.3, pluggy 1.6.0, Pygments 2.21.0, pyproject_hooks 1.3.3,
pytest 9.1.1 and ruff 0.16.10. Isolated builds used hatchling 1.32.4.
AgentForge was 0.1.0 (editable source for pytest; installed wheel for artifact smoke).

## Remaining operator constraints

Exactly one executor process per SQLite database is required, including Issue #31
acceptance. The guard is in-process only; another OS process can start and its
recovery can interrupt live Tasks or affect coding workspace state. Combined MCP
HTTP is loopback-only and intentionally exposes the full trusted dashboard and
Companion controls. Standalone web may be intentionally LAN-bound. Neither HTTP
mode has authentication. Physical Windows/Ollama/RTX 4080/gpt-oss:20b acceptance
has **not** been performed by this validation.
