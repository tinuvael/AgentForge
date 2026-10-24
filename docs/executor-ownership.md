# Exclusive local SQLite executor ownership

Issue #44 enforces one active TaskEngine per supported local database. This prevents
another MCP/web process from recovering the owner's live Tasks as interrupted.
It does not add scheduling, multiple active executors, or multi-client MCP transport.
The persistent authenticated Gateway remains #46; physical acceptance remains #31.

## Ordering and boundaries

1. Application construction captures an absolute resolved SQLite path, composes
   repositories and lazy Providers, and validates configured coding roots without
   creating workspaces, opening SQLite, checking schema or recovering anything.
2. `TaskEngine.start()` acquires nonblocking ownership **before** schema checks,
   interrupted-Task recovery, coding-workspace recovery, or executor loop creation.
   It discards idle pooled connections from earlier ownership so an offline DB
   replacement cannot leave the new owner reading the old inode.
   Recovery commits genuinely orphaned running Tasks as `execution_interrupted`;
   Council participant snapshots derive from those Tasks. Ready/provisioning coding
   metadata is then recovered by the existing workspace manager. No migration runs.
3. Each loop claims a queued Task durably before provisioning coding/inference.
   Submission commits queued Tasks (Council + participant Tasks in one transaction).
   Cancellation remains queued-terminal or cooperative-running cancellation.
4. Application shutdown first drains Worker diagnostics, then closes TaskEngine.
   TaskEngine signals cancellation, cancels and awaits every executor, including
   Provider cancellation cleanup, terminal persistence and coding finalization.
   Only then does it release ownership. Queued rows survive. Repeated close is safe;
   cancellation of the close caller leaves shielded cleanup holding ownership.
5. Application then closes owned Provider clients and disposes the SQLAlchemy pool.
   No Task/coding writes follow TaskEngine cleanup. MCP owns this whole lifespan;
   combined Companion borrows the same started Application, with no second owner.
   Standalone web owns a separate Application and therefore must acquire ownership.

Direct Python TaskEngine submission before `start()` remains supported. Task,
Council submission and cancellation take short exclusive ownership around the
operation, release it afterwards, and perform no recovery or inference. They fail
if another executor owns the DB. Application delegation still requires a started
engine. Application coding inspection/diff/cleanup also takes ownership because
inspection can persist workspace observations. Raw repositories, migration commands
and administrative writes are trusted operator APIs: **stop executors first**.
Read-only administrative DB status does not acquire executor ownership.

Startup failure (including `CancelledError` during schema/recovery) releases all
partially acquired handles. There is no retry/polling loop. A process crash releases
kernel handles; its next legitimate owner performs normal recovery. Failed startup
cannot release a different engine's lock. Ownership alone cannot stop inference
already accepted by a remote Provider, or guarantee exactly-once execution.

## Kernel mechanisms and stable identity

Persistent databases use `<database filename>.agentforge.lock` in the database's
trusted directory. The file contains no PID, path, Task or secret. It is never
truncated or unlinked on release. An unlocked leftover file is normal; do not remove
or replace lock files while any executor may be alive. Deleting a locked sidecar
could create a second inode and defeat pathname-only locking.

- Linux: `flock(LOCK_EX | LOCK_NB)` on the no-follow sidecar descriptor **and** the
  pinned database inode. The second lock also covers a renamed or bind-mounted file
  alias with a different sidecar path. These local flock locks are separate from
  SQLite's POSIX byte-range transaction locks. Descriptors are non-inheritable.
- Native Windows: `msvcrt.locking(LK_NBLCK, 1)` locks byte zero of the sidecar,
  including an empty file, without timed CRT retries. Win32 handles pin database,
  lock and parent ancestry; sharing allows SQLite read/write but denies deletion.
  Handles and CRT descriptors are non-inheritable. Locks disappear on process death.
- The process-local registry remains for independently composed engines and
  in-memory SQLite. A losing owner never reaches schema/recovery or executor launch.

References: [Python CRT locking](https://docs.python.org/3/library/msvcrt.html),
[Linux flock semantics](https://man7.org/linux/man-pages/man2/flock.2.html).

## Filesystem and alias policy

Supported persistent storage is Linux local ext-family, XFS, Btrfs or tmpfs, or
native Windows fixed local NTFS. Unknown filesystems fail closed, including NFS,
SMB, FUSE and overlay filesystems. Other POSIX platforms currently fail closed:
BSD/macOS database flock may interact with SQLite locking and is not represented
as validated Linux behavior. No distributed filesystem ownership is promised.

Use a trusted operator-controlled storage directory and ancestry. On Linux the
immediate parent and files must be owned by the effective user; parent/database
must not be group/world writable. Sidecars are created 0600 and existing sidecars
must remain private. Windows uses the storage directory's inherited operator ACL;
configure it privately. Shared or attacker-controlled directory administration is
outside the threat model; a hostile same-user process can always bypass advisory
locks or replace application code. Linux replacement checks are detection before
SQL, not an OS sandbox against malicious concurrent same-user filesystem writes.

Relative/absolute paths and `.`/`..` normalize to the same absolute path before
SQLite opens. Existing symlinks resolve to their target. Changing the working
directory afterwards cannot retarget a composed database. Windows resolves ordinary
case aliases through NTFS, rejects alternate streams, device/UNC paths,
reparse ancestry and aliases not matching the resolved final handle path. Case-sensitive Windows directories are unsupported.

Database and sidecar must be regular files with exactly one hardlink. This is
checked at acquisition and before every SQL statement while owned, so creating a
hardlink later also stops the owner. Linux retains descriptors and rechecks inode,
parent and pathname identity before SQL: replacement/deletion fails closed; a
contender at the original path still encounters the permanent sidecar. Windows
handles prevent replacement for their lifetime. Never replace, rename, chmod or
retarget an active database/lock/parent; stop the service before restoring backups.
Path normalization alone does not eliminate every alias. Windows mount/namespace
aliases outside the fixed ordinary NTFS path policy and arbitrary namespace changes
are unsupported. Creation, permissions, unsafe file types and unavailable identity
produce a fixed sanitized ownership-unavailable diagnostic, without SQL or paths.

In-memory `sqlite://` / `:memory:` keeps the existing per-SQLAlchemy-engine process
restriction; it has no filesystem ownership or cross-process shared persistence.
TaskEngine rejects URI/query-option SQLite URLs and other database backends, rather
than implying a file lock covers ambiguous configuration. Existing explicit
read-only administration and migrations retain their separate URL handling.

## Operator diagnostics and acceptance

A competing executor exits promptly with:

> Another AgentForge executor already owns this database. Stop the other MCP/web process or use a separate database.

A second Codex CLI/IDE session can launch another stdio child, but that child fails
before recovery. The first service continues normally. Use one client session per
DB, or separate databases with separate registration/history. `--companion` shares
one owner and remains the supported way to combine MCP and the operator browser.
No extra HTTP MCP endpoint, authentication, routing or fallback has been added.

`tests/test_executor_ownership.py` launches fresh Python interpreters with JSON pipe
barriers, bounded waits and finally-block kill/wait/stream cleanup. It covers live
scripted inference, immutable Task/Council/coding state on rejection, simultaneous
starts, forced kill/recovery, startup failure/cancellation, aliases and restart.
Workspace rows in the crash/contention test are synthetic metadata and use the real
workspace recovery method; existing coding suites validate real managed worktrees.
All unmarked cross-process tests run on both hosted platforms. `windows` tests use
real native locks/NTFS; `posix` tests use real Linux descriptors, never Windows mocks.
CI's six Linux/Windows × Python 3.12/3.13/3.14 jobs also run installed-wheel,
migrations, MCP discovery, Companion, coding and Task recovery regressions. Physical
workstation/RTX 4080, live Ollama and real model-driven Codex acceptance remain #31.
