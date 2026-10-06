"""Private bounded Git backend. No caller-supplied options or command API."""

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from agentforge.projects.errors import UnsafeProjectPath
from agentforge.projects.git import git_environment
from agentforge.tools.errors import (
    GitFailure,
    GitTimeout,
    GitUnavailable,
    InvalidToolArgument,
    NotGitRepository,
)
from agentforge.tools.policy import automatic, relative_path

GIT_TIMEOUT = 2


@dataclass(frozen=True)
class _Output:
    data: bytes
    truncated: bool
    returncode: int


class _Git:
    def __init__(self, root: Path, root_fd: int):
        self.fd = root_fd
        # Inherited descriptor pins cwd even if the pathname is swapped. Unsupported
        # platforms fail closed rather than falling back to a racy pathname cwd.
        cwd = f"/proc/self/fd/{root_fd}"
        if not Path(cwd).is_dir():
            raise GitUnavailable("Git tools require Linux descriptor-backed cwd")
        self.argv = [
            "git",
            "--no-optional-locks",
            "--no-pager",
            "--literal-pathspecs",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "core.attributesFile=" + os.devnull,
            "-c",
            "submodule.recurse=false",
            "-C",
            cwd,
        ]
        discovered = self._run(
            ["rev-parse", "--show-toplevel"], 4096, allow_failure=True
        )
        if discovered.returncode:
            # Distinguish absence from an unavailable/refused Git backend without
            # ever publishing stderr (which may contain arbitrary repository data).
            probe = self._run(
                ["rev-parse", "--is-inside-work-tree"], 64, allow_failure=True
            )
            if (probe.returncode == 128 and self._not_repository) or (
                probe.returncode == 0 and probe.data.strip() == b"false"
            ):
                raise NotGitRepository("Project is not a Git worktree")
            raise GitFailure("Git worktree discovery failed")
        try:
            repository_root = Path(discovered.data.decode("utf-8").rstrip("\n"))
            self.prefix = root.relative_to(repository_root).as_posix()
            if self.prefix == ".":
                self.prefix = ""
        except (ValueError, UnicodeError):
            raise GitFailure("Git worktree boundary is unavailable") from None
        filters = self._run(
            [
                "config",
                "--null",
                "--name-only",
                "--get-regexp",
                r"^filter\..*\.(clean|smudge|process|required)$",
            ],
            8192,
            allow_failure=True,
        )
        if filters.truncated or filters.returncode not in {0, 1}:
            raise GitFailure("Git filter configuration cannot be isolated")
        try:
            for key in filters.data.decode("utf-8").split("\0"):
                if not key:
                    continue
                if "=" in key or any(ord(c) < 32 for c in key):
                    raise ValueError
                value = "false" if key.endswith(".required") else ""
                self.argv.extend(["-c", key + "=" + value])
        except (ValueError, UnicodeError):
            raise GitFailure("Git filter configuration cannot be isolated") from None

    def _run(self, args: list[str], budget: int, *, allow_failure: bool = False):
        """Drain both pipes concurrently with fixed memory; stop excess output."""
        try:
            with subprocess.Popen(
                [*self.argv, *args],
                env=git_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                pass_fds=(self.fd,),
                start_new_session=True,
            ) as process:
                buffers = [bytearray(), bytearray()]
                exceeded = [threading.Event(), threading.Event()]

                def stop():
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

                def drain(stream, index, cap):
                    while chunk := stream.read(8192):
                        remaining = cap + 1 - len(buffers[index])
                        buffers[index].extend(chunk[: max(remaining, 0)])
                        if len(buffers[index]) > cap:
                            exceeded[index].set()
                            stop()
                            break

                readers = [
                    threading.Thread(target=drain, args=(process.stdout, 0, budget)),
                    threading.Thread(target=drain, args=(process.stderr, 1, 8192)),
                ]
                for reader in readers:
                    reader.start()
                timed_out = False
                try:
                    process.wait(timeout=GIT_TIMEOUT)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    stop()
                    process.wait()
                finally:
                    for reader in readers:
                        reader.join()
                if timed_out:
                    raise GitTimeout("Git operation timed out")
                if exceeded[1].is_set():
                    raise GitFailure("Git diagnostic output exceeded its limit")
                self._not_repository = b"not a git repository" in buffers[1]
                if (
                    process.returncode
                    and not allow_failure
                    and not exceeded[0].is_set()
                ):
                    raise GitFailure("Git operation failed")
                return _Output(
                    bytes(buffers[0][:budget]), exceeded[0].is_set(), process.returncode
                )
        except FileNotFoundError:
            raise GitUnavailable("Git executable is unavailable") from None
        except OSError:
            raise GitFailure("Git operation could not be started") from None

    def _scoped(self, repository_path: str) -> str | None:
        try:
            parts = relative_path(repository_path)
        except (UnsafeProjectPath, InvalidToolArgument):
            return None
        if self.prefix:
            prefix = tuple(self.prefix.split("/"))
            if parts[: len(prefix)] != prefix:
                return None
            parts = parts[len(prefix) :]
        if not parts or not automatic(parts):
            return None
        return "/".join(parts)

    def branch(self) -> tuple[str | None, bool]:
        result = self._run(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], 256, allow_failure=True
        )
        if result.returncode not in (0, 1) or result.truncated:
            raise GitFailure("Git branch observation failed")
        return (
            result.data.decode("utf-8", errors="replace").strip() or None,
            result.returncode == 1,
        )

    def status(self, path: str, budget: int):
        return self._run(
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--no-renames",
                "--ignore-submodules=all",
                "--",
                path,
            ],
            budget,
        )

    def grep(self, query: str, paths: list[str], case_sensitive: bool, budget: int):
        return self._run(
            [
                "grep",
                "--cached",
                "--no-textconv",
                "-I",
                "-F",
                "-n",
                "-z",
                "--full-name",
                *([] if case_sensitive else ["-i"]),
                "-e",
                query,
                "--",
                *paths,
            ],
            budget,
            allow_failure=True,
        )

    def staged_diff(self, paths: list[str], budget: int):
        return self._run(
            [
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--no-color",
                "--unified=3",
                "--diff-algorithm=myers",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--relative",
                "--ignore-submodules=all",
                "--",
                *paths,
            ],
            budget,
        )

    def indexed_files(self, path: str, budget: int):
        return self._run(
            ["ls-files", "--full-name", "--stage", "-z", "--", path], budget
        )

    def blob(self, object_id: str, budget: int):
        # IDs come exclusively from parsed ls-files records, never model arguments.
        if len(object_id) not in {40, 64} or any(
            c not in "0123456789abcdef" for c in object_id
        ):
            raise GitFailure("Invalid indexed object identifier")
        return self._run(["cat-file", "blob", object_id], budget)
