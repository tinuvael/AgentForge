# Development and release validation

Use Python 3.12+ and local Git. Install the package before testing so the `src/`
layout exercises installed imports:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -ra
ruff check .
ruff format --check .
git diff --check
python -m build
```

On native Windows, activate `.venv\Scripts\Activate.ps1`. The default suite uses
migrated temporary SQLite and synthetic repositories; an autouse fixture rejects
live sockets/DNS. Inference uses scripted Providers or httpx MockTransport. There
is no live Ollama/cloud/GPU dependency. Platform-independent Windows mocks run on
Linux; `windows` tests require actual native Windows and are skipped elsewhere.
POSIX descriptor/race tests are skipped on Windows.

## Migrations

Version **0.1.0** is the initial release candidate, pending native Windows,
Ollama and hardware acceptance. Its first supported schema is **`0001_initial`**.
This single revision creates the complete schema directly; the eight unreleased
development revisions were intentionally removed before any supported deployment.
Their databases are not an upgrade source. Preserve/export any development history
and uncommitted workspaces you need, then select a fresh database and register roots
explicitly. No command wipes, stamps or silently adopts an old database.

The current head remains `0001_initial`, including bounded operator diagnostic
checkpoints. Diagnostics were folded into this clean first-release baseline before
any supported deployment; internal development schemas are not an upgrade source.

Migrations live under `src/agentforge/db/migrations/` and ship in the wheel. Root
`alembic.ini` selects those same files. After this baseline is released, add new
linear revisions (`0002`, `0003`, ...) rather than rewriting `0001_initial`.

The schema separates registration/cache from durable execution history:

- `projects` requires one versioned, platform-tagged root identity JSON. No nullable
  POSIX/Windows transition columns or migration-time filesystem observations.
- `project_indexes`, `indexed_files`, `index_symbols` and `index_relationships`
  cascade with registration/file/source removal. Relationship targets are recomputed
  cache links, rather than authorization or foreign keys.
- `tasks` and `councils` retain historical Project IDs without Project FKs.
  Council membership references both parents without cascading deletion; Task
  membership and each Council's Worker/ordinal are unique.
- `worker_diagnostic_observations` keeps one safe checkpoint and last success/failure
  timestamps per Worker identity/probe kind, with no Task/Project FK or bodies.
  Writes update only the selected checkpoint. Unconfigured Worker rows may remain
  dormant; current configuration controls visibility, with no automatic pruning.
- `task_telemetry` is independently retained immutable terminal observation data.
  `coding_workspaces` independently retains private ownership for safe cleanup even
  after deregistration; neither is cascaded by deleting another record. There is no
  supported Task/history deletion API. Workspaces have unique IDs/branch bindings.
- State/coverage CHECK constraints reject unsupported lifecycle values. Unknown
  target snapshots, counts, timings and outcomes stay SQL NULL; queued telemetry
  coverage defaults to `pending`. Source/diff tool bodies are not automatically
  persisted; intentional requests, answers and validation captures may contain them.
- UUIDs use SQLAlchemy's portable `Uuid` (32 hexadecimal characters on SQLite).
  Datetimes are written in UTC and restored as UTC by repositories because SQLite
  drops timezone metadata. Runtime and Alembic CLI use the same SQLite transaction
  and foreign-key policy. Explicit indexes support actual lookup/history queries.

An explicit upgrade works from both an installed wheel and source:

```sh
python -m agentforge.db.migrate --database-url sqlite:///agentforge.db
```

Server startup requires the packaged migration head before recovery/execution;
it never migrates or stamps storage. Stop executors, back up private state and use the
same database URL when upgrading/restarting. Source-level Alembic maintenance:

```sh
alembic -c alembic.ini upgrade head
alembic -c alembic.ini check
```

For another URL, set `sqlalchemy.url` in a local config beside the root config, or
supply a Connection through `Config.attributes["connection"]` as tests do. Do not
casually modify released revisions. `tests/test_migrations.py` checks the single
initial revision, empty upgrades, idempotency with populated storage, and populated
head → base → head against ORM types, defaults, FKs, indexes and CHECK constraints.
Registry/index/Task/telemetry/Council tests additionally check identity, historical
retention, foreign keys and lifecycle behavior. Downgrades remove schema/data;
they are consistency checks, not a lossless rollback plan.
Never run `downgrade base` on a database whose history you need.

## Clean wheel validation

`python -m build` builds the sdist and then a wheel from that sdist. Check the wheel
in a fresh venv outside the checkout, with no editable install or source PYTHONPATH:

```sh
python -m venv ../agentforge-wheel-check
../agentforge-wheel-check/bin/python -m pip install dist/agentforge-0.1.0-py3-none-any.whl
cd ..
agentforge-wheel-check/bin/python AgentForge/scripts/verify_install.py
agentforge-wheel-check/bin/python -m pip check
```

On Windows use `agentforge-wheel-check\Scripts\python.exe` instead of `bin/python`.
Installation requires available dependency distributions, or an operator-prepared
wheelhouse with `pip install --no-index --find-links <wheelhouse> ...`. The smoke
itself is offline and creates only temporary synthetic storage. Git is not required
for this installation smoke. Coding-enabled discovery checks installed definitions
and CLI configuration listing only; they do not establish coding host readiness.
Actual coding host/runtime validation still requires a real Git executable and
the configured private workspace parent. The smoke checks:

- All production module imports resolve under the clean venv.
- Migration scripts/template and dashboard templates, CSS, JS, HTMX/license data.
- Console-script metadata and `--help` for the operator CLI and retained modules.
- Installed diagnostic CLI health/probe/read with offline synthetic Providers,
  checkpoint restart, fresh initial schema and the head → base → head roundtrip.
- Installed operator setup, Project lifecycle, read-only DB status, Worker/Agent
  config inspection, examples and confirmed deregistration.
- The installed migration command creates the initial schema and upgrades twice.
- Application/CLI/MCP discovery of the shipped Agents, scripted General Agent
  document comparison using actual read-only tools, bounded execution and telemetry.
- Installed MCP console script and module startup/discovery/status over real
  stdio, without inference.
- Installed `watch_task` input/output discovery and live standard MCP progress
  through the official in-memory SDK session with scripted offline inference.
- Real ASGI dashboard lifespan, pages/static resources and clean shutdown.

The wheel declares `agentforge = agentforge.cli:main`; `python -m agentforge`
is an equivalent operator entrypoint. Original migration, MCP and dashboard
module commands remain supported.
The dashboard's direct Starlette imports are declared explicitly; other runtime
dependencies are used by production modules. Build tooling belongs in the dev
extra. Jinja2 and HTMX assets are vendored package data, without a Node build.

## Acceptance and review

Review the complete branch diff, example commands and Markdown links before a PR.
Verify explicit Worker selection, Provider-independent runtime, root authorization,
primary-checkout safety and no automatic Git publication. Keep secrets, local
TOML/databases, logs, generated artifacts and machine-specific paths untracked.
Follow [AGENTS.md](../AGENTS.md) for branch/PR rules.

Real inference/hardware acceptance is opt-in and follows
[Repo Explorer smoke](manual-repo-explorer.md) and
[native Windows acceptance](native-windows-smoke.md). Do not run those live scripts
as part of the default suite or describe Linux mocks as physical Windows evidence.

Companion coverage includes packaged templates/CSS/JS and real combined MCP stdio
plus loopback HTTP operation and EOF shutdown. `scripts/verify_companion.py` is
called by the installed smoke, and can also be run explicitly with `--database-url`
and `--workers`. It uses no inference, internet or GPU. The normal suite invokes
that smoke in an isolated subprocess; its network use is limited to loopback.
