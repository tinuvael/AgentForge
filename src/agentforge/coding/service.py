"""One durable owner for coding worktree provisioning, inspection and cleanup."""

import difflib
import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import UUID

from agentforge.coding.filesystem import (
    CodingDirectory,
    CodingPosixBackend,
    mutate,
    parts_for,
    remove_tree,
)
from agentforge.coding.git import WorkspaceGit, pinned_repository
from agentforge.coding.models import (
    CodingDiff,
    CodingError,
    CodingLimit,
    CodingResult,
    ValidationRun,
)
from agentforge.coding.process import validation_process
from agentforge.db.coding import WorkspaceRepository
from agentforge.projects.identity import RootIdentity
from agentforge.projects.windows import WindowsDirectory, WindowsSafeFilesystemBackend
from agentforge.tools import filesystem as fs
from agentforge.tools.errors import InvalidToolArgument, UnsupportedTextFile
from agentforge.tools.policy import clip
from agentforge.tools.service import RepositoryTools


def _identity(value):
    return RootIdentity.from_json(value)


def _mkdir(backend, parent, name):
    backend.validate_parts((name,))
    if isinstance(parent, WindowsDirectory):
        backend._verify(parent)
        os.mkdir("\\\\?\\" + str(Path(parent.path) / name))
    else:
        os.mkdir(name, 0o700, dir_fd=parent.fd)


class CodingWorkspaceManager:
    def __init__(
        self, projects, repository: WorkspaceRepository, config, *, filesystem=None
    ):
        self.projects, self.repository, self.config = projects, repository, config
        if filesystem is not None:
            self.filesystem = filesystem
        elif os.name == "nt":
            from agentforge.coding.win32 import Win32Mutation

            self.filesystem = WindowsSafeFilesystemBackend(Win32Mutation())
        else:
            self.filesystem = CodingPosixBackend()
        # Require a pre-created private operator root; never guess a writable
        # directory or accept a model/caller parent. Opening rejects symlink ancestry.
        self.parent = config.workspace_parent
        self.parent_identity = self.filesystem.observe_root(self.parent)
        with self.filesystem.anchored_root(self.parent, self.parent_identity):
            if os.name != "nt":
                info = self.parent.stat()
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise CodingError("Coding workspace parent must be private (0700)")
        if not config.git_executable.is_file():
            raise CodingError("A pinned Git executable is required")

    def _record(self, task_id):
        return self.repository.get(UUID(str(task_id)))

    def _paths(self, row):
        expected = self.parent / str(row["project_id"]) / str(row["task_id"])
        if (
            str(expected) != row["worktree_path"]
            or row["workspace_id"] != row["task_id"]
            or row["branch_name"] != "agentforge/task-" + str(row["task_id"])
            or _identity(row["identities"]["parent"]) != self.parent_identity
        ):
            raise CodingError("Coding workspace ownership does not match")
        prefix = row["prefix"]
        if prefix:
            parts_for(self.filesystem, prefix, public=False)
        return expected, expected.joinpath(*prefix.split("/")) if prefix else expected

    def _primary(self, project_id):
        project = self.projects.get_project(project_id)
        # Discovery by opened ancestry, never Git config-provided --show-toplevel.
        with self.projects.open_root(project_id):
            for candidate in (project.root_path, *project.root_path.parents):
                identity = self.filesystem.observe_root(candidate)
                with self.filesystem.anchored_root(candidate, identity) as node:
                    try:
                        observed = self.filesystem.stat(node, ".git")
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISDIR(observed.st_mode):
                        raise CodingError(
                            "Coding requires an ordinary primary .git directory"
                        )
                    prefix = project.root_path.relative_to(candidate).as_posix()
                    prefix = "" if prefix == "." else prefix
                    if prefix:
                        parts_for(self.filesystem, prefix, public=False)
                    return candidate, identity, prefix
        raise CodingError("Coding requires a committed Git repository")

    @contextmanager
    def _registration(self, row):
        target, _ = self._paths(row)
        with pinned_repository(
            self.filesystem,
            Path(row["repository_path"]),
            _identity(row["identities"]["repository"]),
            self.config.git_executable,
        ) as (repo, meta, _):
            with self.filesystem.directory(
                meta, ("worktrees", row["identities"]["admin_name"])
            ) as admin:
                path = (
                    Path(row["repository_path"])
                    / ".git/worktrees"
                    / row["identities"]["admin_name"]
                )
                if self.filesystem.observe_root(path) != _identity(
                    row["identities"]["admin"]
                ):
                    raise CodingError("Coding Git registration was replaced")
                pointer, partial = fs.read_bytes(admin, "gitdir", 4096)
                common, common_partial = fs.read_bytes(admin, "commondir", 32)
                if (
                    partial
                    or common_partial
                    or common.strip() != b"../.."
                    or Path(pointer.decode("utf-8").strip()) != target / ".git"
                ):
                    raise CodingError("Coding Git registration association changed")
                yield repo, admin

    @contextmanager
    def _git(self, row, *, workspace=False):
        if workspace:
            root, _ = self._paths(row)
            with self._registration(row) as (repo, admin):
                with self.filesystem.anchored_root(
                    root, _identity(row["identities"]["worktree"])
                ) as tree:
                    yield WorkspaceGit(
                        self.config.git_executable, repo, admin, worktree=tree
                    )
        else:
            with pinned_repository(
                self.filesystem,
                Path(row["repository_path"]),
                _identity(row["identities"]["repository"]),
                self.config.git_executable,
            ) as (_, _, git):
                yield git

    def create(self, task):
        # A duplicate durable Task may never acquire a second workspace, including
        # provisioning failures/process loss. The executor does not resume it.
        if not isinstance(task.task_id, UUID) or not isinstance(task.project_id, UUID):
            raise CodingError("Coding requires validated Task and Project UUIDs")
        try:
            self._record(task.task_id)
        except CodingError:
            pass
        else:
            raise CodingError("Task already owns a coding workspace")
        provisioning_started = monotonic()
        repo_path, repo_identity, prefix = self._primary(task.project_id)
        if self.config.git_executable.is_relative_to(
            repo_path
        ) or self.config.git_executable.is_relative_to(self.parent):
            raise CodingError("Git executable must be outside source/workspace roots")
        if (
            self.parent == repo_path
            or self.parent.is_relative_to(repo_path)
            or repo_path.is_relative_to(self.parent)
        ):
            raise CodingError(
                "Coding workspace parent must be separate from the repository"
            )
        # Also prevent operator-selected storage from living inside another Project.
        for project in self.projects.list_projects():
            if self.parent == project.root_path or self.parent.is_relative_to(
                project.root_path
            ):
                raise CodingError("Coding workspace parent overlaps a Project")
        branch = "agentforge/task-" + str(task.task_id)
        target = self.parent / str(task.project_id) / str(task.task_id)
        identities = {
            "parent": self.parent_identity.as_json(),
            "repository": repo_identity.as_json(),
        }
        with self.filesystem.anchored_root(self.parent, self.parent_identity) as parent:
            try:
                _mkdir(self.filesystem, parent, str(task.project_id))
            except FileExistsError:
                pass
            with self.filesystem.directory(
                parent, (str(task.project_id),)
            ) as directory:
                identities["project_parent"] = self.filesystem.observe_root(
                    target.parent
                ).as_json()
                try:
                    self.filesystem.stat(directory, str(task.task_id))
                except FileNotFoundError:
                    pass
                else:
                    raise CodingError("Unowned workspace path already exists")
                with pinned_repository(
                    self.filesystem,
                    repo_path,
                    repo_identity,
                    self.config.git_executable,
                ) as (_, _, git):
                    base = git.head()
                    collision = git.complete(
                        ["show-ref", "--verify", "--quiet", "refs/heads/" + branch],
                        128,
                        allow_failure=True,
                    )
                    if collision.returncode != 1:
                        raise CodingError("Coding branch collision or unavailable refs")
                    tree = git.tree(base, prefix)
                    full_tree = git.tree(base, "")
                    self.repository.add(
                        task_id=task.task_id,
                        workspace_id=task.task_id,
                        project_id=task.project_id,
                        worker_id=task.worker_id,
                        branch_name=branch,
                        base_commit=base,
                        created_at=datetime.now(UTC),
                        state="provisioning",
                        repository_path=str(repo_path),
                        worktree_path=str(target),
                        prefix=prefix,
                        identities=identities,
                        observations={},
                    )
                    try:
                        # Never checkout: no hooks/filters/LFS and no primary
                        # index, HEAD or worktree-file operations.
                        git.complete(
                            [
                                "worktree",
                                "add",
                                "--no-checkout",
                                "-b",
                                branch,
                                "--",
                                str(target),
                                base,
                            ],
                            8192,
                        )
                        identities["worktree"] = self.filesystem.observe_root(
                            target
                        ).as_json()
                        with self.filesystem.anchored_root(
                            target, _identity(identities["worktree"])
                        ) as root:
                            gitfile, partial = fs.read_bytes(root, ".git", 4096)
                            if partial or not gitfile.startswith(b"gitdir: "):
                                raise CodingError(
                                    "Coding worktree Git association is unavailable"
                                )
                            admin_path = Path(gitfile.decode("utf-8").strip()[8:])
                            if admin_path.parent != repo_path / ".git" / "worktrees":
                                raise CodingError("Unexpected worktree Git association")
                            parts_for(self.filesystem, admin_path.name, public=False)
                            identities["admin_name"] = admin_path.name
                            identities["admin"] = self.filesystem.observe_root(
                                admin_path
                            ).as_json()
                            self.repository.update(task.task_id, identities=identities)
                            total = 0
                            materialized = set()
                            # Internal materialization copies raw blobs only within
                            # the registered subtree. Special Git objects stay absent.
                            for path, (mode, kind, object_id) in sorted(tree.items()):
                                if monotonic() - provisioning_started > 60:
                                    raise CodingLimit(
                                        "Coding provisioning time budget exceeded"
                                    )
                                parts_for(self.filesystem, path, public=False)
                                if mode not in {"100644", "100755"} or kind != "blob":
                                    continue
                                blob = git.complete(
                                    ["cat-file", "blob", object_id], 2_097_152
                                ).data
                                total += len(blob)
                                if total > 256 * 1024 * 1024:
                                    raise CodingError(
                                        "Coding materialization byte limit exceeded"
                                    )
                                relative = (prefix + "/" if prefix else "") + path
                                mutate(
                                    root,
                                    relative,
                                    blob,
                                    expected=None,
                                    create=True,
                                    mkdir=True,
                                    public=False,
                                    provision_mode=0o700 if mode == "100755" else 0o600,
                                    check=lambda: self._check_root(
                                        target, identities["worktree"]
                                    ),
                                )
                                materialized.add(relative)
                            if prefix:
                                self._ensure_subtree(root, prefix)
                        _, tool_root = self._paths(self._record(task.task_id))
                        identities["tool_root"] = self.filesystem.observe_root(
                            tool_root
                        ).as_json()
                        self.repository.update(task.task_id, identities=identities)
                        row = self._record(task.task_id)
                        with self._git(row, workspace=True) as worktree_git:
                            worktree_git.complete(["read-tree", base], 128)
                            # A sparse materialization must not look like sibling
                            # deletion to ordinary Git inspection outside tools.
                            # Special Git entries also remain absent and untouched.
                            worktree_git.skip_absent(full_tree.keys() - materialized)
                        with self.projects.open_root(task.project_id):
                            pass
                        self.repository.update(task.task_id, state="ready")
                    except BaseException:
                        self.repository.update(
                            task.task_id, state="failed", identities=identities
                        )
                        raise
        return self.get(task.task_id)

    def _ensure_subtree(self, root, prefix):
        from agentforge.coding.filesystem import _parent

        with _parent(root, tuple(prefix.split("/")), lambda: None, mkdir=True):
            pass

    def _check_root(self, path, expected):
        with self.filesystem.anchored_root(self.parent, self.parent_identity):
            with self.filesystem.anchored_root(path, _identity(expected)):
                pass

    @contextmanager
    def _worktree_root(self, row):
        worktree, _ = self._paths(row)
        if not all(
            key in row["identities"]
            for key in ("project_parent", "worktree", "admin_name")
        ):
            raise CodingError("Coding worktree ownership is incomplete")
        with self.filesystem.anchored_root(self.parent, self.parent_identity):
            with self.filesystem.anchored_root(
                worktree.parent, _identity(row["identities"]["project_parent"])
            ):
                with self.filesystem.anchored_root(
                    worktree, _identity(row["identities"]["worktree"])
                ) as root:
                    gitfile, partial = fs.read_bytes(root, ".git", 4096)
                    expected = (
                        Path(row["repository_path"])
                        / ".git/worktrees"
                        / row["identities"]["admin_name"]
                    )
                    if (
                        partial
                        or Path(
                            gitfile.decode("utf-8").strip().removeprefix("gitdir: ")
                        )
                        != expected
                        or not gitfile.startswith(b"gitdir: ")
                    ):
                        raise CodingError("Coding worktree Git association changed")
                    yield root

    @contextmanager
    def open_root(self, task_id):
        row = self._record(task_id)
        if (
            row["state"] in {"removed", "provisioning", "cleanup_pending", "suspicious"}
            or "tool_root" not in row["identities"]
        ):
            raise CodingError("Coding workspace is not available for tools")
        _, tool_root = self._paths(row)
        with self._worktree_root(row):
            with self.filesystem.anchored_root(
                tool_root, _identity(row["identities"]["tool_root"])
            ) as node:
                yield row, node

    def _under(self, path, entry):
        parts, prefix = path.split("/"), entry.split("/")
        return len(parts) >= len(prefix) and all(
            self.filesystem.same_component(a, b)
            for a, b in zip(parts, prefix, strict=False)
        )

    def _tree(self, row):
        with self._git(row) as git:
            return git.tree(row["base_commit"], row["prefix"])

    def diff(self, task_id, *, max_bytes=None):
        row = self._record(task_id)
        cap = min(
            max_bytes or self.config.limits.max_diff_bytes,
            self.config.limits.max_diff_bytes,
        )
        if cap < 1:
            raise InvalidToolArgument("Diff budget must be positive")
        tree = self._tree(row)
        baseline = {}
        special = set()
        for path, item in tree.items():
            parts = parts_for(self.filesystem, path, public=False)
            if item[0] not in {"100644", "100755"}:
                special.add(path)
            elif self.filesystem.automatic(parts):
                baseline[path] = item
        current = {}
        current_modes = {}
        total = visited = 0
        with self.open_root(task_id) as (_, root):
            traversal = fs.files(root, ())
            try:
                for path, parent, name in traversal:
                    visited += 1
                    if visited > 10_000:
                        raise CodingLimit("Coding inspection file limit exceeded")
                    if any(self._under(path, entry) for entry in special):
                        raise CodingError("Special Git object was replaced")
                    data, partial = fs.read_bytes(parent, name, 2_097_152)
                    if partial:
                        raise CodingLimit("Coding inspection file size exceeded")
                    total += len(data)
                    if total > 256 * 1024 * 1024:
                        raise CodingLimit("Coding inspection byte limit exceeded")
                    current[path] = data
                    info = self.filesystem.stat(parent, name)
                    current_modes[path] = (
                        "100755"
                        if (
                            isinstance(parent, CodingDirectory) and info.st_mode & 0o111
                        )
                        else "100644"
                    )
            finally:
                traversal.close()
        changed = sorted(
            path
            for path in baseline.keys() | current.keys()
            if path not in baseline
            or path not in current
            or (os.name != "nt" and current_modes[path] != baseline[path][0])
            or hashlib.sha1(
                b"blob "
                + str(len(current[path])).encode("ascii")
                + b"\0"
                + current[path]
            ).hexdigest()
            != baseline[path][2]
        )
        chunks, stats = [], []
        used = 0
        truncated = len(changed) > 100
        with self._git(row) as git:
            for path in changed[:100]:
                if used >= cap:
                    truncated = True
                    break
                before = (
                    git.complete(
                        ["cat-file", "blob", baseline[path][2]], 2_097_152
                    ).data
                    if path in baseline
                    else b""
                )
                after = current.get(path, b"")
                try:
                    old = fs.text_content(before).splitlines(keepends=True)
                    new = fs.text_content(after).splitlines(keepends=True)
                except UnsupportedTextFile:
                    lines = [f"Binary/non-text file differs: {path}\n"]
                    stats.append(f"{path} | binary/non-text")
                else:
                    if len(old) > 10_000 or len(new) > 10_000:
                        truncated = True
                        stats.append(f"{path} | line limit exceeded")
                        continue
                    lines = list(
                        difflib.unified_diff(
                            old,
                            new,
                            fromfile="a/" + path if path in baseline else "/dev/null",
                            tofile="b/" + path if path in current else "/dev/null",
                        )
                    )
                    added = sum(
                        line.startswith("+") and not line.startswith("+++")
                        for line in lines
                    )
                    deleted = sum(
                        line.startswith("-") and not line.startswith("---")
                        for line in lines
                    )
                    stats.append(f"{path} | +{added} -{deleted}")
                    if not lines:
                        lines = [
                            (
                                "Metadata-only change: "
                                if path in baseline and path in current
                                else "Empty file "
                                + ("created: " if path in current else "deleted: ")
                            )
                            + path
                            + "\n"
                        ]
                for line in lines:
                    if not line.endswith("\n"):
                        line += "\n\\ No newline at end of file\n"
                    text, partial = clip(line, cap - used)
                    chunks.append(text)
                    used += len(text.encode("utf-8"))
                    if partial:
                        truncated = True
                        break
        stat_text, stat_partial = clip("\n".join(stats), 8192)
        return CodingDiff(
            task_id=row["task_id"],
            workspace_id=row["workspace_id"],
            content="".join(chunks),
            diff_stat=stat_text,
            changed_files=tuple(changed[:100]),
            truncated=truncated or stat_partial,
        )

    def get(self, task_id):
        row = self._record(task_id)
        observations = row["observations"]
        values = {
            name: row[name]
            for name in (
                "task_id",
                "workspace_id",
                "project_id",
                "worker_id",
                "branch_name",
                "base_commit",
                "created_at",
                "state",
            )
        }
        try:
            diff = (
                self.diff(task_id)
                if row["state"]
                not in {"removed", "cleanup_pending", "provisioning", "suspicious"}
                else None
            )
        except (CodingError, OSError, ValueError, KeyError):
            diff = None
        except Exception:
            # Public metadata remains inspectable when source/Git authorization
            # fails. Never return exception details or fabricated clean state.
            diff = None
        runs = tuple(
            ValidationRun.model_validate(run)
            for run in observations.get("validation_runs", [])
        )
        return CodingResult(
            **values,
            changed_files=diff.changed_files
            if diff
            else tuple(observations.get("changed_files", [])),
            diff_stat=diff.diff_stat if diff else observations.get("diff_stat", ""),
            truncated=diff.truncated
            if diff
            else bool(observations.get("truncated", False)),
            inspection_available=diff is not None,
            termination_reason=observations.get("termination_reason"),
            validation_runs=runs,
            write_calls=observations.get("write_calls", 0),
            bytes_written=observations.get("bytes_written", 0),
            validation_duration_seconds=sum(run.duration_seconds for run in runs),
        )

    def finalize(self, task):
        row = self._record(task.task_id)
        if row["state"] in {"removed", "cleanup_pending"}:
            return
        observations = dict(row["observations"])
        observations["termination_reason"] = getattr(task, "reason", None)
        try:
            diff = self.diff(task.task_id)
        except Exception:
            observations["inspection_available"] = False
        else:
            observations.update(
                changed_files=list(diff.changed_files),
                diff_stat=diff.diff_stat,
                truncated=diff.truncated,
            )
        self.repository.update(
            task.task_id, state=task.state, observations=observations
        )

    def recover(self, tasks=None):
        # No filesystem deletion, no replay, no silent second worktree. Identity
        # verification is deferred to inspection/cleanup and suspicious roots deny.
        for task_id in self.repository.list():
            row = self._record(task_id)
            if row["state"] in {"ready", "provisioning"}:
                if tasks is not None:
                    task = tasks.get(task_id)
                    if task.state in {"completed", "cancelled"} or (
                        task.state == "failed"
                        and task.reason != "execution_interrupted"
                    ):
                        self.finalize(task)
                        continue
                observations = dict(row["observations"])
                observations["termination_reason"] = "execution_interrupted"
                self.repository.update(
                    task_id, state="interrupted", observations=observations
                )

    def orphans(self):
        known = {
            self._paths(self._record(task_id))[0] for task_id in self.repository.list()
        }
        # Detection only. Report logical names, never host paths; reject links.
        result = []
        with self.filesystem.anchored_root(self.parent, self.parent_identity) as root:
            pathname = (
                "\\\\?\\" + root.path if isinstance(root, WindowsDirectory) else root.fd
            )
            with os.scandir(pathname) as entries:
                names = [entry.name for entry in entries]
            for name in names[:1000]:
                try:
                    UUID(name)
                    with self.filesystem.directory(root, (name,)) as node:
                        pathname = (
                            "\\\\?\\" + node.path
                            if isinstance(node, WindowsDirectory)
                            else node.fd
                        )
                        with os.scandir(pathname) as entries:
                            tasks = [entry.name for entry in entries]
                        for task in tasks[:1000]:
                            UUID(task)
                            if self.parent / name / task not in known:
                                result.append((name, task))
                except (ValueError, OSError):
                    continue
        return tuple(result[:1000])

    def cleanup(self, task_id, workspace_id):
        row = self._record(task_id)
        if row["workspace_id"] != UUID(str(workspace_id)):
            raise CodingError("Workspace does not belong to the addressed Task")
        if row["state"] == "removed":
            return self.get(task_id)
        if row["state"] in {"ready", "provisioning"}:
            raise CodingError("Only terminal workspaces may be removed")
        target, _ = self._paths(row)
        if not all(
            key in row["identities"] for key in ("worktree", "admin", "admin_name")
        ):
            raise CodingError(
                "Incomplete ownership metadata requires operator recovery"
            )
        # Keep a bounded final observation before destruction, if still available.
        observations = dict(row["observations"])
        if row["state"] != "cleanup_pending":
            with self._worktree_root(row):
                pass
            report = self.get(task_id)
            observations.update(
                changed_files=list(report.changed_files),
                diff_stat=report.diff_stat,
                truncated=report.truncated,
            )
        with self._registration(row) as (repo, admin):
            git = WorkspaceGit(self.config.git_executable, repo, admin)
            symbolic = (
                git.complete(["symbolic-ref", "HEAD"], 256).data.decode("ascii").strip()
            )
            if symbolic != "refs/heads/" + row["branch_name"]:
                raise CodingError("Coding branch association changed")
            if git.head() != row["base_commit"]:
                raise CodingError("Coding branch moved; operator recovery required")
        # Direct capability-based removal avoids Git's path-recursive remove/race
        # window. Remove ONLY the verified worktree and its matching registration.
        self.repository.update(
            task_id, state="cleanup_pending", observations=observations
        )
        with self.filesystem.anchored_root(self.parent, self.parent_identity):
            with self.filesystem.anchored_root(
                target.parent, _identity(row["identities"]["project_parent"])
            ) as parent:
                try:
                    self.filesystem.stat(parent, target.name)
                except FileNotFoundError:
                    # Only explicit pending cleanup can accept a missing owned root.
                    if row["state"] != "cleanup_pending":
                        raise CodingError("Managed workspace disappeared") from None
                else:
                    with self.filesystem.anchored_root(
                        target, _identity(row["identities"]["worktree"])
                    ) as root:
                        remove_tree(
                            root,
                            lambda: self._check_root(
                                target, row["identities"]["worktree"]
                            ),
                        )
                    self._remove_root(
                        parent, target.name, _identity(row["identities"]["worktree"])
                    )
        # Git registration removal is capability based too. Keep the primary .git
        # ancestry pinned and revalidate its gitdir/commondir association after
        # source removal; there is no pathname-recursive Git remove subprocess.
        with self._registration(row) as (_, admin):
            path = (
                Path(row["repository_path"])
                / ".git/worktrees"
                / row["identities"]["admin_name"]
            )
            remove_tree(
                admin, lambda: self._check_root(path, row["identities"]["admin"])
            )
        with self.filesystem.anchored_root(
            path.parent, self.filesystem.observe_root(path.parent)
        ) as parent:
            self._remove_root(parent, path.name, _identity(row["identities"]["admin"]))
        self.repository.update(task_id, state="removed")
        return self.get(task_id)

    def _remove_root(self, parent, name, expected):
        if isinstance(parent, WindowsDirectory):
            self.filesystem.api.remove_directory(parent, name, expected)
        else:
            observed = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
            if (str(observed.st_dev), str(observed.st_ino)) != (
                expected.volume,
                expected.file_id,
            ):
                raise CodingError("Managed cleanup root changed")
            os.rmdir(name, dir_fd=parent.fd)

    def bind(self, task, cancellation):
        row = self._record(task.task_id)
        if (row["project_id"], row["worker_id"], row["state"]) != (
            task.project_id,
            task.worker_id,
            "ready",
        ):
            raise CodingError("Coding workspace execution binding is invalid")
        return CodingSession(self, task, cancellation)


class WorkspaceRegistry:
    """Ephemeral authorized-root adapter. No durable Project/Index mutations."""

    def __init__(self, session):
        self.session = session
        self.filesystem = session.manager.filesystem

    def get_project(self, project_id):
        if UUID(str(project_id)) != self.session.task.project_id:
            raise CodingError("Workspace Project binding is invalid")
        row = self.session.manager._record(self.session.task.task_id)
        _, root = self.session.manager._paths(row)
        original = self.session.manager.projects.get_project(project_id)
        return replace(
            original,
            root_path=root,
            root_identity=_identity(row["identities"]["tool_root"]),
        )

    @contextmanager
    def open_root(self, project_id):
        project = self.get_project(project_id)
        with self.session.manager.open_root(self.session.task.task_id) as (_, root):
            yield project, root


class CodingSession:
    def __init__(self, manager, task, cancellation):
        self.manager, self.task, self.cancellation = manager, task, cancellation
        self.registry = WorkspaceRegistry(self)
        self.reads = RepositoryTools(self.registry)
        from agentforge.coding.tools import coding_toolset

        self.tools = coding_toolset(self)

    def read(self, project_id, arguments, budget):
        self.registry.get_project(project_id)
        # Source and optimistic hash come from ONE authorized byte observation.
        data = self._text(arguments.path, max_bytes=2_097_152)
        lines = fs.text_content(data).splitlines(keepends=True)
        selected = lines[arguments.start_line - 1 : arguments.end_line]
        content, partial = clip(
            "".join(selected[: arguments.max_lines]), max(1, budget // 4)
        )
        count = len(content.splitlines())
        return {
            "path": arguments.path,
            "content": content,
            "start_line": arguments.start_line if count else None,
            "end_line": arguments.start_line + count - 1 if count else None,
            "truncated": partial or len(selected) > arguments.max_lines,
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def _text(self, path, *, max_bytes=None):
        parts = parts_for(self.manager.filesystem, path)
        with self.manager.open_root(self.task.task_id) as (_, root):
            with self.manager.filesystem.directory(root, parts[:-1]) as parent:
                data, partial = fs.read_bytes(
                    parent,
                    parts[-1],
                    max_bytes or self.manager.config.limits.max_file_bytes,
                )
        if partial:
            raise CodingLimit("Coding file size exceeded")
        fs.text_content(data)
        return data

    def edit(self, project_id, arguments, budget, *, operation):
        self.registry.get_project(project_id)
        if self.cancellation.cancelled:
            raise CodingError("Coding Task is cancelled")
        config = self.manager.config
        parts = parts_for(self.manager.filesystem, arguments.path)
        path = "/".join(parts)
        row = self.manager._record(self.task.task_id)
        tree = self.manager._tree(row)
        for entry, item in tree.items():
            if item[0] not in {"100644", "100755"} and self.manager._under(path, entry):
                raise CodingError("Special Git objects are not editable")
        observations = dict(row["observations"])
        calls = observations.get("write_calls", 0) + 1
        paths = set(observations.get("edited_files", [])) | {path}
        if row["state"] != "ready":
            raise CodingError("Only the active coding Task may edit its workspace")
        if (
            calls > config.limits.max_write_calls
            or len(paths) > config.limits.max_changed_files
        ):
            raise CodingLimit("Coding write/file budget exceeded")
        observations.update(write_calls=calls, edited_files=sorted(paths))
        self.manager.repository.update(self.task.task_id, observations=observations)
        if operation == "write":
            data = arguments.content.encode("utf-8")
            fs.text_content(data)
            create = arguments.expected_sha256 is None
            if not create:
                self._text(path)  # Binary/oversized existing files cannot be replaced.
        elif operation == "patch":
            old = self._text(path).decode("utf-8")
            if (
                hashlib.sha256(old.encode("utf-8")).hexdigest()
                != arguments.expected_sha256
            ):
                from agentforge.coding.models import EditConflict

                raise EditConflict("File changed; reread before editing")
            size = len(arguments.old_text.encode("utf-8")) + len(
                arguments.new_text.encode("utf-8")
            )
            if size > config.limits.max_patch_bytes:
                raise CodingLimit("Coding patch size exceeded")
            if old.count(arguments.old_text) != 1:
                raise InvalidToolArgument(
                    "Patch requires exactly one old_text occurrence"
                )
            data = old.replace(arguments.old_text, arguments.new_text, 1).encode(
                "utf-8"
            )
            fs.text_content(data)
            create = False
        else:
            self._text(path)
            data, create = b"", False
        total = observations.get("bytes_reserved", 0) + len(data)
        if (
            len(data) > config.limits.max_file_bytes
            or total > config.limits.max_total_write_bytes
        ):
            raise CodingLimit("Coding byte budget exceeded")
        # Charge BEFORE the operation so crashes/partial failures cannot lose limits.
        observations.update(
            write_calls=calls, edited_files=sorted(paths), bytes_reserved=total
        )
        self.manager.repository.update(self.task.task_id, observations=observations)
        _, root_path = self.manager._paths(row)
        with self.manager.open_root(self.task.task_id) as (_, root):

            def check():
                if self.cancellation.cancelled:
                    raise CodingError("Coding Task is cancelled")
                self.manager._check_root(root_path, row["identities"]["tool_root"])

            mutate(
                root,
                path,
                data,
                expected=arguments.expected_sha256,
                create=create,
                delete=operation == "delete",
                mkdir=create,
                check=check,
            )
        observations["bytes_written"] = observations.get("bytes_written", 0) + len(data)
        self.manager.repository.update(self.task.task_id, observations=observations)
        return {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest()
            if operation != "delete"
            else None,
            "deleted": operation == "delete",
        }

    async def validate(self, project_id, arguments, budget):
        self.registry.get_project(project_id)
        if self.cancellation.cancelled:
            raise CodingError("Coding Task is cancelled")
        command = self.manager.config.validations.get(arguments.name)
        if command is None:
            raise InvalidToolArgument("Validation command is not allowlisted")
        row = self.manager._record(self.task.task_id)
        observations = dict(row["observations"])
        count = observations.get("validation_count", 0)
        if count >= self.manager.config.limits.max_validations:
            raise CodingLimit("Coding validation count exceeded")
        if row["state"] != "ready":
            raise CodingError("Only the active coding Task may run validation")
        observations["validation_count"] = count + 1
        self.manager.repository.update(self.task.task_id, observations=observations)
        executable = Path(command.argv[0])
        _, tool_root = self.manager._paths(row)

        def save_run(run):
            def redact(text):
                for path in (
                    tool_root,
                    Path(row["worktree_path"]),
                    Path(row["repository_path"]),
                    executable,
                ):
                    text = text.replace(str(path), "<coding-host-path>")
                    text = text.replace(
                        str(path).replace("\\", "/"), "<coding-host-path>"
                    )
                return text

            out, out_partial = clip(
                redact(run.stdout),
                self.manager.config.limits.max_validation_output_bytes,
            )
            err, err_partial = clip(
                redact(run.stderr),
                self.manager.config.limits.max_validation_output_bytes,
            )
            run = run.model_copy(
                update={
                    "name": arguments.name,
                    "stdout": out,
                    "stderr": err,
                    "stdout_truncated": run.stdout_truncated or out_partial,
                    "stderr_truncated": run.stderr_truncated or err_partial,
                }
            )
            observations = dict(self.manager._record(self.task.task_id)["observations"])
            observations["validation_runs"] = [
                *observations.get("validation_runs", []),
                run.model_dump(mode="json"),
            ]
            self.manager.repository.update(self.task.task_id, observations=observations)
            return run

        with self.manager.open_root(self.task.task_id) as (_, root):
            _, tool_root = self.manager._paths(row)
            if executable.is_relative_to(
                Path(row["repository_path"])
            ) or executable.is_relative_to(self.manager.parent):
                raise CodingError(
                    "Validation executable must be outside source/workspace roots"
                )
            # Pin the configured executable and no-follow ancestry for its lifetime.
            backend = self.manager.filesystem
            with backend.anchored_root(
                executable.parent, backend.observe_root(executable.parent)
            ) as directory:
                with backend.regular_file(directory, executable.name):
                    cwd = (
                        root.path
                        if isinstance(root, WindowsDirectory)
                        else f"/proc/self/fd/{root.fd}"
                    )
                    run = await validation_process(
                        command.argv,
                        cwd=cwd,
                        timeout=min(
                            command.timeout_seconds,
                            self.manager.config.limits.max_validation_seconds,
                        ),
                        cap=self.manager.config.limits.max_validation_output_bytes,
                        cancellation=self.cancellation,
                        on_cancel=save_run,
                        pass_fds=()
                        if isinstance(root, WindowsDirectory)
                        else (root.fd,),
                    )

        run = save_run(run)
        # Director retains the configured bounded capture; model additionally fits
        # its RuntimeLimits framing budget. Explicit truncation survives projection.
        stdout, out_partial = clip(run.stdout, max(1, budget // 8))
        stderr, err_partial = clip(run.stderr, max(1, budget // 8))
        return run.model_copy(
            update={
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": run.stdout_truncated or out_partial,
                "stderr_truncated": run.stderr_truncated or err_partial,
            }
        ).model_dump()
