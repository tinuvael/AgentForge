# Synthetic Codex Worker over SSH

The Windows Gateway owns the Task queue and SQLite. A configured `ssh_codex`
Worker uses one existing TaskEngine slot to open one SSH connection to Linux.
The Ubuntu host needs Python 3, SSH and its own authenticated Codex CLI. It
receives a single fixed synthetic prompt over stdin. The SSH transport does not
receive the Gateway credential, project files, project paths or Task metadata.

## Prepare Ubuntu for an approved smoke test

1. Use an unprivileged account dedicated to synthetic testing, with no
   repositories, operator permissions or service credentials. Authenticate
   Codex on Ubuntu using that account's own credentials. Verify that its trusted
   `~/bin/codex-remote` wrapper forwards top-level and exec arguments, uses
   the current working directory, and leaves stdin and stdout to Codex.
2. Install the reviewed
   [Python launcher](../src/agentforge/execution/ssh_codex_runner.py) as
   `~/bin/agentforge-codex-worker` under that account. Keep the launcher
   and wrapper writable only by trusted operators. The Worker invokes a fixed
   command through SSH. Prefer a dedicated public key entry with a forced
   command and OpenSSH `restrict` on the remote account. The forced
   command must match the Worker's fixed command:

   ```text
   exec /usr/bin/python3 "$HOME/bin/agentforge-codex-worker"
   ```

   Keep the private identity file on Windows. Use the existing private network
   path and SSH service.
3. The launcher uses a fresh empty working directory, read-only sandbox, and
   `on-request` approval policy. It forwards no interactive approvals.
   It uses ephemeral JSONL output and ignores user configuration for this run,
   so the remote account must be able to authenticate independently of custom
   user settings. Review account isolation and organization-level Codex policy
   before approving the smoke. Codex can contact its model service, so authorize
   that destination and any cost even though SSH travels over Tailscale.

## Configure Windows

1. In an isolated Gateway instance with its own temporary SQLite, register an
   empty synthetic project. For later production enablement, provision a
   dedicated synthetic project during a maintenance window following the
   [Gateway procedure](gateway.md). Set its egress policy to
   `cloud_allowed` with `--allow-connection inline:ubuntu-codex-smoke`.
   Leave working project registrations and policies untouched.
2. Add the following to a test copy of the Worker's TOML. Substitute the
   Tailscale address, SSH account, files and synthetic project UUID.

   ```toml
   [[workers]]
   id = "ubuntu-codex-smoke"
   kind = "ssh_codex"
   provider = "ssh_codex"
   model = "codex-cli"
   trust = "cloud"
   supports_tools = false
   timeout_seconds = 90

   [workers.ssh_codex]
   host = "100.100.100.100"
   user = "codex-test"
   project_id = "00000000-0000-0000-0000-000000000000"
   ssh_executable = "C:/Windows/System32/OpenSSH/ssh.exe"
   identity_file = "C:/AgentForgeState/ssh/codex-smoke"
   known_hosts_file = "C:/AgentForgeState/ssh/codex-known-hosts"
   ```

3. Verify the host key via a trusted channel and pin it for the configured
   address in the specified `known_hosts` file. Run the offline check in
   the actual Windows service account context, using the service's interpreter
   and these same absolute paths:

   ```powershell
   & $afPython -m agentforge worker config-check --workers $testWorkersToml
   ```

   This opens the three local files without reading their contents, opening
   SSH, or accessing SQLite. Also verify ACLs and confirm at the approved smoke
   that OpenSSH accepts the key without a prompt. User-profile SSH settings,
   implicit trust and agent forwarding are disabled for this connection.
4. Grant the MCP client access to the synthetic project, the
   `ssh_codex_synthetic` Agent, the new Worker and the required existing
   Task operations. With separate approval for live SSH and Codex, submit
   `delegate_task` with the exact request below. Verify the terminal
   result via `get_task` and Task progress via `watch_task`.

   ```text
   Reply with exactly AGENTFORGE_SSH_OK. Do not use tools.
   ```

## Result and cancellation

Success requires an SSH exit code of zero and a valid launcher response for
`AGENTFORGE_SSH_OK`. The launcher requires a completed Codex turn, its
matching answer and a zero Codex exit code. It caps Codex stdout at 256 KiB
and sends a small structured response. The Gateway caps SSH stdout at 1 KiB.
Stderr and raw Codex events are discarded.

When cancelling a running Task, the Gateway sends a cancellation frame on the
same SSH connection. The launcher terminates Codex's process group and
responds only after checking that the group is gone. If the Gateway cannot
establish a confirmed remote outcome, the Task timeline records
`remote_stop_unconfirmed`. A terminal `cancelled` Task alone
indicates cancellation in Gateway, so check the timeline. The Linux launcher
also attempts cleanup on SSH EOF, receipt of a shutdown signal, or its own
120-second deadline. Process-group tracking covers descendants that stay in
the group. The protocol has no reconnect or automatic retry. If the Gateway
itself shuts down or crashes, use the existing interrupted-Task handling and
verify remote state before any manual resubmission.

The Worker does not accept arbitrary prompts, project context, tools supplied
by AgentForge, Council or workflow calls, or explicit request or token budgets.
Codex's model identity and internal usage are not reported by this integration.
To disable the Worker, remove its TOML entry at the next approved Gateway restart.
