"""Manager-private fixed Git invocations, with no checkout/filter operations."""

import os
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path

from agentforge.coding.models import CodingError
from agentforge.coding.process import git_environment
from agentforge.projects.backends import GitLocation
from agentforge.projects.errors import UnsafeProjectPath
from agentforge.projects.windows import WindowsDirectory
from agentforge.tools.git_backend import _Git

# Reuse the existing concurrent bounded-pipe implementation, with its environment
# supplied per instance instead of changing the read-only Phase 04 boundary.


class WorkspaceGit(_Git):
    def __init__(self, executable, repository, metadata, *, worktree=None):
        self.fd = metadata
        if isinstance(metadata, WindowsDirectory):
            location = GitLocation(
                repository.path, prefix="", executable=str(executable)
            )
            gitdir = metadata.path
            tree = (
                worktree.path
                if isinstance(worktree, WindowsDirectory)
                else repository.path
            )
        else:
            repo_fd, meta_fd = repository.fd, metadata.fd
            gitdir = f"/proc/self/fd/{meta_fd}"
            tree = (
                f"/proc/self/fd/{worktree.fd}"
                if worktree
                else f"/proc/self/fd/{repo_fd}"
            )
            descriptors = (repo_fd, meta_fd, *((worktree.fd,) if worktree else ()))
            location = GitLocation(
                tree, descriptors=descriptors, executable=str(executable)
            )
        self.location = location
        self.argv = [
            str(executable),
            "--no-pager",
            "--literal-pathspecs",
            "--git-dir=" + gitdir,
            "--work-tree=" + tree,
        ]
        for key, value in {
            "core.hooksPath": os.devnull,
            "core.fsmonitor": "false",
            "core.attributesFile": os.devnull,
            "submodule.recurse": "false",
            "core.bare": "false",
            "core.autocrlf": "false",
            "core.quotePath": "false",
            "core.untrackedCache": "false",
            "core.sparseCheckout": "false",
            "core.splitIndex": "false",
            "diff.external": "",
            "core.pager": "",
            "maintenance.auto": "false",
            "gc.auto": "0",
        }.items():
            self.argv.extend(["-c", key + "=" + value])
        self.environment = git_environment()

    def complete(self, args, cap=65536, *, allow_failure=False):
        output = self._run(args, cap, allow_failure=allow_failure)
        if output.truncated:
            raise CodingError("Git metadata exceeded the coding limit")
        return output

    def head(self):
        head = (
            self.complete(["rev-parse", "--verify", "HEAD^{commit}"], 128)
            .data.decode("ascii")
            .strip()
        )
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise CodingError(
                "Coding requires a normal SHA-1 repository with committed HEAD"
            )
        return head

    def tree(self, base, prefix):
        data = self.complete(
            ["ls-tree", "-r", "-z", base, "--", prefix or "."], 2_097_152
        ).data
        result = {}
        for record in data.split(b"\0")[:-1]:
            try:
                metadata, path = record.decode("utf-8").split("\t", 1)
                mode, kind, object_id = metadata.split(" ")
                if not re.fullmatch(r"[0-9a-f]{40}", object_id):
                    raise ValueError
                if prefix:
                    if not path.startswith(prefix + "/"):
                        raise ValueError
                    path = path[len(prefix) + 1 :]
                result[path] = (mode, kind, object_id)
            except (ValueError, UnicodeError):
                raise CodingError("Coding base tree could not be decoded") from None
        if len(result) > 10_000:
            raise CodingError("Coding base tree file limit exceeded")
        return result

    def skip_absent(self, paths):
        """Mark known unmaterialized base entries without touching their contents.

        Paths come only from the immutable base tree, never tool/model arguments.
        Batches also fit Windows' process command-line bound. No sparse checkout,
        index refresh, filters or filesystem checkout is performed.
        """
        batch, size = [], 0
        for path in sorted(paths):
            encoded_size = len(path.encode("utf-8")) + 3
            if encoded_size > 4096:
                raise CodingError("Coding base path exceeds the index limit")
            if size + encoded_size > 8192:
                self.complete(["update-index", "--skip-worktree", "--", *batch], 128)
                batch, size = [], 0
            batch.append(path)
            size += encoded_size
        if batch:
            self.complete(["update-index", "--skip-worktree", "--", *batch], 128)


@contextmanager
def pinned_repository(filesystem, path, identity, executable):
    """An ordinary primary .git directory only, with all access roots pinned.

    Linked/bare/redirected repositories are deliberately unsupported for the first
    write workflow. Configuration is data; supported commands cannot execute
    filters/hooks/drivers. Runtime metadata and installation remain trusted.
    """
    with ExitStack() as stack:
        repo = stack.enter_context(filesystem.anchored_root(path, identity))
        meta = stack.enter_context(filesystem.directory(repo, (".git",)))
        exe_parent = filesystem.canonical_root(
            executable.parent, base_directory=Path.cwd()
        )
        exe_root = stack.enter_context(
            filesystem.anchored_root(exe_parent, filesystem.observe_root(exe_parent))
        )
        if isinstance(exe_root, WindowsDirectory):
            stack.enter_context(filesystem.regular_file(exe_root, executable.name))
        else:
            from agentforge.projects.filesystem import regular_file

            stack.enter_context(regular_file(exe_root.fd, executable.name))
        git = WorkspaceGit(executable, repo, meta)
        for name in (
            "commondir",
            "objects/info/alternates",
            "objects/info/http-alternates",
        ):
            parts = tuple(name.split("/"))
            try:
                with filesystem.directory(meta, parts[:-1]) as parent:
                    filesystem.stat(parent, parts[-1])
            except FileNotFoundError:
                continue
            raise CodingError("Redirected Git metadata is unsupported")
        extensions = git.complete(
            ["config", "--local", "--name-only", "--get-regexp", r"^extensions\."],
            8192,
            allow_failure=True,
        )
        if extensions.returncode not in {0, 1} or extensions.data:
            raise CodingError("Git repository extensions are unsupported")

        # Git opens object/ref/index paths itself. Reject no-follow metadata escape
        # before launch; live concurrent mutation of .git by a privileged same-user
        # process remains outside the security contract, as for existing POSIX Git.
        def audit(node, depth=0):
            if depth > 64:
                raise UnsafeProjectPath("Git metadata nesting limit exceeded")
            nonlocal count, metadata_bytes
            if isinstance(node, WindowsDirectory):
                pathname = "\\\\?\\" + node.path
            else:
                pathname = node.fd
            with os.scandir(pathname) as entries:
                names = [entry.name for entry in entries]
            for name in names:
                count += 1
                if count > 100_000:
                    raise CodingError("Git metadata entry limit exceeded")
                filesystem.validate_parts((name,))
                observed = filesystem.stat(node, name)
                import stat

                if stat.S_ISDIR(observed.st_mode):
                    with filesystem.directory(node, (name,)) as child:
                        audit(child, depth + 1)
                elif stat.S_ISREG(observed.st_mode):
                    size = (
                        observed.size if hasattr(observed, "size") else observed.st_size
                    )
                    metadata_bytes += size
                    if size > 128 * 1024 * 1024 or metadata_bytes > 256 * 1024 * 1024:
                        raise CodingError("Git metadata byte limit exceeded")
                    links = (
                        observed.links
                        if hasattr(observed, "links")
                        else observed.st_nlink
                    )
                    if links != 1:
                        raise UnsafeProjectPath(
                            "Hard-linked Git metadata is unsupported"
                        )
                else:
                    raise UnsafeProjectPath(
                        "Linked/special Git metadata is unsupported"
                    )

        count = metadata_bytes = 0
        audit(meta)
        yield repo, meta, git
