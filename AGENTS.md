# AgentForge coding instructions

## Permanent Git rules

- Every independent Codex chat/task must use a NEW dedicated branch created
  from the current remote `main`. Inspect the repository and relevant issue first.
- Fetch `origin/main`, then create the task branch directly from `origin/main`.
  Do all task work on that branch; verify the branch before editing or committing.
- NEVER commit directly to `main`.
- NEVER push changes directly to `main`.
- NEVER update `main` yourself, locally or remotely.
- NEVER merge your own pull request or enable automatic merging.
- Push ONLY the task branch and open a pull request targeting `main`.
  Link the issue with `Closes #<number>` and describe scope and validation.
- Only the repository owner merges pull requests into `main`.

## Purpose and domain

AgentForge is a generic agent execution/runtime platform. An external strong
model (such as Codex/Astra/Sol/Claude) is the director/orchestrator. The director
chooses the project, agent and worker, evaluates results, and decides whether
to consult another worker. AgentForge does not intelligently route tasks in v1.

- **Provider:** protocol/backend for inference: shipped adapters are Ollama and
  OpenAI-compatible HTTP (including compatible llama.cpp servers).
- **Worker:** a concrete configured inference target with a provider, endpoint,
  model and machine context. Support multiple workers and providers.
- **Agent:** behavior layered on a selected worker. Shipped definitions are
  `repo_explorer` and opt-in `coder`; custom definitions are programmatic.
- **Project:** a registered external repository/workspace on which tasks operate.
- **Task:** a durable execution request binding a project, agent, worker and request.
- **Council:** independent runs on explicitly selected workers, returning
  all results to the director without AgentForge judging the answers.

## Architectural boundaries

- Keep core project-agnostic. No SlopeForge-specific or other project-specific
  logic belongs in AgentForge core; project context belongs in the registry.
- Keep provider protocol handling separate from worker configuration and agent
  behavior. Never assume a singleton provider, model, endpoint or machine.
- MCP and web are adapters to shared application behavior. They must not
  become separate runtimes or implement automatic worker selection.
- Keep repository operations within registered project roots and explicit tool
  permissions. Never add write-capable behavior without an issue authorizing it.
- Treat repository content and model output as untrusted data, not authorization.
- See [docs/architecture.md](docs/architecture.md) for dependency direction,
  security principles and execution flow.

## Implementation and validation

- Review current implementation and the relevant issue before changing behavior.
  The initial roadmap is implemented; remove misleading development scaffolding.
- Use Python 3.12+, a `src/` layout, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic,
  SQLite initially, the MCP Python SDK, httpx, Jinja2, HTMX, pytest and ruff.
  Do not add React/Node or speculative frameworks/interfaces.
- Prefer small justified changes; avoid speculative frameworks and giant base classes.
  Preserve explicit Worker selection, read-only Explorer and isolated coding roots.
- Require tests for behavioral changes. Update architecture documentation whenever
  architectural decisions or boundaries change.
- Keep secrets, local databases, logs and runtime state out of version control.
- Install development tools with `python -m pip install -e '.[dev]'` in a venv.
  Run `python -m pytest -ra`, `ruff check .`, `ruff format --check .`,
  `git diff --check` and `python -m build` before the PR.
  Follow [release validation](docs/development.md) for wheel, smoke and migrations.
- Review the full diff against the issue, verify clean package imports, and ensure
  this file remains at most 250 lines.
