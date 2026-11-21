# Synthetic Codex Worker over SSH

The Windows Gateway owns the Task queue and SQLite. A configured `ssh_codex`
Worker uses one existing TaskEngine slot to open one SSH connection to Linux.
The remote launcher requires Linux process-group semantics. The Ubuntu host
needs Python 3, SSH and its own authenticated Codex CLI. It
receives a single fixed synthetic prompt over stdin. The SSH transport does not
receive the Gateway credential, project files, project paths or Task metadata.

## Prepare Ubuntu for an approved smoke test

1. Use an unprivileged account dedicated to synthetic testing, with no
   repositories, operator permissions or service credentials. Authenticate
   Codex on Ubuntu using that account's own credentials. Verify that its trusted
   `~/bin/codex-remote` wrapper forwards top-level and exec arguments, uses
   the current working directory, and preserves stdin, stdout and the exit
   status. After making the installed Codex binary available on `PATH`, the
   wrapper should end with `exec codex "$@"`. Check `--version`, `--help` and
   `exec --help` on that installation before the smoke.
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
indicates cancellation in Gateway, so check the timeline. The launcher can
return a normal terminal reply while the Gateway is cancelling. In that
case the timeline retains the remote outcome before the Gateway's cancellation
event. Only a remote cancellation reply confirms cancellation on Ubuntu.
The launcher also attempts cleanup on SSH EOF, receipt of a shutdown signal,
or its own 120-second deadline. A permission error during signalling or probing cannot
confirm termination. The launcher can still escalate from SIGTERM to SIGKILL
and confirm the stop if the group subsequently disappears.
Process-group tracking covers descendants that stay in
the group. The protocol has no reconnect or automatic retry. If the Gateway
itself shuts down or crashes, use the existing interrupted-Task handling and
verify remote state before any manual resubmission.

The Worker does not accept arbitrary prompts, project context, tools supplied
by AgentForge, Council or workflow calls, or explicit request or token budgets.
Codex's model identity and internal usage are not reported by this integration.
To disable the Worker, remove its TOML entry at the next approved Gateway restart.

## Read-only repository pilot

The repository pilot uses the same SSH transport and Task lifecycle, with a
dedicated `ssh_codex_repository` Agent. Use a separate Gateway database and
client grants for the pilot. Start with a single Worker, Gateway concurrency
of one and an independently authenticated Linux Codex account. Grant the
explicit project and `inline:<worker-id>` egress to that account's inference
service. SSH only authenticates the transport.

1. Identify an existing Ubuntu checkout. Record the GitHub `owner/repository`,
   canonical checkout root and the full lowercase 40-digit merged commit.
   Verify that the checkout has a real `.git` directory, that the local
   `remote.origin.url` is the matching GitHub HTTPS or SSH URL, and that HEAD
   resolves to the pinned commit. Update the checkout separately if needed.
2. Install the reviewed launcher and
   `src/agentforge/execution/ssh_codex_repository.py` alongside it under
   `~/bin`. Keep both under the authenticated pilot account. Pin the SSH host
   key and configure the pilot identity's forced command with OpenSSH
   `restrict`:

   ```text
   exec /usr/bin/python3 "$HOME/bin/agentforge-codex-worker" --repository
   ```

3. Create the private Linux profile
   `~/.config/agentforge/ssh-repository.toml`, owned by the pilot account and
   writable by that account alone. Use the actual values agreed with the
   Gateway.

   ```toml
   repository = "owner/repository"
   commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
   root = "/srv/checkouts/repository"
   files = ["README.md", "src/config.py", "tests/test_config.py"]
   ```

   Authorize the listed files for model access. The launcher reads only regular
   committed UTF-8 blobs at the pinned SHA. It rejects symlinks, submodules,
   missing files and mismatched identity. A profile can list up to 24 files,
   each at most 16 KiB and 64 KiB total. Keep the profile fixed while a Task
   runs. A lock on the profile permits a single remote run at a time.
4. To the isolated Worker's configuration above, add the following block.
   Grant `ssh_codex_repository` to the isolated client. Keep the existing
   synthetic configuration dedicated to the fixed acceptance prompt.

   ```toml
   [workers.ssh_repository]
   repository = "owner/repository"
   commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
   ```

On submission, provide a standalone analysis question (up to 4 KiB UTF-8).
The launcher verifies both pins before starting Codex. It passes the question
and committed source via memory-backed stdin. Codex runs in an empty temporary
directory, in read-only mode, with shell, web search and configured MCP disabled.
Citations are verified against the transmitted paths and inclusive line ranges.
The Gateway records the answer and evidence with the repository and full commit.
The reply is capped at 64 KiB and the remote deadline is 120 seconds.

Acceptance uses two distinct questions whose answers can be verified in the
allowlisted files. Also check mismatched identity, cancellation, lost transport
and absence of duplicate executions. Treat `remote_stop_unconfirmed` as an
unknown remote outcome and check it before manually creating another Task.
The pilot does not modify the checkout, synchronize it or retry automatically.
