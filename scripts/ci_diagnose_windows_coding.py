"""Bounded synthetic-only Git diagnostics; never run against an operator Project.

Compare immutable revisions with the same interpreter, dependencies and Git.
Exceptions are observations, not successful acceptance: pytest remains the gate.
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


def run(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, timeout=30, **kwargs)
    return {
        "argv": list(map(str, argv)),
        "returncode": result.returncode,
        "stdout": result.stdout[:8192].decode("utf-8", "replace"),
        "stderr": result.stderr[:8192].decode("utf-8", "replace"),
    }


def probe(source, root):
    sys.path.insert(0, str(source / "src"))
    from sqlalchemy.engine import URL

    import agentforge
    from agentforge.coding.config import CodingConfig
    from agentforge.coding.service import CodingWorkspaceManager
    from agentforge.db.coding import WorkspaceRepository
    from agentforge.db.database import create_database_engine, create_session_factory
    from agentforge.db.migrate import upgrade_database
    from agentforge.db.projects import ProjectRepository
    from agentforge.projects.service import ProjectRegistry
    from agentforge.tools.git_backend import _run_git

    report = {"source": str(agentforge.__file__), "commands": []}
    primary = root / "primary"
    primary.mkdir()
    executable = str(Path(shutil.which("git")).resolve())
    report["git"] = run([executable, "--version"])
    report["executable_links"] = Path(executable).stat().st_nlink
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Offline Test"],
        ["config", "user.email", "test@example.invalid"],
    ):
        subprocess.run([executable, "-C", str(primary), *args], check=True)
    (primary / "source.py").write_text("original\n", newline="\n")
    for args in (["add", "."], ["commit", "-m", "base"]):
        subprocess.run([executable, "-C", str(primary), *args], check=True)
    database = URL.create("sqlite", database=str(root / "registry.sqlite"))
    upgrade_database(database)
    engine = create_database_engine(database)
    sessions = create_session_factory(engine)
    registry = ProjectRegistry(ProjectRepository(sessions), base_directory=root)
    project = registry.register_project("Synthetic diagnostic", primary)
    parent = root / "workspaces"
    parent.mkdir()
    manager = CodingWorkspaceManager(
        registry,
        WorkspaceRepository(sessions),
        CodingConfig(workspace_parent=parent, git_executable=Path(executable)),
    )
    task = SimpleNamespace(
        task_id=uuid4(), project_id=project.id, worker_id="synthetic", agent_id="coder"
    )
    failed = None

    def trace(frame, event, arg):
        nonlocal failed
        if frame.f_code is not _run_git.__code__:
            return None
        local = frame.f_locals
        if event == "return" and "buffers" in local:
            report["commands"].append(
                {
                    "argv": local["argv"],
                    "environment": local["environment"],
                    "process_options": local["location"].process_options(),
                    "returncode": local["process"].returncode,
                    "stderr": bytes(local["buffers"][1]).decode("utf-8", "replace"),
                }
            )
        if event == "exception" and "buffers" in local and "read-tree" in local["argv"]:
            failed = (local["argv"], local["environment"])
            prefix = local["argv"][:-2]
            report["while_pinned"] = [
                run([*prefix, *args], env=local["environment"])
                for args in (
                    ["rev-parse", "--git-path", "index"],
                    ["rev-parse", "--git-common-dir"],
                    ["cat-file", "-t", local["argv"][-1]],
                    ["read-tree", local["argv"][-1]],
                )
            ]
        return trace

    try:
        sys.settrace(trace)
        manager.create(task)
        report["outcome"] = "ready"
    except Exception as error:
        report["outcome"] = f"{type(error).__name__}: {error}"
    finally:
        sys.settrace(None)
    if failed:
        argv, environment = failed
        report["after_unpinning"] = run(argv, env=environment)
        report["inherited_environment"] = run(argv)
        gitdir = Path(
            next(value[10:] for value in argv if value.startswith("--git-dir="))
        )
        report["metadata"] = {
            name: (gitdir / name).read_text()
            for name in ("commondir", "gitdir", "HEAD")
        }
        report["index_exists"] = (gitdir / "index").exists()
        report["index_lock_exists"] = (gitdir / "index.lock").exists()
        # Isolate the conflicting root without changing the runtime's locking.
        worktree = Path(
            next(value[12:] for value in argv if value.startswith("--work-tree="))
        )
        report["individual_pins"] = []
        for path in (
            primary,
            primary / ".git",
            gitdir,
            worktree,
            Path(executable).parent,
        ):
            (gitdir / "index").unlink(missing_ok=True)
            with manager.filesystem.anchored_root(
                path, manager.filesystem.observe_root(path)
            ):
                result = run(argv, env=environment)
                result["pinned_path"] = str(path)
                report["individual_pins"].append(result)
        report["directory_mutations"] = []
        for pinned in (False, True):
            from contextlib import nullcontext

            context = (
                manager.filesystem.anchored_root(
                    gitdir, manager.filesystem.observe_root(gitdir)
                )
                if pinned
                else nullcontext()
            )
            with context:
                operation = "create"
                first, second = gitdir / "diagnostic.lock", gitdir / "diagnostic.index"
                try:
                    first.write_bytes(b"synthetic index\n")
                    operation = "rename"
                    os.replace(first, second)
                    operation = "delete"
                    second.unlink()
                    report["directory_mutations"].append({"pinned": pinned, "ok": True})
                except OSError as error:
                    report["directory_mutations"].append(
                        {
                            "pinned": pinned,
                            "operation": operation,
                            "error": str(error),
                            "winerror": getattr(error, "winerror", None),
                        }
                    )
            first.unlink(missing_ok=True)
            second.unlink(missing_ok=True)
    engine.dispose()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", action="append")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.source:
        with tempfile.TemporaryDirectory(prefix="af-git-probe-") as directory:
            report = probe(args.source.resolve(), Path(directory).resolve())
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return
    args.output.mkdir(parents=True, exist_ok=True)
    for ref in args.ref or ["HEAD"]:
        sha = (
            subprocess.check_output(["git", "rev-parse", "--verify", ref])
            .decode()
            .strip()
        )
        archive = subprocess.check_output(["git", "archive", "--format=zip", sha])
        with tempfile.TemporaryDirectory(prefix="af-revision-") as directory:
            source = Path(directory).resolve()
            with zipfile.ZipFile(io.BytesIO(archive)) as snapshot:
                snapshot.extractall(source)
            subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--source",
                    str(source),
                    "--output",
                    str(args.output.resolve() / f"windows-coding-{sha}.json"),
                ],
                check=True,
                timeout=180,
            )


if __name__ == "__main__":
    main()
