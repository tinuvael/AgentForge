# Security and trust boundaries

AgentForge is a trusted-user control plane, not a multi-tenant service or an OS
sandbox. Deploy it under an account authorized to read registered Projects and to
run any explicitly configured validation commands.

## Deployment responsibilities

- Keep the database, operator TOML, executable installation, workspace parent and
  temporary storage private and trusted. Task requests and final answers are
  stored; they may contain source or secrets. History retention is manual.
- Choose Workers with their data destination in mind. Local, LAN and cloud
  inference receive Task context and selected tool evidence. Coding diffs and
  validation captures may leave the host. Endpoint retention is outside AgentForge.
- Put credential environment-variable names in named Provider connections.
  Keep actual tokens in operator secret provisioning, use HTTPS for remote
  authentication and restart after rotation. Credentials are not stored on Workers
  or projected to MCP/dashboard. HTTP has no transport encryption.
- Use exactly one executor process per database. Ownership is checked only within
  a process; AgentForge does not enforce this across OS processes. A second owner
  can start and recovery can interrupt live Tasks and affect coding workspaces.
- Treat MCP stdio clients as trusted local Directors. Dashboard CSRF, Host checks,
  escaped output and loopback defaults do not provide authentication. Protect
  intentional LAN exposure with a trusted network/VPN or authenticated proxy.
  `agentforge mcp ... --companion` accepts loopback IP binds only and serves the
  full dashboard (`/`, `/workers`, `/projects`, `/tasks`, `/councils`) plus
  `/companion`, including trusted cancellation and workspace cleanup controls.
  Standalone `agentforge web` may be intentionally LAN-bound and also exposes
  Companion. The neutral OPERATOR label is not an assertion about network binding.

## Repository and coding boundary

Registered root identity and no-follow access protect against traversal and
ordinary replacement races. Model calls have explicit tool allowlists and
structured arguments; model text and repository instructions never grant authority.
Repo Explorer is read-only. Coding tools bind to a dedicated Task worktree and
preserve primary files/index/HEAD. Runtime never commits, pushes, merges or opens PRs.

POSIX read-only access rejects symlinks/special files and detects changes, but
allows regular-file hardlinks and does not claim mount containment. POSIX coding
additionally rejects hardlinks and directory/file mount transitions. Native
Windows requires ordinary local fixed NTFS, pinned ancestry, single-link files
and no reparse points or ambiguous aliases. Unsupported access fails closed.
Detailed guarantees and limits: [repository tools](docs/repository.md),
[Windows security](docs/windows-repository-security.md), [coding](docs/coding.md).

Sensitive filename exclusions are defense in depth, not secret scanning or DLP.
Secrets in ordinary source or a visible model answer can be returned. Private
Provider reasoning stays in ephemeral history, but AgentForge cannot guarantee
that a model will never repeat sensitive material in its visible answer. Git
metadata must remain trusted during POSIX fixed-command observations. Concurrent
malicious mutation by the same user, privileged mounts/kernel changes, identity
reuse and preexisting writable memory maps are outside the security contract.

## Validation is trusted host execution

An allowlisted validator can execute repository/model-edited code and access host
files, the primary checkout and network through its own logic. **This is not an OS
or network sandbox.** Enable validation only where that execution is trusted.
Absolute executables, fixed argv, conservative environment, bounded captures and
process groups/Windows Jobs reduce accidental exposure and limit execution. They
do not confine hostile code; POSIX daemonized descendants can escape a process
group. No package installation is performed automatically.

Completion/failure/cancellation retain coding changes. Explicit cleanup discards
uncommitted edits after ownership checks; export the diff first. Suspicious or
incomplete ownership refuses destructive recovery. Windows file rewrites are
not crash-atomic and may retain partial edits after interruption.

## Reporting

Report defects to the repository owner. For a vulnerability, use GitHub's private
vulnerability reporting in the repository Security tab if available; otherwise
contact the owner to arrange private disclosure before posting exploit details
or sensitive repository contents publicly. This first implementation is pending
native Windows, Ollama and hardware acceptance; offline mocks are not physical
platform validation.
