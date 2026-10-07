# Projects, Index and read-only repository tools

`projects.models.Project` is immutable configuration: a generated UUID, a
human-readable name, a canonical absolute local root and a UTC creation timestamp.
Names may repeat; canonical roots may not. Identity does not depend on a mutable
name or path. A normal directory is a valid project; Git is optional. Core contains
no project-specific assumptions.

`projects.service.ProjectRegistry` exposes synchronous, transport-independent
`register_project(name, root_path)`, `list_projects()`, `get_project(id)`,
`remove_project(id)` and `inspect_project(id)`. IDs accept UUIDs or UUID strings.
Removal deletes registration and its cached index, never repository files.
Configuration remains retrievable/removable when its directory is unavailable;
inspection validates that the root still exists.
Important failures have explicit `ProjectError` subclasses, including invalid
paths/names, duplicate roots, missing IDs, unsafe candidates and storage failures.
SQLAlchemy errors and database statements are not exposed as service errors.

Relative registration paths use the registry's explicit `base_directory`, or the
working directory captured at construction. POSIX registration follows symlinks
and requires an existing directory, so aliases of the same directory collide.
Windows registration rejects every reparse ancestor and ambiguous namespace/path
construct. The stored canonical root is the security boundary.
`resolve_path(id, candidate)` delegates to the platform backend. The original
POSIX `projects.paths.resolve_project_path` helper resolves existing candidates
and checks `Path.is_relative_to` against the resolved root. Windows validates
relative components and opens them under identity-checked, pinned ancestry;
absolute candidates and reparse aliases are denied. A stored root replaced by a
symlink to another location is rejected. These helpers return point-in-time paths,
not I/O capabilities. Execution uses `open_root`, not `resolve_path`. `open_root(id)` supplies capabilities for descriptor-anchored POSIX
access and persists directory device/inode identity. Windows uses the same
Registry entrypoint with opened handles and volume/file identity. Migration
`0003_project_root_identity` leaves POSIX fields null for
legacy registrations: live Registry inspection, Index refresh/status scans and
repository tools fail closed until those projects are removed and re-registered.
Configuration retrieval/removal and cached Index queries remain available.
Migration never observes or authorizes a replacement directory. Creation semantics
for nonexistent paths remain unimplemented.

`inspect_project` returns `ProjectInspection(project, git)`. `GitMetadata` is a
fresh, nonpersisted observation with an observation timestamp, discovery status,
repository/worktree root, optional branch and optional HEAD commit. Status is
`repository`, `not_repository` or `unavailable`; unavailable discovery does not
assert that the directory is non-Git. Detached HEAD has no branch; an unborn
repository has no commit. Git failures/timeouts are best effort and do not prevent
registration. `inspect_project` first opens the identity-checked registered root
through `open_root`; boundary failures propagate Project errors, not best-effort
Git metadata. Inspection uses bounded, read-only `rev-parse`/`symbolic-ref` calls
with the verified descriptor inherited as Linux `/proc/self/fd` cwd, a sanitized
Git environment, disabled optional locks and no network or repository mutation.
On POSIX without descriptor-backed Git cwd, Git is observationally `unavailable`;
no pathname fallback may inspect a replacement. Windows instead copies approved
Git metadata and Project working files through locked handles into a pinned,
bounded private snapshot, then executes the same fixed Git commands there.
Separate reads are not an atomic snapshot of a concurrently changing repository.
An observed Git root may be an
ancestor of a registered subdirectory; it never expands the approved boundary.

`db.database` owns explicit SQLAlchemy 2 engine/session construction;
`db.models.ProjectRecord` owns the `projects` table. `db.projects.ProjectRepository`
maps records to domain values and owns short-lived sessions/transactions, a unique
canonical-root constraint and error translation. The service currently uses this
small concrete storage adapter; domain models, path helpers and Git inspection
have no SQLAlchemy dependency. No generic repository framework is introduced.
SQLite is the initial backend; UUID and timezone-aware timestamp columns use
SQLAlchemy types. SQLite timestamps are interpreted as UTC when read.

Schema changes are explicit Alembic operations, never registration/startup side
effects. From the repository root in the development venv (migrations are packaged under
`src/agentforge/db/migrations`):

```sh
alembic upgrade head
alembic revision --autogenerate -m "Describe the schema change"
alembic check
```

`alembic.ini` defaults to the ignored local `agentforge.db`; set `sqlalchemy.url`
in a local Alembic configuration beside the root config for another database. Pass the same URL to
`create_database_engine`, then wire `create_session_factory(engine)` →
`ProjectRepository` → `ProjectRegistry`. Dispose the engine when its owner shuts
down. The initial revision creates only project configuration. Tests migrate
temporary SQLite databases and verify upgrade/downgrade and database reopening.

The current Registry stores name, root and identity only; it has no per-Project
validation profile, permissions editor or generated architecture summary.

## Deterministic Python Index

`index.service.ProjectIndex` consumes the existing `ProjectRegistry` and
`db.index.IndexRepository`. Wire both repositories with a session factory for the
same migrated database. The service is synchronous and transport-independent:
`refresh_index(id)`, `get_index_status(id)`, `find_symbol(id, query)`,
`get_symbol(id, symbol_id)`, `get_dependencies`, `get_dependents`,
`get_related_symbols`, `get_relationships` and
`render_project_map(id, focus=None, max_tokens=3000)`. Index operations themselves
involve no HTTP/MCP adapter or Worker; the Agent wrappers consume these
public queries for structural navigation.

The boundary is Python bytes → built-in AST → small immutable extraction records
→ SQLite → queries/maps. `index.python_parser` owns definitions, line locations,
lexical containment and import/call facts; it never executes source. Its
`parse_python(relative_path, bytes) -> ParsedFile` boundary allows another parser
later without a plugin framework or storage redesign. `index.scanner` uses shared
`projects.backends` platform capabilities and `projects.exclusions` for safe
reads/traversal.
`db.index` owns transaction-scoped replacement and link
resolution; `index.render` owns deterministic relevance and bounded rendering.
No LLM, embeddings, vector database or graph library builds structural facts.

Alembic revision `0002_project_index` follows the shipped `0001_projects` revision.
Four small tables store project snapshot metadata, eligible files, symbols and
relationships. Files carry observed and last successfully parsed SHA-256 hashes;
raw source/bodies are not persisted. POSIX Index caches can contain structure from
sensitive filenames; Agent wrappers filter cached paths again before model exposure. Module symbols act as containment roots and
import targets; only unambiguous, unshadowed module-level definitions are eligible
definition import targets. Definition IDs hash the relative path, kind, lexical
qualified name and start line, so duplicate and nested definitions stay distinct.
IDs can change when definitions move. IDs are scoped to a Project, with no
cross-project semantic identity. Module names follow paths relative to the
registered root, with package `__init__.py` mapped to its directory. A root
initializer uses the display namespace `__root__`, without assuming an absolute
package name; its relative imports of indexed child modules can still link.
The index does not infer Python installation layouts, `sys.path` or `src/` roots.

Refresh explicitly scans `.py` files one at a time, compares content hashes and
reuses unchanged extraction records. New/changed files replace their structure;
deleted files are removed. Import links are recomputed from cached facts against
the complete current symbol set, including unchanged importers. A fresh registry
Git observation records HEAD when available, but never determines freshness:
dirty working trees and non-Git projects use the same hash checks.
`get_index_status` performs an explicit live hash scan and reports changed paths,
counts, snapshot time, observed HEAD and parse failures. Other queries read cached
structure; callers check status or request refresh when they need current data.

One database transaction covers the complete refresh. Filesystem, unexpected
parser or storage failures roll back all changes. Invalid Python syntax/encoding
is a per-file failure: its observed hash and a safe diagnostic are stored, while
its previous valid symbols/relationships remain available, flagged `stale` and
marked in maps. A newly invalid file has no structural records. Unchanged invalid
files retain their diagnostic without reparsing; edits retry parsing. Status keeps
reporting these failures, so a partial index is never presented as fully current.
SQLite connections enable foreign keys and transaction control covering reads;
cascades clean file-owned facts and all index tables when the registry removes a
Project. Resolved target IDs are checked and rebuilt transactionally rather than
defining a generic graph ORM.

The Registry's `open_root(project_id)` is authoritative for both Index filesystem
operations and repository tools: it validates the canonical root and its persisted
platform filesystem identity. `ProjectIndex._scan` keeps that context open while
its scanner consumes the verified directory capability. Windows retains handles
for every ancestor before full-path child opens; POSIX keeps descriptor-relative
opens. Refresh consumes the scan, including root exit validation, inside the database transaction
so replacement during parsing rolls back changes. `inspect_project` checks the
same identity before refresh's Git observation. Legacy NULL identity rows require
re-registration for live refresh/status scans; cached symbols, relationships and
maps need no live root authorization and remain queryable.
Traversal skips **all** symlinks (including internal aliases), special
files and fixed cache/build/vendor/IDE/secret directories, including `.git`, virtual
environments, `site-packages`, `node_modules`, `dist`, `build`, `vendor`,
`third_party` and `.secrets`; no configurable ignore engine is added. POSIX
no-follow descriptors anchor root ancestors, directory traversal and regular-file
reads, closing the validation/I/O symlink race. Windows uses no-follow NTFS handles
with write/delete sharing denied and the same automatic policy, case-insensitively,
including sensitive paths. Python source reads are capped at 2 MiB; exceeding the
cap rolls back refresh. Unsupported capabilities fail closed.
Root/directory replacement and changes during a file read abort refresh. The
repository is strictly read-only, and index operations never access the network.
As with Git inspection, a changing filesystem is not an atomic source snapshot;
explicit hash checks/refresh detect subsequent edits.

Relationships retain their kinds and direction: `contains`, `imports`, `calls`.
Dependencies are outgoing resolved edges, dependents incoming resolved edges, and
related symbols return their union as typed edges. `get_relationships` also exposes
unresolved textual imports/simple calls with a null target. Imports link only
unambiguous root-relative indexed modules/definitions; external imports, wildcards,
unknown exports and ambiguous names are not guessed. Call links describe syntactic
lexical targets, not guaranteed runtime dispatch: unique plain local definitions
and straightforward `self.method()` within a plain class without bases, metaclass,
class decorators or custom attribute lookup. Parameters, assignments,
imports, duplicate/conditional/decorated definitions and explicit receiver/method
rebinding block uncertain links. Arbitrary `obj.method()`, inherited dispatch,
imported-alias calls, lambdas/comprehensions, dynamic exports, monkeypatching and
full type inference are deliberately unsupported. No general reference analysis
or graph path operation is implemented. `find_symbol` returns all case-insensitive
exact simple/qualified matches, falling back to qualified-name substrings; it never
selects one ambiguous definition silently.

Maps contain paths, classes/functions/methods and start lines, without bodies or
AI summaries. Focus ranks exact names, qualified names, substrings/path matches
and direct import/call neighbors. General maps use relationship degree, top-level
definition counts and shallow paths before stable lexical tie-breaks. Rendering
uses whole structural lines, with necessary parent context, and a strict UTF-8
byte cap of `3 * max_tokens`; `ceil(bytes / 3)` is the documented approximate token
estimator, not a model-tokenizer guarantee. Zero or too-small budgets return an
empty map; negative/noninteger budgets are rejected. Repo Explorer requests exact source regions from these paths/line spans. The Index itself
does not execute Tasks, route Workers or generate architecture summaries.

## Read-only repository tools

The Task Engine validates explicit bindings, persists execution requests and
lifecycle state, coordinates bounded execution, and makes outcomes retrievable.
See [Tasks and telemetry](tasks.md) for lifecycle and recovery.
Infrastructure errors are reported to the director; automatic model fallback
would violate explicit worker selection.

`tools.service.RepositoryTools(ProjectRegistry)` supplies synchronous,
transport-independent `list_files`, `read_file`, `search_code`, `git_grep`,
`git_status` and `git_diff`. Every operation requires a registered Project UUID;
there is no arbitrary root/path API, generic command runner, shell, write tool,
MCP wrapper or execution loop inside the service. Agent tool wrappers call these methods
without duplicating their implementations. Tools do not require a refreshed index.
The Index supplies symbols, relationships and maps; tools supply exact source and Git state.

Filesystem authorization remains the registry's canonical Project root, using
`open_root(id)` and a shared platform security backend for root ancestors, traversal
and regular-file reads. Paths must be project-relative; absolute paths, `..`,
backslashes, drive/pathspec syntax and control characters are rejected. Tools
reject all symlinks (including internal aliases), directories as file reads and
special files. File changes during reads and directory/root replacement fail
closed, including early traversal termination at a result limit. Persisted
device/inode (POSIX) or volume/file (Windows) identity catches ordinary root
replacement across service/database reopening. Containment uses path components
and opened identity, never string-prefix comparisons.
POSIX uses no-follow descriptors; Windows uses local NTFS handles and denies reparse
points, ambiguous path constructs and conflicting write/delete access. Unsupported
systems fail closed.
Privileged mount manipulation and inode reuse are beyond this filesystem boundary.

Automatic listing/search reuse the Index's fixed generated/cache/vendor directory
exclusions. There is no configurable ignore engine. They additionally omit a small,
case-insensitive sensitive-path policy: `.env`, all `.env.*` (including examples),
`.secrets`, `.ssh`, `.aws`, `.gnupg`, `.git`/`.hg`/`.svn`, `.netrc`, `.npmrc`,
`.pypirc`, `credentials.json`, common `id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519`
key names, and `.pem`/`.key`/`.p12`/`.pfx` suffixes, anywhere under the root.
Explicit reads may inspect otherwise excluded generated files, but sensitive
paths are always denied by every tool, without content in errors. This is
conservative defense-in-depth, not secret classification or DLP.

File reads accept optional inclusive 1-based start/end lines and return requested
and returned ranges, text and a truncation flag. Decoding is strict UTF-8 with
control-character rejection (except tab/CR/LF); no encoding guessing, tokenizer,
base64, media extraction or execution occurs. A bounded prefix is inspected,
so this is not a claim that an unseen tail is textual. Listings and searches have
stable lexical path/line ordering. `search_code` is literal, optionally casefolded,
and supports directory scope and a `fnmatchcase` glob over relative paths;
non-text files are skipped and counted. Snippet truncation is explicit on each
match; result truncation means the requested scan/output could not be completed.

| Budget | Default | Hard upper bound |
| --- | --- | --- |
| Returned UTF-8 bytes (all potentially large tools) | 64 KiB | 1 MiB |
| File listing / Git status entries | 1,000 | 10,000 |
| Returned file lines | 1,000 | 10,000 |
| Search / Git grep matches | 100 | 1,000 |
| Matching-line snippet bytes | 500 | 2,000 |
| Inspected source/index blob prefix per file | 2 MiB | fixed 2 MiB |
| Files / bytes inspected by filesystem search | 100,000 / 64 MiB | fixed |
| Changed paths considered by a diff | 100 | fixed |
| Lines compared per file in an unstaged diff | 10,000 | fixed |
| Git metadata capture / stderr capture | 64 KiB / 8 KiB | fixed |
| Git subprocess timeout | 2 seconds | fixed per subprocess |

Entry/match byte budgets account for paths as well as content. File range bounds
are limited to 1,000,000. Truncation is returned whenever a result, scan or diff
limit prevents completeness; incomplete oversized diff inputs are skipped rather
than treated as complete files. UTF-8 truncation never emits a split character.
Filesystem scans process one file at a time; directory names are sorted in memory.
Separate filesystem and Git observations are not an atomic snapshot.

Git tools require a Git worktree. Linux uses `/proc/self/fd` to pin subprocess cwd;
Windows uses an isolated bounded Git snapshot copied through authorized handles.
Absence, unavailable Git, timeout and backend failure are separate domain errors.
A private backend uses fixed explicit argv, `shell=False`, bounded concurrently
drained stdout/stderr, process-group termination and sanitized shared Git
configuration. Inherited `GIT_*` redirection is removed; global/system config,
optional locks, network transports, lazy fetch, pagers, fsmonitor, hooks, external
diff/textconv and submodule recursion are disabled. Configured clean/smudge/process
filters are discovered by name and overridden before worktree status. Git metadata
must remain stable during an observation; these calls are not an OS process sandbox
against another process concurrently rewriting Git configuration.

`git_grep` deliberately searches regular-file **index contents** with `--cached`,
not untracked or unstaged text, to avoid worktree symlink races. It accepts literal
queries and safe relative paths only, with no caller Git options/pathspec magic.
`git_status` parses NUL-delimited porcelain v1, disables rename expansion, and
returns branch/detached state plus staged, unstaged, untracked and conflict facts.
`git_diff(staged=True)` checks expanded raw paths and regular file modes against
the exact allowed path list before producing a bounded patch. Directory-to-file
transitions cannot disclose sensitive HEAD descendants; rename expansion is disabled.
On POSIX, Git metadata must stay trusted between the checks; Windows pins a snapshot.

Unstaged diffs compare validated indexed blobs against descriptor-safe working
files with stdlib `difflib`. Binary changes get a textual unsupported marker,
metadata-only changes a marker, and conflict/oversized input omission marks the
result incomplete. Symlink reads fail closed; submodule content is omitted.
There is no historical revision API, network access or repository mutation.

For registered `/repo/allowed` inside Git root `/repo`, every Git request has a
literal project-relative scope. Captured root-relative names are converted by
component containment, filtered by exclusion/sensitive policy and returned as
Project-relative paths. Grep/diff receive only eligible scoped paths; rename
expansion is disabled so a move across the boundary cannot reveal sibling content.
Git discovery never authorizes `/repo/secret`. Synthetic tests cover all three
Git tools on this subdirectory boundary and verify byte-for-byte read-only behavior.
Public errors reuse Project identity/path failures and add invalid argument,
missing file, unsupported text, sensitive path, I/O, non-Git, unavailable Git,
timeout and safe backend failure types; OS/SQL/Git stderr details are not exposed.
