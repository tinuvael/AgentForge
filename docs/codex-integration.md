# Global Codex integration (optional, per device)

Codex remains the Director. AgentForge provides execution, discovery and durable
Task observation. Configure each device once; repositories need no AgentForge
instructions. Global availability is **not** global authorization. The installer
never registers the current repository or edits a repository's `AGENTS.md`.

## Supported configuration and deployment

Verified against official documentation on 2026-10-24:

- [Codex configuration reference](https://developers.openai.com/codex/config-reference):
  user-global `config.toml`, stdio command/argv/cwd, `enabled`, `required`,
  `env_vars`, `disabled_tools`, and `default_tools_approval_mode`.
- [Codex MCP](https://developers.openai.com/codex/mcp): CLI and IDE extension share
  the host's configuration; `codex mcp list`, `codex mcp get`, and TUI `/mcp`.
- [Global instructions](https://developers.openai.com/codex/guides/agents-md):
  `$CODEX_HOME/AGENTS.md`, normally `~/.codex/AGENTS.md`, loads before repository
  instructions. A nonempty global `AGENTS.override.md` takes precedence.

Use a Codex build supporting these documented fields. This setup uses the supported
TOML mechanism directly; it does not set `developer_instructions`, experimental
remote placement, or invented settings. It never changes existing approval policy.
Generated tools use `writes` approvals; delegation/cancellation require supported
client approval because the server marks them as writes. Destructive workspace
cleanup is disabled in this integration. Do not relax client approvals for
unattended coding or paid use. Explicit `--coding` loads the existing coding
configuration; it does not authorize any coding Task by itself.

**Only one active Codex session per database: CLI OR IDE, not both.** Close other
sessions before starting a new one, even in another repository. Each session can
launch an independent stdio process. No other standalone MCP, web server, smoke
script, or client may execute against that database concurrently. Stop executors
before administrative writes. Separate databases permit independent deployments,
but Project registrations and Task histories are separate too.

Install and enable require `--single-client` to acknowledge this restriction;
setup also rejects recognizable duplicate database bindings in this global MCP
configuration, including relative-path aliases. This is a limited configuration
safeguard. Runtime [kernel ownership](executor-ownership.md) now rejects a second
executor on supported local SQLite storage before recovery (#44), including another
Codex stdio child. The first service remains active; stop it or select another DB.
Authenticated shared Streamable HTTP and the Windows-to-Mac
service are Issue #46. There is no HTTP transport option or new listener here.
Transport-specific generation is isolated so a future authenticated transport can
be added without changing instruction ownership or application composition.

## Prepare explicit local paths

Install Python 3.12+ and AgentForge in a dedicated venv. From a trusted checkout:
`python -m pip install -e '.[dev]'` using that venv's Python. For packaged deployment,
install the wheel in the venv. See [installation and migrations](development.md).
Prepare a trusted Worker TOML and explicitly migrate a private database before
setup. Configuration validation/import probes do not start a TaskEngine, recover
Tasks, probe Workers or make inference/API requests. Use absolute paths for all
inputs, including `--cwd`; there is no shell expansion inside generated argv.

`--installation` means the **directory containing the `agentforge` Python package**:
`<checkout>/src` for an editable installation, or the venv's `site-packages` for a
wheel. Determine it with the selected Python, independently of the current repo:

```sh
/path/to/venv/bin/python -I -c 'import pathlib, agentforge; print(pathlib.Path(agentforge.__file__).resolve().parent.parent)'
```

On PowerShell replace the executable with your venv's `Scripts\python.exe` and
invoke it with `&`. Setup verifies that the selected interpreter imports AgentForge
from this exact directory. It retains the venv interpreter path even if it is a
symlink. A missing/wrong interpreter, package, file, database schema or Worker
binding fails before configuration changes. Worker/coding paths reference trusted
operator files; no credentials are copied into generated TOML. Named Provider
credential **environment variable names** are forwarded with `env_vars`; provision
the values in the environment from which you launch Codex/your IDE. Restart after
secret rotation. Inline credentials in Worker options remain the operator's risk.

Windows PowerShell (replace every example path):

```powershell
$afPython = 'C:\AgentForge\.venv\Scripts\python.exe'
$afSetup = @('codex', 'install',
  '--python', $afPython,
  '--installation', 'C:\AgentForge\src',
  '--database', 'C:\AgentForgeState\codex.db',
  '--workers', 'C:\AgentForgeState\workers.toml',
  '--cwd', 'C:\AgentForge', '--single-client', '--instructions')
& $afPython -m agentforge @afSetup --dry-run
& $afPython -m agentforge @afSetup
& $afPython -m agentforge codex inspect
```

macOS terminal (replace every example path):

```sh
af_python='/Users/operator/AgentForge/.venv/bin/python'
"$af_python" -m agentforge codex install \
  --python "$af_python" \
  --installation '/Users/operator/AgentForge/src' \
  --database '/Users/operator/AgentForgeState/codex.db' \
  --workers '/Users/operator/AgentForgeState/workers.toml' \
  --cwd '/Users/operator/AgentForge' --single-client --instructions --dry-run
# Review the report, then repeat without --dry-run.
"$af_python" -m agentforge codex inspect
```

Use `--coding <absolute-file>` only for explicit coding configuration and review
[coding authorization and host requirements](coding.md). Omit `--instructions`
for MCP registration alone. `--codex-home <absolute-directory>` selects another
global profile; otherwise setup respects `CODEX_HOME`, then `~/.codex`. Choose a
private directory outside every Git repository. Keep the home and backups private;
on Windows protect the directory with the operator account's ACL.

## Exact file ownership, updates and removal

Setup owns only two clearly marked regions: `AgentForge managed MCP v1` in
`config.toml`, and optional `AgentForge delegation policy v1` in global `AGENTS.md`.
Text outside them, including comments, line endings, other MCP entries,
`developer_instructions`, profiles, repository trust settings and user instructions,
is preserved byte for byte. Anything placed **inside** a managed region is managed
and may be replaced. Do not place personal settings there.

- Installation appends one `[mcp_servers.agentforge]` table. Repeating the same
  command changes nothing and creates no additional backups. Repeat installation
  with new explicit paths to update the managed registration. Installation enables
  it; adding `--instructions` installs/updates the policy, while omitting that flag
  preserves any existing managed policy.
- Dry run performs offline validation/import checks and reports changed file paths;
  it writes no configuration or backups and never launches MCP. `inspect` reports
  the managed entry, policy presence/shadowing and `not_probed` server availability.
  Neither operation claims that a server is connected.
- Before changing any existing file, setup creates a unique sibling
  `<name>.agentforge-backup-<random-id>` containing the entire original bytes.
  Backups may contain user secrets. Retain/remove them manually as appropriate.
  Replacements are atomic per file, not transactional across the two files. If
  interrupted, rerun setup or restore the reported backups with executors stopped.
- Malformed TOML, duplicate/damaged markers, an unmanaged `agentforge` registration,
  or settings crossing managed boundaries cause an actionable refusal. No entry is
  silently adopted. Back up and explicitly rename/remove an unmanaged entry first.
  Inline/sealed `mcp_servers` tables that cannot be extended safely must be converted
  by the operator to ordinary tables. Setup does not attempt a lossy rewrite.
- A nonempty global override blocks `--instructions`; reconcile it explicitly or
  install MCP alone. Oversized global guidance reaching `project_doc_max_bytes`
  also blocks policy installation. Repository overrides and combined size limits
  can still affect instruction loading; verify them in a fresh session.
- Uninstall removes only marked regions and backs up changed files. It leaves
  empty files in place, retains unrelated files/backups, and never removes the
  venv, Worker/coding files, Projects, database, Tasks or coding workspaces.

PowerShell:

```powershell
& $afPython -m agentforge codex disable --dry-run
& $afPython -m agentforge codex disable
# Close the existing client; disable affects subsequent launches, not live processes.
& $afPython -m agentforge codex enable --single-client
& $afPython -m agentforge codex uninstall --dry-run
& $afPython -m agentforge codex uninstall
```

macOS uses `"$af_python" -m agentforge codex ...` with the same arguments. Both
platforms support `--codex-home` on every operation. Disable preserves instructions;
the policy's availability fallback remains applicable. Reenable requires confirming
that every previous executor has stopped. Never manually launch another stdio
server as a connectivity check while a Codex session owns the database.

## Client acceptance and representative safe test

1. With all executors stopped, run `codex mcp list` and
   `codex mcp get agentforge --json`. These inspect configuration without inference.
   Confirm command, argv, cwd, tool restrictions and enabled status.
2. Launch **one** fresh CLI session; `/mcp` should list AgentForge and its tools.
   In the IDE extension, use Settings / MCP servers and restart the extension
   after setup. Close the CLI before trying the IDE. Client versions and enterprise
   controls may limit these features; record the exact build and policies.
3. Ask `List the instruction sources loaded and summarize the optional AgentForge
   delegation policy.` Confirm the global file plus the repository's own guidance.
   Repeat sequentially in two unrelated repositories. Do not edit either repository.
4. Ask `Inspect AgentForge availability and capabilities, and list its registered
   Projects, Workers and Agents without submitting work.` Expect `agentforge_status`,
   `describe_capabilities`, and paginated `list_projects/workers/agents`. Metadata is
   not an authorization grant or a Worker health probe.
5. The operator must authorize an existing registered Project and a specific
   read-only Agent/Worker, its data destination and any cost. Register/index Projects
   separately with the [operator CLI](operator-cli.md), with executors stopped.
   Then request: `Independently inspect the entry point in authorized Project <UUID>
   using repo_explorer and authorized Worker <ID>. Cite source evidence and stop
   after that bounded analysis.` Approve the exact binding. Observe returned Task
   IDs using `watch_task`/`get_task`; verify cited files yourself before relying on
   conclusions. A Council requires separate explicit participant IDs/authorization.
6. Ask `What is 2 + 2?` Direct `4` is appropriate without an MCP call. Exercise a
   missing Project, denied cloud Worker and unavailable server; expect no delegation
   and an explanation/fallback where relevant, with no automatic Project creation,
   Worker substitution or blind retry. For a long Task, retain its ID, observe
   bounded progress and get the completed answer; cancellation is explicit.

Global instructions **cannot guarantee a tool call**. Do not mistake a prompt,
configuration parse, scripted client, or mock transcript for model-driven selection.
Real Windows/macOS CLI and IDE acceptance, approvals, instruction loading in two
repositories and natural-language selection remain device tests under #31/#46.
The Windows workstation was unavailable for this implementation; no on-device integration
acceptance is claimed. Each device needs initial local configuration; these files
do not connect a Mac to a Windows database/service.

## Troubleshooting and reproducible simulated evidence

For unavailable MCP: verify the interpreter/import root and paths with `inspect`,
check database migration status offline with executors stopped, check stderr and
client MCP status. Stale sessions need a restart. Check inherited credential
variables for named Providers. Do not create/migrate storage automatically or
retry a possibly committed Task. For missing Projects use explicit operator
registration after authorization; for invalid Workers use discovery and
`worker config-check`, checking Agent tool support. Never silently switch bindings.

Run `python -m pytest -ra tests/test_codex_integration.py tests/test_codex_delegation.py`.
All homes/storage are temporary. `test_codex_integration.py` covers installation,
complex TOML/instructions, backups, updates, uninstall, paths, conflicts, duplicate
DB bindings and an optional actual Codex CLI configuration parse.
`test_codex_delegation.py` uses the existing typed MCP adapter/application with a
mocked Provider and a **scripted Director**, producing these asserted interactions:

| Scripted case | Interaction and observation |
| --- | --- |
| Bounded consultation | status → capabilities → Project/Worker/Agent lists → delegate with explicit `repo_explorer`/`home-i5` and registered UUID → queued ID |
| Long Task | mocked Provider held running → snapshot watch and poll both running → release → completed answer; exactly the selected Worker ran |
| Missing ID/binding | missing Worker argument, unknown Worker and unregistered UUID produce failed typed tool results; no fallback |
| Trivial | direct answer, zero MCP calls/inference |
| Unavailable | stopped executor reports unavailable; a mocked disconnected MCP falls back without submission/inference |
| Unregistered repository | discovery does not contain the scripted open Project; no registration/submission |
| Denied cloud Worker | cloud exists in discovery but is outside explicit authorization; no submission/inference |

This demonstrates protocol and policy-compatible scripted cases, **not** actual
Codex reasoning or enforcement of Director-side data-egress authorization. Existing
MCP tests separately cover standard progress tokens, lifecycle and security checks.
The pre-existing POSIX Git/coding implementation uses Linux descriptor paths
(`/proc/self/fd`); successful macOS configuration tests do not establish native
macOS Git/coding runtime support. That runtime is not changed by this integration.

The installed-wheel smoke also installs/inspects/removes the integration in a
fresh temporary home and launches its generated stdio argv without inference.
