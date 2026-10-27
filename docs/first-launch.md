# Fresh Windows first launch

Use Windows 11 x64, Python **3.13.x**, current Git for Windows and ordinary local
NTFS. The owner installs Windows/drivers/apps and the desired Ollama model as
specified in [physical acceptance #31](https://github.com/tinuvael/AgentForge/issues/31).
No CUDA Toolkit, compiler, OpenSSL development kit or model download is part of
AgentForge installation. Every path below is an **operator-selected example**.
Use private storage outside Git/OneDrive and quote paths, including paths with spaces.
Stop existing executors before registration, migration or other administrative writes.

## Build and install

Clone the reviewed revision to `C:\AgentForge\source` first. Record `git rev-parse
HEAD`; before this PR is merged, select its reviewed head explicitly. In PowerShell:

```powershell
$afSource = 'C:\AgentForge\source'
$afState = 'C:\AgentForgeState'
$afWorkspaces = 'C:\AgentForgeWorkspaces'
$afArtifacts = 'C:\AgentForgeArtifacts'
$afStaging = 'C:\AgentForgeStaging'
New-Item -ItemType Directory -Force 'C:\AgentForge' | Out-Null
py -3.13 -m venv --copies 'C:\AgentForge\dev-venv'
$afDev = 'C:\AgentForge\dev-venv\Scripts\python.exe'
& $afDev -m pip install --upgrade pip
& $afDev -m pip install --only-binary=:all: -e "${afSource}[dev]"
& $afDev -m build --outdir 'C:\AgentForge\dist' $afSource
py -3.13 -m venv --copies 'C:\AgentForge\venv'
$afPython = 'C:\AgentForge\venv\Scripts\python.exe'
& $afPython -m pip install --upgrade pip
& $afPython -m pip install --only-binary=:all: 'C:\AgentForge\dist\agentforge-0.1.0-py3-none-any.whl'
& $afPython -m pip check
```

Check each native command's exit code (`$LASTEXITCODE`) before continuing. Binary
resolution uses supported distributions within the declared ranges. It can select
an older compatible **wheel**, without a version pin; it must not silently compile
the newest transitive source package. Keep resolution errors for investigation.
To install the sdist directly, use the same binary dependency flag with its explicit
`.tar.gz` path. The isolated build backend also needs available wheels.

## Private state, configuration and committed Project

```powershell
$afAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
foreach ($afDirectory in @($afState, $afWorkspaces, $afArtifacts, $afStaging)) {
    New-Item -ItemType Directory -Force $afDirectory | Out-Null
    icacls $afDirectory /inheritance:r /grant:r "${afAccount}:(OI)(CI)F"
}
$afDb = 'sqlite:///' + (Join-Path $afState 'gateway.db').Replace('\', '/')
$afWorkers = Join-Path $afState 'workers.toml'
Copy-Item (Join-Path $afSource 'config/workers.example.toml') $afWorkers
# Edit workers.toml: exact installed model, endpoint and factual host metadata.
# The shipped local-4080/gpt-oss:20b is a configuration, not observed GPU readiness.
& $afPython -m agentforge worker config-check --workers $afWorkers
& $afPython -m agentforge worker list --workers $afWorkers
& $afPython -m agentforge agent list
& $afPython -m agentforge db upgrade --database-url $afDb
& $afPython -m agentforge db upgrade --database-url $afDb
& $afPython -m agentforge db status --database-url $afDb
```

Windows absolute SQLite spelling is `sqlite:///C:/AgentForgeState/gateway.db`;
POSIX absolute spelling has four slashes, e.g. `sqlite:////private/.../gateway.db`.
The database parent must exist. `db status` never creates missing state; startup
requires the packaged migration head `0005_workflows`, and never migrates implicitly.

Create a **new disposable** Git repository such as `C:\Projects\AgentForge Acceptance`,
with committed source. These identity values belong only to this synthetic repository:

```powershell
$afProject = 'C:\Projects\AgentForge Acceptance'
New-Item -ItemType Directory $afProject | Out-Null
[IO.File]::WriteAllText((Join-Path $afProject 'source.py'), "value = 1`n")
git -C $afProject init -b main
git -C $afProject config user.name 'Offline acceptance'
git -C $afProject config user.email 'acceptance@example.invalid'
git -C $afProject add source.py
git -C $afProject commit -m 'Committed acceptance fixture'
$afRegistration = & $afPython -m agentforge project add $afProject --name Acceptance --database-url $afDb | ConvertFrom-Json
$afProjectId = $afRegistration.registration.project_id
& $afPython -m agentforge project list --database-url $afDb
& $afPython -m agentforge project index $afProjectId --database-url $afDb
```

Use the ordinary absolute path, never `project add .` on Windows. Registry identity,
NTFS alias/reparse restrictions and committed coding roots apply outside the checkout.
Current Git uses `C:\Program Files\Git\ucrt64\bin\git.exe`; older installations may
use `mingw64`/`mingw32`. Read-only lookup handles the `cmd` PATH launcher, but coding
configuration must name the real absolute executable. Do not downgrade Git to make
an old path example work.

Copy `config/coding.example.toml` to private state and replace every executable/root.
Use the copied runtime venv's `python.exe` for a named deterministic validator such
as `argv = ['C:\AgentForge\venv\Scripts\python.exe', '-B', '-c', 'from pathlib import Path; assert Path("source.py").read_text().strip() == "value = 2"']`.
TOML literal single-quoted strings preserve Windows backslashes. This intentionally
fails until the isolated coder fixes the fixture. For real pytest/ruff, install
those tools in a separate trusted validator venv and use the cache-free shipped
arguments. Run `coding check --coding <absolute-file> --database-url $afDb` before
adding `--coding <absolute-file>` to Gateway. See [freshness limits](coding.md).

## Credentials and one Gateway

```powershell
$afAuth = Join-Path $afState 'clients.json'
foreach ($afClient in @('windows-codex', 'macbook-codex', 'local-director')) {
    & $afPython -m agentforge gateway client-add --auth $afAuth --client-id $afClient --token-file (Join-Path $afState "$afClient.token")
}
& $afPython -m agentforge gateway auth-check --auth $afAuth
```

New credentials have **no grants**. Apply the complete snapshot/compare-and-swap
procedure in [Gateway authorization](gateway.md#provision-separate-client-credentials-and-grants),
retaining generated hashes and identities. Grant this disposable Project UUID,
`local-4080`, `general_agent`/`repo_explorer` and the documented analysis/lifecycle
tools separately to each intended client. Coding additionally requires `coder`,
`coding_submit: true` and inspection grants. No token belongs in command arguments,
Git, logs or browser JavaScript. A malformed policy fails closed.

```powershell
& $afPython -m agentforge gateway serve --database-url $afDb --workers $afWorkers --auth $afAuth --companion
```

Keep this terminal running. Add only reviewed opt-in `--coding`, `--specialists` or
`--director-config` files to this **same** command. Open the local
`http://127.0.0.1:8765/companion`. Port 8766 exposes authenticated `/mcp`; it does
not expose the operator dashboard. A second executor refuses before recovery.

Connect Windows Codex with the reversible HTTP installer/dry-run in
[Gateway setup](gateway.md#connect-codex-on-windows-and-macbook). Mac Codex uses its
separate credential through private verified HTTPS/Tailscale. Neither client starts
an executor. Keep SQLite local; Windows reads its registered Windows files, including
committed coding bases, rather than uncommitted Mac files.

Discover IDs through authenticated MCP, submit a read-only Task, retain its ID,
watch/get its result, and explicitly cancel a separate Task. Disconnecting the client
only stops observation. Exercise an approved coder workflow with `required_validators`
and review its retained diff. Use the specialist example's **explicitly enabled fakes**
first for a GPU-free artifact chain; every real definition remains disabled until
reviewed. [Workflows](workflows.md) describes exact plans, grants, approvals and retry
identities. A model cannot approve privileged steps. Local Director's
[private configuration and CLI](local-director.md) connect to this same Gateway with
their separate credential/journal. Scope approvals remain on the operator workflow page.

Stop Gateway with Ctrl-C, restart the identical command and retrieve retained IDs.
Queued work may execute; interrupted running Tasks are not replayed. Unknown
ComfyUI submission effects require receipt reconciliation, never blind resubmission.

## Evidence boundary

`scripts/ci_verify_wheel.py` installs wheel and sdist outside source and executes
real MCP/Gateway/Companion/Director/workflow/process/persistence code with deterministic
local inference/executable fixtures. Native Windows jobs additionally execute
`verify_windows_setup.ps1`; `verify_workflows.py` executes the configured first-run
chain, named validator, Companion approval, CLI Director and restart. Neither test
downloads a model or invokes a paid endpoint. The [audit report](release-readiness-audit.md)
records results. Actual Ollama tool behavior, GPU/offload/memory, Windows browser,
Mac/Tailscale, real ComfyUI and Windows Blender GUI acceptance remain **unchecked** in #31.
