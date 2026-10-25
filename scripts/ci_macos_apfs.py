"""Run native security/coder checks on a disposable case-sensitive APFS image."""

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


def main():
    if sys.platform != "darwin":
        raise RuntimeError("Native Darwin is required, never emulated")
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
            "Case-sensitive APFS",
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
            (mount / "CaseProbe").write_text("native case-sensitive APFS")
            assert not (mount / "caseprobe").exists(), "APFS is not case-sensitive"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-ra",
                    "--tb=short",
                    "--basetemp",
                    str(mount / "pytest"),
                    "--junitxml=reports/pytest-apfs-case-sensitive.xml",
                    "tests/test_macos.py",
                    "tests/test_coding_workspaces.py",
                    "tests/test_coding_validation.py",
                    "tests/test_executor_ownership.py",
                ],
                cwd=ROOT,
                check=True,
                timeout=600,
            )
            (ROOT / "reports/apfs-case-sensitive.json").write_text(
                json.dumps(
                    {"native": True, "filesystem": "apfs", "case_sensitive": True}
                )
                + "\n",
                encoding="utf-8",
            )
        finally:
            # Never force-detach an image still in use or report that as success.
            run("hdiutil", "detach", str(mount))


if __name__ == "__main__":
    main()
