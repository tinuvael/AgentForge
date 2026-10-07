# Native Windows MCP / Repo Explorer acceptance

Run in Windows PowerShell with Python 3.12+, modern Windows 10/11, a fixed local
NTFS Project outside junction/OneDrive/reparse paths, a normal Git for Windows
installation with an absolute PATH entry (no installation junctions), and a
reachable Ollama with a native tool-capable model. No WSL is required.
Use one executor per database. TEMP and the installation must be trusted local NTFS.
These steps perform real inference and are never executed by pytest.

1. Install and migrate from the AgentForge checkout:

   ```powershell
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -e '.[dev]'
   .venv\Scripts\alembic.exe upgrade head
   .venv\Scripts\python.exe -m pytest -m windows -rs
   ```

   Record actual Windows version, filesystem, Git version, pass/skip counts and
   symlink privilege skips. Run the full pytest suite too. Do not count mocked tests
   executed on Linux as native validation. Use a fresh database from the
   [first-release baseline](development.md#migrations).

2. Copy `config/workers.example.toml` to ignored `workers.local.toml`. Configure
   `local-4080`, provider `ollama`, your installed model (for example `gpt-oss:20b`),
   endpoint `http://localhost:11434`, `supports_tools = true`, and factual deployment
   information. Verify Ollama/model availability yourself and warm up a cold model.

3. Register a real Project and refresh its Index through the operator CLI.
   Replace the path with the ordinary absolute path you intend to authorize;
   copy the returned `registration.project_id` for the Index command:

   ```powershell
   agentforge db upgrade --database-url sqlite:///agentforge.db
   agentforge project add 'C:\Projects\AgentForge' --name AgentForge --database-url sqlite:///agentforge.db
   agentforge project index <PROJECT_UUID> --database-url sqlite:///agentforge.db
   agentforge worker config-check --workers workers.local.toml
   agentforge worker check local-4080 --workers workers.local.toml
   ```

4. Configure your external Codex/MCP director as shown in [MCP setup](mcp.md),
   with the native `.venv\Scripts\python.exe`, `-m agentforge.mcp.server`, the same
   database URL and Worker file. For startup diagnostics you can also run:

   ```powershell
   .venv\Scripts\python.exe -m agentforge.mcp.server --database-url sqlite:///agentforge.db --workers workers.local.toml
   ```

5. Call `list_projects({})` and verify the registered UUID/root.
6. Call `list_workers({})` and verify `local-4080`; discovery does not probe health.
7. Call `delegate_task` with that Project UUID, `agent_id: "repo_explorer"`,
   `worker_id: "local-4080"` and a task requiring `read_file` and `search_code`,
   for example “Read src/agentforge/projects/service.py, search for ProjectRegistry,
   and explain root authorization with source path/line evidence.”
8. Observe successful source tool calls through the persisted sanitized trace.
   Workers receive bounded tool messages, not host filesystem capabilities.
9. Poll `get_task({"task_id":"<returned UUID>"})` until completed and inspect the
   final answer and execution counters. A model skipping source calls does not pass
   this acceptance check.
10. Inspect terminal telemetry via `TelemetryService`, or use the smoke client below
    to print it after server shutdown. Expect recorded coverage, actual selected
    provider/model and positive tool counts; unknown metrics remain null.
11. Verify an attempted `../escape.txt` read is rejected. The smoke preflight below
    performs that check using the actual central RepositoryTools. You can also ask
    the Agent to attempt it and inspect the safe invalid-arguments result; do not
    rely on the model alone to attempt an escape.

For a reproducible external SDK client run, stop other MCP/executor processes first.
The extended existing smoke utility starts **native AgentForge MCP**, performs
discovery, submits/polls a durable Task, then prints successful/error tool names and
telemetry after shutdown. `--verify-source` also performs central read/search and
escape checks and requires a successful model `read_file`/`search_code` result:

```powershell
.venv\Scripts\python.exe scripts/smoke_mcp.py --database-url sqlite:///agentforge.db --workers workers.local.toml --project-id '<UUID>' --worker-id local-4080 --verify-source src/agentforge/projects/service.py --task 'Read src/agentforge/projects/service.py and use search_code for ProjectRegistry. Explain root authorization with paths and lines.'
```

Exit zero means the Task completed and the optional source checks observed a model
source call. Inspect telemetry coverage and the answer yourself. Retain the Task
UUID, tool-call evidence, telemetry and native test results for acceptance.

To test home-i5 or another remote Ollama Worker, add its reachable endpoint/model
to the **central** Worker file and repeat with its explicit Worker ID. The Project
and all tools stay on Windows. No share, checkout or repository synchronization
belongs on the Worker. For compatible endpoints use named connections as described in
[Providers](providers.md); this acceptance procedure specifically exercises Ollama.

For restrictions and the Win32/Git threat model, see
[Windows repository security](windows-repository-security.md).
