# Isolated coding Tasks (Phase 12)

## Design and ownership

1. The Project Registry owns the primary checkout identity and read-only boundary.
2. CodingWorkspaceManager owns an operator-selected private workspace parent;
   runtime roots are `<parent>/<project UUID>/<task UUID>`, outside every primary
   repository. Neither the Director's Task nor the Worker supplies a root.
3. The manager generates `agentforge/task-<full task UUID>` and refuses collisions.
4. Ordinary TaskEngine owns claim, cancellation and terminality. Provisioning is
   after claim and before the first Provider request, never a model tool.
5. Agent.workspace_mode selects a separate tool catalog. The coder's reads and
   writes use the same workspace capability, never canonical Project Index/tools.
6. The central host executes fixed Git operations and named trusted validations.
   Workers receive only schemas and bounded source/tool results, never handles,
   host paths, process access, Git credentials, push or merge authority.
7. Durable workspace metadata and explicit manager cleanup own recovery. Task
   recovery never replays interrupted coding work or deletes partial changes.

The tool root is the original Project-relative subtree inside the new worktree,
not the repository root. Sibling source is not materialized. Validation commands
are trusted operator code; they are **not an OS sandbox**, can run repository code,
access the host and network, and must only be enabled for trusted repositories.

Provisioning uses `worktree add --no-checkout` from the exact captured committed
HEAD, then raw `ls-tree`/`cat-file` blobs and a worktree-only `read-tree` index.
Unmaterialized siblings and special entries are marked `skip-worktree` in that
index, so ordinary Git inspection does not misreport them as deletions. The
bounded full base-tree inventory is private and never reaches the Worker.
No checkout/smudge/clean/LFS hook is invoked. Primary files, index, HEAD and branch
are untouched; uncommitted primary changes are not copied. Symlinks/submodules
are not materialized and remain non-editable special entries. Phase 12 produces
an uncommitted diff and never commits, pushes, fetches, merges or creates a PR.

The implementation, trust boundaries and tested limits follow below.

## Enable and configure

Coding is opt-in. Supply `coding_config=CodingConfig(...)` when composing the
Application, or `--coding /absolute/path/coding.local.toml` to either
`python -m agentforge.mcp.server` or `python -m agentforge.web.server`.
Without that configuration, only Repo Explorer is shipped by default. With it,
`coder` is additionally discoverable. Custom Agents must explicitly declare
`workspace_mode="isolated_write"` and an explicit tool allowlist. Council rejects
that capability generically, including Agents with other IDs, before participant
Tasks or workspaces are created.

See [coding.example.toml](../config/coding.example.toml). The operator must create
an existing private workspace parent outside every registered repository. POSIX
requires an owned 0700 parent, Linux descriptor-backed Git cwd and `renameat2`.
Windows requires ordinary fixed local NTFS, no reparse ancestors, case-insensitive
directories and a private user-owned ACL, consistent with the existing backend.
Do not put runtime state in the implementation repository. Database, private
workspace storage, executable installation and operator configuration are trusted.

The Git executable and each validator executable must be explicit absolute real
files outside source/workspace trees, with no-follow ancestry. On Windows select
Git for Windows's real `mingw64/bin/git.exe` (or `mingw32/bin/git.exe`), not its
`cmd` launcher, and direct `.exe` validators, not `.bat`/`.cmd` files. For a POSIX
virtual environment, use `python -m venv --copies` outside source/workspace trees:
resolving a virtualenv interpreter symlink to the base interpreter loses the
virtualenv's installed packages. The host pins executables while they
run. Tool schemas advertise configured validation IDs, never executable paths or
argv. Configuration is global to this host instance in Phase 12; per-Project
validation profiles are not implemented.

Migrate explicitly with `alembic upgrade head`. Revision
`0008_coding_workspaces` adds one record per Task: workspace/Project/Worker identity,
branch, base, private paths, opened root identities, UTC creation time, lifecycle
state and bounded counters/validation observations. No historical migrations
change. Source bodies and complete diffs are not stored in SQLite. Ordinary
Task execution JSON includes a bounded factual `coding_result`; original Task
reason/final answer keep their existing contract and private reasoning remains
memory-only. Discovery, Provider abstractions and explicit Worker selection are
unchanged.

## Tools, preconditions and limits

All tools are host-executed and bound before the first model turn:

| Tool | Contract |
| --- | --- |
| `list_files`, `search_code` | Direct bounded workspace reads; no durable canonical Project Index access. |
| `read_file` | Strict UTF-8 text, line range and whole-file SHA-256 from the same authorized byte observation. |
| `write_file` | `path`, `content`, required `expected_sha256`: null creates an absent file exclusively; a hash replaces an existing ordinary text file. Missing parent directories may be created safely. |
| `apply_patch` | `path`, required hash, nonempty `old_text`, `new_text`; exactly one old-text occurrence must exist. This is a structured text replacement format, not unified diff. |
| `delete_file` | `path`, required current hash; one ordinary UTF-8 file only. No recursive/directory removal. |
| `git_status`, `git_diff` | Central public-file comparison against immutable Git base; includes newly created files, explicit truncation and no raw Git/process API. |
| `run_validation` | Only `name`; no argv, env, cwd, timeout, root or ref input. |

A conflict requires rereading; the host never silently overwrites a changed file.
Binary/undecodable/control-character text is rejected for edits/deletion. Patches
have no headers, rename, mode, symlink, binary or submodule operations. Those
unrecognized schema fields are rejected rather than passed to `patch`/`git apply`.
Sensitive-path policy and `.git` exclusion apply to every model write. Committed
symlinks/submodules remain absent and cannot be replaced or written through,
including Windows case aliases. Git executable bits are preserved on POSIX.

Defaults are 24 write attempts, 256 KiB per edited file, 64 KiB combined patch
old/new text, 1 MiB total reserved write bytes, 32 distinct attempted edit paths,
8 validation attempts, 120 seconds per validator (also bounded by ordinary Agent
wall time), 8 KiB captured per stdout/stderr and 64 KiB Director diff. RuntimeLimits
still bound model steps, tools, serialized output, context and total wall time.
The model receives smaller captures/diffs fitting its tool output budget, with
explicit flags. Budget reservations are durable before mutation; `bytes_written`
counts only successfully observed writes, and write attempts/capture coverage
remain separate facts. Cancellation/process loss can leave additional unobserved
partial effects; counters do not invent them.

Provisioning is capped at 60 seconds, 10,000 base entries, 2 MiB per source blob
and 256 MiB copied bytes. Git metadata is bounded to 100,000 entries, 128 MiB per
file and 256 MiB total, with a two-second fixed-command timeout. Inspection scans
at most 10,000 public files / 256 MiB, limits each file to 2 MiB and each textual
diff input to 10,000 lines; at most 100 changed names and 8 KiB stats are returned.
Exceeding inspection limits makes live inspection unavailable rather than
fabricating a clean or complete diff. Canonical Index refresh still sees only
the original checkout, including after a coding Task finishes.

## Filesystem and Git security

POSIX reads and mutations are no-follow descriptor operations anchored to persisted
root identity. The coding backend additionally rejects multi-link files and mount
transitions. New directories use descriptor-relative creation. Writes use an
exclusive random temporary file in the authorized directory, fsync, ancestry and
file precondition rechecks, and same-directory `replace` or atomic Linux
`renameat2(RENAME_NOREPLACE)` creation. No generic chmod/chown/link/symlink API is
exposed. Existing executable bits are preserved without retaining setuid/setgid.
Deletion unlinks only a precondition-checked ordinary file.

Windows reuses the native Win32 backend for canonical drive paths, fixed NTFS,
ordinal case semantics, reparse rejection, stable volume/file identity and pinned
ancestry. New mutation primitives use `OPEN_REPARSE_POINT`, `CREATE_NEW` or
`OPEN_EXISTING`, read/write/delete handles with no write/delete sharing, ordinary
single-link checks and final handle-path verification. Each pinned ancestor is
rechecked before mutation. Existing-file rewrites seek/truncate/write/flush through
the exclusive handle; deletion uses handle disposition. Windows rewrites are
**not crash-atomic**: an interrupted write may leave a partial coding file, which
is retained for review. They never reopen an authorized target by an unchecked
pathname. UNC/NT namespaces, ADS, drive-relative names, DOS devices, trailing dots
or spaces, 8.3 aliases, junctions and case-sensitive NTFS directories are denied.

The primary checkout invariant covers AgentForge's built-in filesystem and Git
operations: no primary file/index/HEAD/branch edits, stash, reset, clean or checkout.
Dirty primary changes are preserved, not copied into the coding workspace. Base
HEAD is captured once and does not move when the primary checkout moves. Fixed
Git commands use pinned cwd/metadata, literal paths, a conservative environment,
disabled global/system config, hooks, fsmonitor, pager, auto maintenance, protocols
and lazy fetching. Provisioning never invokes checkout filters or initializes
submodules. Only ordinary primary SHA-1 repositories with an in-place `.git`
directory are supported; linked/bare repositories, extensions, redirected object
stores and linked/special metadata fail closed.

Authoritative status/diff use fixed `ls-tree`/`cat-file` base data and no-follow live
workspace bytes with central deterministic text diffing, matching Phase 04's
source-safe comparison approach. They never invoke external diff drivers, textconv
or clean filters, and do not trust model-supplied patches as the result. Only the
registered subtree and public policy are released; sensitive committed files may
exist privately in that subtree but cannot enter read/diff tools. Sibling source
is not copied. Unsupported/special or oversized content is refused or explicitly
marked rather than presented as complete.

Concurrent ordinary primary file/HEAD changes are safe. Like Phase 04, this is not
an OS sandbox against a malicious process with the control-plane user's privileges,
privileged mount/kernel manipulation or preexisting writable memory maps. POSIX
ancestry rechecks detect practical replacement races but do not provide an atomic
filesystem namespace against such a process. Git metadata must not be concurrently
rewritten maliciously by another host process; its links/redirections/layout are
checked before fixed commands. Windows uses native share locks as well. Native
Windows tests are provided; Linux mock results do not claim Windows execution.

## Validation execution and privacy

Validation commands are explicitly **trusted operator code** and may execute
repository/model-edited code. This allowlist is **not an OS or network sandbox**.
Such code can access the host, network and files through its own program logic;
it must be enabled only when that execution is trusted. The built-in tool primary
checkout invariant cannot contain a deliberately hostile validator. No package
installation or automatic dependency setup is performed.

`Popen(shell=False)` receives the exact configured argv, a pinned authorized cwd,
closed inherited handles, no stdin and a fresh conservative environment. There
is no inherited HOME/USERPROFILE, Git configuration variables, Git credentials,
SSH agent, Provider/API token, cloud/package credential, PYTHONPATH or proxy env.
PATH is a fixed OS helper path (`/usr/bin:/bin`, or Windows System32), and the main
executable is absolute, preventing repository/cwd executable shadowing. Python
user-site loading is disabled. Windows SystemRoot/WINDIR are retained deliberately
for native process loading. No model environment override exists.

Concurrent stdout/stderr readers hold fixed-size captures; excess output terminates
the command and marks the affected stream truncated. Timeouts and cooperative Task
cancellation kill the POSIX process group, or a Windows kill-on-close Job assigned
before resuming a suspended process. Exited parents' remaining descendants are
terminated too. POSIX deliberately daemonized children can escape a process group;
no hostile-process containment claim is made. Unsupported Windows Job setup fails
closed before repository code runs. Nonzero exits are factual results, not retried
and not automatically interpreted as Task failure or coding success. No run means
no validation success. Known coding host paths are redacted from captures, ANSI/
controls are stripped, and dashboard templates still HTML-escape output/diffs.
Arbitrary text emitted by trusted code is not a general-purpose secret scrubber.

Cloud/LAN Workers receive Task prompts, selected source and bounded tool evidence,
including coding diffs and validation output. They receive no filesystem paths,
handles or credentials from coding tools; that does not prevent intentional source
data egress through inference. The external Director chooses the Worker and must
consider that data transfer. Raw Provider reasoning and hidden tool internals are
never part of coding results, MCP or dashboard projections.

## Inspection, cancellation, cleanup and recovery

Normal `delegate_task(project_id, agent_id="coder", worker_id, task)` uses ordinary
TaskEngine lifecycle. Workspace setup failure fails before any Provider request.
`get_coding_workspace(task_id)` returns private-path-free identity, lifecycle,
branch/base, changed names, stats, validation runs, write observations and termination
reason. `get_coding_diff(task_id)` regenerates the current bounded authoritative
diff while the workspace is retained. Final answer and Task reason remain in
`get_task`; factual coding observations stay separate from token telemetry.

Completion, failure and cancellation preserve workspaces by default. Cooperative
cancellation prevents further writes and terminates an active validator; it never
resets partial edits or destroys the workspace. Application shutdown awaits local
validation/Provider cleanup. Restart follows Phase 06: running Tasks fail with
`execution_interrupted`, are never resumed, and ready/provisioning workspace
records become `interrupted`. Terminal workspaces stay inspectable. Unknown
filesystem workspaces are never adopted or deleted; `orphans()` only reports
bounded logical Project/Task IDs. There is one control-plane process per database,
with no distributed lease or concurrent executor replay.

`cleanup_coding_workspace(task_id, workspace_id)` is an explicit destructive
Director operation for terminal Tasks only. The two IDs must match persisted
ownership. The manager verifies configured parent, parent/worktree/admin identities,
Git gitfile/registration association, generated branch and unchanged base. It
refuses links, mounts, special files, suspicious replacements and moved branches.
It uses bounded descriptor/handle removal of the known tree and its exact matching
Git administrative registration, rather than path-recursive `git worktree remove`
that could race a replacement root. The branch is always retained, and primary
files/index/HEAD stay unchanged. Branch deletion has no endpoint.

Export/review the diff **before** removal: uncommitted changes live in the worktree,
not in the retained branch. A removal interrupted before Git registration destruction
can be explicitly retried from `cleanup_pending`; incomplete/missing administrative
identity or interruption during registration deletion requires operator recovery
and never guesses an arbitrary path to delete. Failed partial provisioning with
known worktree/admin identity can be safely removed; failure before those identities
are persisted requires operator reconciliation. Startup never cleans it up.

Task detail shows status, branch, base, changed files, stats, bounded diff and actual
validation results without host paths. “Remove workspace” is POST-only with explicit
confirmation and existing CSRF protection; there are no commit/push/merge controls.
Inspection availability and truncation are explicit. A retained last observation
is labeled when the current filesystem/Git state cannot be inspected.

## Threat-model coverage

The numbered review corresponds to the Phase 12 request:

| Cases | Answer and offline evidence |
| --- | --- |
| 1–9: traversal, absolute/drive/UNC/NT/ADS/aliases/devices | Structured relative paths and strict portable Windows components; parametrized writes/patches/deletes and Windows mocks. |
| 10–15: symlink, junction, hardlink, ancestry/root replacement | No-follow/identity/mount and single-link checks, descriptor atomic publication or pinned Windows handles; escape and mid-write race tests plus native Windows tests. |
| 16–19: patch headers, link target, rename, mode | One explicit structured substring format, required hash, no header/options/path extraction; schema and special-object tests. |
| 20: recursive delete | Only ordinary-file `delete_file`; directory and special-object rejection tests. |
| 21–24: command ID/argv/shell/shadow injection | Simple allowlisted ID, forbidden extra fields, shell=False, absolute external executable; schema, cwd/env and shadow tests. |
| 25–27: huge output, hangs, cancellation | Bounded concurrent captures, command/group/Job termination and durable factual outcomes; stdout/stderr, descendant, timeout and cancellation tests. |
| 28: restart with dirty workspace | Durable metadata and ordinary interrupted-Task recovery; no replay/deletion, live diff and cleanup tests. |
| 29–30: wrong workspace ID/replaced cleanup root | Matching IDs, persisted root/admin identity and exact registration association; mismatch/link/root/gitdir replacement tests. |
| 31–33: subproject, dirty or moving primary | Only subtree copied/bound; immutable committed base; tests compare primary files/index/HEAD/branch/status and move primary HEAD independently. |
| 34–35: `.git` and sensitive paths | Existing policy plus portable component rules, applied to writes/reads/diff; parametrized denial and no secret diff tests. |
| 36–37: Explorer write/Council coder | Separate catalogs/capability rejection before I/O; renamed-coder Council and zero workspace/Task assertions. |
| 38: HTML/script/ANSI/control output | Plain-text normalization plus Jinja autoescaping and CSRF/confirmation; real ASGI tests. |
| 39: branch collision | Centrally generated full Task UUID ref, no force; collision/duplicate provisioning tests. |
| 40: hostile Git hooks/config/filters/LFS | No-checkout raw blob materialization, fixed isolated commands, no external diff/textconv/filters; marker-script adversarial test and redirected-metadata denial. |

Known limitations are the trusted-validation and same-user-process boundaries,
non-atomic Windows rewrite, bounded small ordinary repositories, special-object/
LFS-pointer handling (no fetch/smudge), conservative incomplete cleanup recovery,
no automatic commits/branch deletion and no multi-coder Council. These are explicit
rather than weakened filesystem permissions or fabricated sandbox guarantees.
