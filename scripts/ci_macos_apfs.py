"""Run native security/coder checks on both disposable APFS case variants."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agentforge.projects.darwin import local_apfs

ROOT = Path(__file__).resolve().parents[1]


def run(*argv):
    subprocess.run(argv, check=True, timeout=90)


def acceptance(*, case_sensitive):
    label = "case-sensitive" if case_sensitive else "case-insensitive"
    result = ROOT / f"reports/apfs-{label}.json"
    result.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentforge-apfs-") as temporary:
        directory = Path(temporary).resolve()
        image, mount = directory / "acceptance.sparseimage", directory / "mount"
        mount.mkdir()
        run(
            "hdiutil",
            "create",
            "-size",
            "512m",
            "-type",
            "SPARSE",
            "-fs",
            "Case-sensitive APFS" if case_sensitive else "APFS",
            "-volname",
            "AgentForgeAcceptance",
            str(image),
        )
        run(
            "hdiutil",
            "attach",
            "-nobrowse",
            "-owners",
            "on",
            "-mountpoint",
            str(mount),
            str(image),
        )
        try:
            fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY)
            try:
                local_apfs(fd)
            finally:
                os.close(fd)
            (mount / "CaseProbe").write_text("native APFS")
            assert (not (mount / "caseprobe").exists()) == case_sensitive
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-ra",
                    "--tb=short",
                    "--basetemp",
                    str(mount / "pytest"),
                    f"--junitxml=reports/pytest-apfs-{label}.xml",
                    "tests/test_macos.py",
                    "tests/test_coding_workspaces.py",
                    "tests/test_coding_validation.py",
                    "tests/test_executor_ownership.py",
                ],
                cwd=ROOT,
                check=True,
                timeout=600,
            )
            result.write_text(
                json.dumps(
                    {
                        "native": True,
                        "filesystem": "apfs",
                        "case_sensitive": case_sensitive,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        finally:
            # Never force-detach an image still in use or report that as success.
            run("hdiutil", "detach", str(mount))


def main():
    if sys.platform != "darwin":
        raise RuntimeError("Native Darwin is required, never emulated")
    for case_sensitive in (True, False):
        acceptance(case_sensitive=case_sensitive)


if __name__ == "__main__":
    main()
