# Offline installed-package operator setup. Run only against a new disposable Root.
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Git,
    [Parameter(Mandatory=$true)][string]$Root
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (Test-Path -LiteralPath $Root) { throw 'Acceptance Root must be new' }
New-Item -ItemType Directory -Path $Root | Out-Null
$Root = (Resolve-Path -LiteralPath $Root).Path
$afAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $Root /inheritance:r /grant:r "${afAccount}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Private ACL setup failed' }
foreach ($name in @('state', 'committed project', 'workspaces', 'artifacts', 'staging')) {
    New-Item -ItemType Directory -Path (Join-Path $Root $name) | Out-Null
}
$state = Join-Path $Root 'state'
$database = 'sqlite:///' + (Join-Path $state 'gateway.db').Replace('\', '/')
$workers = Join-Path $state 'workers.toml'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '../config/workers.example.toml') -Destination $workers

function Invoke-AgentForge([string[]]$AfArguments) {
    $output = & $Python -m agentforge @AfArguments
    if ($LASTEXITCODE -ne 0) { throw 'AgentForge operator command failed' }
    return ($output | ConvertFrom-Json)
}

$initial = & $Python -m agentforge db status --database-url $database
if ($LASTEXITCODE -ne 4 -or ($initial | ConvertFrom-Json).state -ne 'uninitialized') {
    throw 'Missing database status must be read-only'
}
if (Test-Path -LiteralPath (Join-Path $state 'gateway.db')) { throw 'Status created a database' }
Invoke-AgentForge -AfArguments @('db', 'upgrade', '--database-url', $database) | Out-Null
Invoke-AgentForge -AfArguments @('db', 'upgrade', '--database-url', $database) | Out-Null
if ((Invoke-AgentForge -AfArguments @('db', 'status', '--database-url', $database)).state -ne 'current') {
    throw 'Migration did not reach packaged head'
}
Invoke-AgentForge -AfArguments @('worker', 'config-check', '--workers', $workers) | Out-Null
Invoke-AgentForge -AfArguments @('worker', 'list', '--workers', $workers) | Out-Null
Invoke-AgentForge -AfArguments @('agent', 'list') | Out-Null

$project = Join-Path $Root 'committed project'
[IO.File]::WriteAllText((Join-Path $project 'source.py'), "value = 1`n", (New-Object Text.UTF8Encoding($false)))
foreach ($gitArguments in @(
    @('init', '-b', 'main'),
    @('config', 'user.name', 'Offline acceptance'),
    @('config', 'user.email', 'acceptance@example.invalid'),
    @('add', '.'), @('commit', '-m', 'Committed acceptance fixture')
)) {
    & $Git -C $project @gitArguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Disposable Git project setup failed' }
}
$registered = Invoke-AgentForge -AfArguments @('project', 'add', $project, '--name', 'PowerShell acceptance', '--database-url', $database)
$projectId = $registered.registration.project_id
Invoke-AgentForge -AfArguments @('project', 'index', $projectId, '--database-url', $database) | Out-Null
Invoke-AgentForge -AfArguments @('project', 'list', '--database-url', $database) | Out-Null

$auth = Join-Path $state 'clients.json'
foreach ($client in @('windows-codex', 'macbook-codex', 'local-director')) {
    Invoke-AgentForge -AfArguments @('gateway', 'client-add', '--auth', $auth, '--client-id', $client, '--token-file', (Join-Path $state "$client.token")) | Out-Null
}
Invoke-AgentForge -AfArguments @('gateway', 'auth-check', '--auth', $auth) | Out-Null
# Provisioning never silently grants execution or probes a configured endpoint.
$policies = Get-Content -LiteralPath $auth -Raw | ConvertFrom-Json
if ($policies.clients.Count -ne 3) { throw 'Expected distinct client identities' }
foreach ($client in $policies.clients) {
    if ($client.tools.Count -ne 0 -or $client.projects.Count -ne 0) { throw 'Unexpected default grants' }
}
$profile = Join-Path $Root 'disposable Codex profile'
$registration = @('codex', 'install', '--codex-home', $profile, '--url', 'http://127.0.0.1:8766/mcp', '--bearer-token-env-var', 'AGENTFORGE_ACCEPTANCE_TOKEN')
Invoke-AgentForge -AfArguments ($registration + @('--dry-run')) | Out-Null
Invoke-AgentForge -AfArguments $registration | Out-Null
Invoke-AgentForge -AfArguments @('codex', 'inspect', '--codex-home', $profile) | Out-Null
Invoke-AgentForge -AfArguments @('codex', 'uninstall', '--codex-home', $profile) | Out-Null
Write-Output 'Native PowerShell installed setup: absolute paths with spaces, SQLite, Git, discovery, private credentials and reversible HTTP registration: OK'
