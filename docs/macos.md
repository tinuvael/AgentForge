# Native Mac-local AgentForge

This optional execution mode reads the Mac's registered checkout, including dirty
Swift sources, and uses explicitly selected inference Workers. The separate
[Windows Gateway](gateway.md) reads Windows-hosted checkouts. The two instances
have separate databases, Project IDs, Tasks, histories and workspace ownership.
They never share SQLite, synchronize Git or federate Tasks. Configure distinct MCP
names, `agentforge_mac` and `agentforge_windows`, and explicitly select the intended
instance before discovering its IDs. An uncommitted Mac edit is visible only locally.

Hosted macOS CI is code-level acceptance. **Physical MacBook, FieldForge, Xcode,
simulator/device and Windows RTX 4080/Ollama/Tailscale acceptance remains pending
in [#31](https://github.com/tinuvael/AgentForge/issues/31).** No local model is needed.

## Install and register a checkout

Use Python 3.12–3.14, installed Git and local APFS. Build the wheel following
[development validation](development.md), then install the artifact in private
storage outside every registered repository. Substitute your wheel and FieldForge
paths; these examples authorize no real user repository:

```sh
AF_STATE="$HOME/Library/AgentForge"
mkdir -p "$AF_STATE" "$AF_STATE/worktrees"
chmod 700 "$AF_STATE" "$AF_STATE/worktrees"
python3.12 -m venv --copies "$AF_STATE/venv"
"$AF_STATE/venv/bin/python" -m pip install --only-binary=:all: /absolute/path/agentforge-0.1.0-py3-none-any.whl
AF_CLI="$AF_STATE/venv/bin/agentforge"
AF_DB="sqlite:///$AF_STATE/mac.sqlite"
"$AF_CLI" db upgrade --database-url "$AF_DB"
"$AF_CLI" project add /absolute/path/FieldForge --name FieldForge --database-url "$AF_DB"
"$AF_CLI" project list --database-url "$AF_DB"
"$AF_CLI" project index PROJECT_UUID --database-url "$AF_DB"
```

Save the actual UUID. `project index` maintains the existing Python structural
index; Swift remains ordinary UTF-8 text for list/read/search and Git inspection.
Use exact on-disk path spelling and canonical paths, including `/private/tmp`
instead of a symlinked temporary root. Do not register your whole home directory.
Database parent, configuration, installation and worktrees must stay private.

## Configure an explicitly authorized remote Worker

Create `$AF_STATE/workers.toml` with operator-approved endpoint/model values:

```toml
[[providers]]
id = "windows_inference"
type = "ollama"
base_url = "http://windows-ollama.private:11434"
trust = "local" # Explicitly authorized private Windows inference host.

[[workers]]
id = "windows_4080"
provider = "windows_inference"
model = "operator-installed-tool-model"
supports_tools = true
timeout_seconds = 60
```

The hostname is a placeholder. Restrict Windows Ollama to the authorized private
LAN/Tailscale connection and firewall. Plain HTTP requires a trusted encrypted
network when source crosses hosts; use HTTPS for authenticated remote endpoints.
For compatible cloud inference, use the existing `openai_compatible` configuration
and credential environment-variable names in [Provider configuration](providers.md).
Choosing that Worker authorizes sending Task prompts and selected tool evidence
to its endpoint. No filesystem descriptors, tool authority or remote shell are
given to the inference service. There is no routing/fallback when Windows is down.

## Start local MCP in Codex

Configure a distinct stdio entry using absolute paths, replacing `/Users/you`:

```toml
[mcp_servers.agentforge_mac]
command = "/Users/you/Library/AgentForge/venv/bin/agentforge"
args = ["mcp", "--database-url", "sqlite:////Users/you/Library/AgentForge/mac.sqlite", "--workers", "/Users/you/Library/AgentForge/workers.toml"]
```

The interpreter/module form in [MCP setup](mcp.md) is also supported. Configure
`agentforge_windows` separately with the authenticated URL/grants from the Gateway
guide. Use one local stdio executor per Mac database; a second launch refuses
before schema validation/recovery. Optional `--companion` shares that executor.

Discover `list_projects`, `list_agents`, `list_workers`; delegate explicitly to
`repo_explorer` and `windows_4080` with the Mac Project UUID. Request an evidence
check using `read_file`, `search_code`, `git_status` and `git_diff` on a known dirty
Swift file. Watch/poll the Task, inspect its result and verify source evidence.
Do not assume the Windows instance knows the Mac UUID or source state.

## Enable isolated coding and trusted Xcode validation

First use a disposable repository. Configure `$AF_STATE/coding.toml`:

```toml
workspace_parent = "/Users/you/Library/AgentForge/worktrees"
git_executable = "/usr/bin/git"

[validations.swift_test]
argv = ["/usr/bin/swift", "test"]
timeout_seconds = 120.0

[validations.xcode_build]
argv = ["/usr/bin/xcodebuild", "-project", "FieldForge.xcodeproj", "-scheme", "FieldForge", "-sdk", "iphonesimulator", "build"]
timeout_seconds = 120.0
```

These optional validators require the actual toolchain/project/scheme; remove
unavailable commands. Install/select Xcode and accept its license as the operator.
Use fixed trusted arguments for your project and configure appropriate build
output paths. SwiftPM dependency downloads, signing, simulator/device behavior and
Xcode account access are operator responsibilities. The conservative environment
does not inherit arbitrary user/toolchain/credential variables. A validator may
need a separately installed trusted executable to establish its host environment.
Workers can select only a configured name; they cannot supply argv or shell text.
**Validators execute trusted host code, not a security sandbox.** They can access
the host/network and original checkout through their own behavior.

Run `agentforge coding check --coding ... --database-url ...` and add `--coding`
with this file to the local MCP args. Discover/select `coder` explicitly. Its clean
worktree derives from committed HEAD; dirty primary edits are not copied. Record
primary HEAD, branch, index and dirty files before and after. Inspect
`get_coding_workspace`, `get_coding_diff` and validation captures, then explicitly
request `cleanup_coding_workspace` with both identities after saving the diff.
No automatic commit, push, PR, merge or cleanup occurs. See [coding](coding.md).

## Shutdown, recovery and limitations

Stdio EOF or normal shutdown drains local executors/validators and releases kernel
ownership. Forced process death releases locks; restart against the same private
database marks running Tasks/workspaces interrupted, retains changes, and resumes
only queued Tasks. No running inference/coding Task is replayed. Remote inference
may continue after local cancellation. Windows/network unavailability produces a
bounded Provider failure, with no alternative Worker selected automatically.

Only local APFS is accepted for native repository/storage operations. HFS+, SMB,
NFS, FUSE, shared/network SQLite and unsupported Darwin capabilities fail closed.
Device/inode identity, no-follow `openat` traversal, `F_GETPATH` spelling checks,
local filesystem identity and closing ancestry/file checks authorize access.
Repository files must have one hardlink; clones with distinct inodes are ordinary
files. Symlinks and special objects are denied. Mount transitions are denied.

Case-sensitive APFS supports distinct exact names. Case-insensitive APFS aliases
and Unicode normalization aliases are refused for repository tools: use the exact
directory-entry spelling. No normalization rewrite/merge occurs. Root aliases
that differ from the descriptor's canonical path are refused. Existing operator
registration symlinks resolve once to the canonical root. Privileged mounts,
identity reuse and hostile same-user administration remain outside the contract.

Git starts through a separate Python child calling `fchdir` on the inherited
authorized descriptor, then `exec`; it never reopens an unchecked root pathname.
There is no `/proc` or `/dev/fd` fallback. Coding Git runs fixed metadata commands
at its pinned administrative directory; file comparison uses authorized reads
against immutable Git blobs. Git metadata and executable installation are trusted,
as for the existing Linux backend. New files use
`renameatx_np(RENAME_EXCL)`; edits/deletes retain the existing hash/conflict checks.
Darwin process-group termination is serialized across output readers and the
owner, and becomes complete only after a successful signal or an absent group.
This avoids repeated-kill EPERM races while preserving real permission failures.
Trusted executable pins permit Apple's system executable hardlinks only at those
operator-configured executable call sites, never for repository data.

SQLite ownership uses permanent-sidecar `flock` plus a database-inode Darwin
`F_OFD_SETLK` lock on byte zero. SQLite uses its normal APFS POSIX transaction-lock
range starting at `0x40000000`; the ownership lock is disjoint. A whole-file
database `flock` is deliberately avoided. No PID or stale-file deletion is used.
See [ownership details and alias limits](executor-ownership.md).

## Reproducible acceptance

The ordinary full suite and installed-wheel smoke run on hosted `macos-15` for
Python 3.12, 3.13 and 3.14 alongside the existing Ubuntu/Windows matrix. Environment
artifacts record actual macOS, filesystem, Python/SQLite, Git and dependencies.
Preflight requires actual local APFS for checkout and temporary storage. Native
CI also mounts separate disposable case-sensitive and case-insensitive APFS images,
verifies actual case behavior, repeats security/coding/ownership tests and detaches them.
Image/mount failures fail that step; no emulated APFS acceptance is substituted.
Tests include independent replacement processes, SQLite contention/kill/restart,
transaction-lock coexistence, dirty Swift, indexing and the full coder/MCP suites.
Unavailable native capabilities are failures or specifically documented skips;
mocked Providers demonstrate protocol behavior, not real Windows inference.

Physical #31 still requires actual MacBook APFS alias/race tests, a dirty FieldForge
checkout, Xcode simulator/device builds, real Windows Ollama over Tailscale,
Windows sleep/network loss, disposable isolated coding with primary preservation,
and explicit comparison with the remote Windows Gateway workflow. Record versions,
SHA, sanitized Task evidence and outcomes without marking unperformed rows complete.
