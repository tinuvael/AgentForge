# Native Windows repository security

The central Windows workstation owns Project files, the database, the Index and
all repository tools. Local, LAN and cloud Workers using Ollama or OpenAI-compatible
protocols are inference endpoints. They receive bounded tool results through AgentRuntime;
they need no repository checkout, SMB share or synchronization. MCP → TaskEngine
→ Repo Explorer → explicitly selected Worker runs natively without WSL.

## Platform boundary and identity

`projects.backends.SafeFilesystemBackend` is the internal authorization boundary.
ProjectRegistry selects it once: POSIX uses `PosixSafeFilesystemBackend`, native
Windows uses `WindowsSafeFilesystemBackend`. Index and RepositoryTools dispatch
through the same live directory capability. There are no platform checks in their
authorization logic and no generic pathlib fallback.

The `projects.filesystem` POSIX implementation uses no-follow
directory descriptors, descriptor-relative opens, ancestry checks on exit, and
device/inode identity. Linux Git still inherits the verified descriptor as
`/proc/self/fd` cwd, with process-group termination and its existing isolation.
Other POSIX hosts without descriptor-backed Git retain unavailable Git observations.

`RootIdentity` describes a platform, volume and file ID. Required JSON
`projects.root_identity` is shared by both supported platforms. Windows stores:

```json
{"version":1,"kind":"windows","volume":"0123456789abcdef","file_id":"0123456789abcdef0123456789abcdef"}
```

The volume is the 64-bit volume serial and the ID is the full 128-bit file ID from
`GetFileInformationByHandleEx(FileIdInfo)`, encoded as hex without SQLite integer
overflow. Root identity is observed through opened handles at registration and
must match at every subsequent live access. Platforms and unknown identity versions
cannot be substituted.

POSIX stores decimal device/inode strings in the same versioned shape with
`kind="posix"`. Registration requires an observed identity; there is no fallback
to transitional columns or migration-time reauthorization. Do not share a registration
or database between Windows and WSL to reinterpret root identity. See the
[first-release schema baseline](development.md#migrations).

## Windows authorization

The focused stdlib `ctypes` wrapper uses `CreateFileW(OPEN_EXISTING)`,
`FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS`, `GetFileType`,
`GetFileInformationByHandle`, `GetFileInformationByHandleEx`,
`GetFinalPathNameByHandleW`, `GetVolumeInformationByHandleW`, `GetDriveTypeW`,
`CompareStringOrdinal` and `ReadFile`. No native dependency is added.

Every ancestor, starting at the drive root, is opened and retained during access.
Handles permit read sharing only: **write and delete sharing are denied**. A writer
already holding conflicting access makes the operation fail; a subsequent writer
cannot replace, rename or reparse an opened ancestor or edit/replace an opened file.
This makes full-path Win32 child opens safe while their entire ancestry is pinned.
The opened object's identity, attributes and final normalized DOS path are checked,
including after access and early traversal closure. A file observation/open identity
mismatch or observable size/write-time/attribute/link change rejects the read.
No result is accepted before the enclosing root checks finish.

The final path is compared by Windows ordinal ignore-case equality, not string
prefix matching or Unicode `casefold`. Containment is established by locked parent
objects and validated individual child components. A volume change is rejected.
Case-sensitive directories are rejected using `FileCaseSensitiveInfo`.

All reparse points are denied for explicit access and registration: symlinks,
junctions, volume mount points, cloud placeholders and internal aliases alike.
Automatic traversal observes but skips reparse entries; it never follows them.
Unsupported object/identity errors abort scans rather than masquerading as deletions.
Windows hard-linked files (link count other than one) are also unsupported.

| Construct | Windows v1 policy |
| --- | --- |
| Ordinary fixed local NTFS drive root | Supported on modern Windows 10/11 with required identity/case APIs |
| Ordinary nested regular files and case differences | Supported; exact opened identity remains authoritative |
| Project-relative tool paths | Forward slashes; absolute paths, drives, backslashes, `..` and controls denied |
| Registration paths | Ordinary drive-absolute paths or simple relative paths; forward/backslash drive syntax accepted |
| UNC, SMB, device/NT namespace, user-supplied `\\?\` | Denied; internal extended DOS paths are used only after validation |
| NTFS alternate data streams | Denied by rejecting `:` in every child component |
| Reserved devices, wildcards, trailing dots/spaces | Denied before Win32 normalization |
| DOS 8.3 aliases | Denied when opened final name differs from the requested name |
| ReFS, FAT, removable/network filesystems, case-sensitive directories | Unsupported; no downgrade |
| Symlinks, junctions, other reparse points, multi-linked files | No traversal or reads; unsupported |

The existing case-insensitive sensitive-file policy remains in force, including
`.env*`, credential locations and private-key names/suffixes. Windows automatic
directory exclusions also ignore case, so `BUILD`/`NODE_MODULES` cannot bypass them.
Both policies and cached Agent Index filters use Win32 ordinal comparisons, so
Unicode aliases recognized by Windows cannot bypass Python lowercase matching.
Index scans use the same handles and Windows automatic/sensitive policy as tools.
Python source reads are capped at 2 MiB on both backends; an oversized source aborts
refresh and rolls back, preserving the previous snapshot. Cached queries and stale
parse-failure behavior remain as before. Source is never executed.

## Windows Git

Git does not open the original Windows worktree. A private, bounded snapshot is
built through authorized handles while the registered root remains pinned:

1. Find a normal `.git` directory along the locked ancestry. This permits a
   registered `C:\repo\allowed` inside a larger `C:\repo` repository.
2. Copy only HEAD, index, packed refs, shallow state, ordinary refs, SHA-1 loose
   objects and pack/idx/rev files. This is a narrow Git metadata exception, never
   authorization to read sibling working files. Reparse metadata, gitfiles,
   `commondir`, object alternates and HTTP alternates are refused.
3. Copy only public, automatically eligible Project working files under the same
   original subproject prefix. Sibling worktree files and sensitive files are absent.
4. Create a fixed config instead of copying repository config, includes, hooks,
   filters, fsmonitor, info attributes or global/system config. Scratch directories
   are pinned before writes, files are exclusively created and hash-checked when
   reopened, and all copied objects are held against write/delete during Git calls.
5. Run the existing fixed commands with an absolute `git.exe`, `shell=False`,
   sanitized environment, disabled optional locks/pagers/network/lazy fetch/hooks/
   submodules/external diff/textconv, two-second subprocess timeout and bounded
   concurrent pipe draining. No configured helpers can execute; timeout/output
   overflow terminates the single Git process. Delete scratch state on root exit.

Executable lookup examines only absolute PATH entries, never the implicit current
directory, and refuses candidates inside the original worktree. Standard Git for
Windows `cmd`/`bin` PATH entries resolve to the installed `mingw64/bin/git.exe` or
`mingw32/bin/git.exe` command rather than its launcher. The executable and its
ancestry are also pinned with no-reparse handles for the operation. Git installation
paths must therefore be ordinary local NTFS paths; junction-based installations
are unsupported. PATH and the executable installation remain trusted runtime
configuration, and custom process launchers are outside the timeout contract.

Returned status/index/grep paths are validated again, stripped by **component**
containment against the original subproject prefix and filtered by Windows path,
exclusion and sensitive policies. Renames are disabled, so an outgoing cross-boundary
move appears only as a scoped deletion. An incoming destination now inside the
Project is authorized. Grep searches regular-file cached index contents; unstaged
diffs compare indexed blobs with the live handle-authorized Project files.
Staged diff additionally validates expanded NUL-delimited file names and regular
file modes against the exact approved paths before releasing a patch, so a
directory-to-file transition cannot reveal sensitive HEAD descendants, symlink
targets or submodule content. The pinned snapshot keeps that validation and patch
generation on the same metadata.

Snapshot limits are 256 MiB total copied bytes, 100,000 visited entries, 128 MiB per
metadata file and 2 MiB per source file; overflow fails with no partial Git result.
Large object databases may exceed this v1 limit. Only ordinary SHA-1 worktrees with
an in-place `.git` directory are supported. Bare repositories, linked worktrees,
split/external indexes, custom repository extensions and redirected object stores
are outside this backend's support. Repository-specific filter/EOL/config and
`.git/info/exclude` semantics are deliberately not inherited; status observes the
isolated fixed configuration and copied `.gitignore`/`.gitattributes` data. This
can differ from an administrator's configured Git CLI. Source tools still work when
Git is unavailable; Git inspection remains best effort.

## Threat model and validation

Repository content and model output cannot choose a root, expand permissions or
run arbitrary commands. The boundary covers path escape, reparse traversal, ordinary
root/directory/file replacement and detectable read changes. Like POSIX, it is not
an atomic whole-repository snapshot, an OS sandbox against a malicious process
with the control-plane user's privileges, or protection against privileged kernel,
mount/volume manipulation, file-ID reuse, or modifications through preexisting
writable memory maps that leave no observable metadata change. The executable
installation, database, user-owned private TEMP directory and runtime state must
remain trusted; TEMP must itself be on supported NTFS with ordinary ancestors.
Competing access/unsupported Windows APIs fail closed.

POSIX regressions run unchanged on POSIX. Platform-independent tests exercise
lexical validation, wrapper signatures/structure widths, mocked opened identity,
reparse policy, races, rollback, snapshot composition and real offline Git output.
Mock tests are **not native Windows integration evidence**. Tests marked `windows`
run real Win32 APIs, NTFS locks/junctions/ADS, shared source/Git security contracts
and MCP with scripted inference; POSIX-specific regression seams are marked `posix`.
No automated test needs Ollama, a GPU or networking. See
[native Windows smoke](native-windows-smoke.md) for actual acceptance.


Repo Explorer uses the read-only backend and snapshot contract. The opt-in
[coding backend](coding.md) reuses Win32 identity, path, volume, ordinal-case and
pinned-ancestry primitives, adding exclusive mutation handles and manager-owned
no-checkout worktree provisioning. Windows rewrites are durable handle operations,
not crash-atomic replacements. Native Windows coding/Job integration requires
execution on Windows; deterministic Linux mocks are not native evidence.
