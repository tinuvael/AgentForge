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

Migrations live under `src/agentforge/db/migrations/` and ship in the wheel. Root
`alembic.ini` selects those same files; historical revision contents are unchanged.
The linear chain is:

| Revision | Schema purpose |
| --- | --- |
| `0001_projects` | Registered roots. |
| `0002_project_index` | Python structure cache. |
| `0003_project_root_identity` | POSIX root identity; old rows remain unauthorized. |
| `0004_tasks` | Durable lifecycle and sanitized result history. |
| `0005_task_telemetry` | Target snapshots and terminal observations; no fabricated backfill. |
| `0006_windows_root_identity` | Tagged Windows identity; no live reauthorization. |
| `0007_councils` | Ordered independent Task membership. |
| `0008_coding_workspaces` | Private durable worktree ownership and bounded observations. |

An explicit upgrade works from both an installed wheel and source:

```sh
python -m agentforge.db.migrate --database-url sqlite:///agentforge.db
```

Server startup never migrates. Stop executors, back up private state and use the
same database URL when upgrading/restarting. Source-level Alembic maintenance:

```sh
alembic -c alembic.ini upgrade head
alembic -c alembic.ini check
```

For another URL, set `sqlalchemy.url` in a local config beside the root config, or
supply a Connection through `Config.attributes["connection"]` as tests do. Do not
casually modify released revisions. `tests/test_migrations.py` upgrades fresh
storage, checks one head and every intermediate downgrade/re-upgrade against ORM
metadata. Existing registry/index/Task/telemetry/Council tests additionally check
history, foreign keys and fail-closed legacy identity. Downgrades remove later
schema/data; they are development consistency checks, not a lossless rollback plan.
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
itself is offline and creates only temporary synthetic storage. It checks:

- Imports resolve under the clean venv, rather than source/editable paths.
- Migration scripts/template and dashboard templates, CSS, JS, HTMX/license data.
- `--help` for packaged migration, MCP and dashboard module entrypoints.
- Fresh/idempotent upgrades from packaged revisions.
- Actual SDK in-memory MCP discovery, scripted Task execution and telemetry.
- Real ASGI dashboard lifespan, pages/static resources and clean shutdown.

There are no console-script aliases; supported commands are `python -m` modules.
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
