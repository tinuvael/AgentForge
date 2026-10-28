"""Install both release distributions in clean external venvs and run acceptance."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    wheels = list((ROOT / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(
            "Expected exactly one built wheel; clean dist/ and build first"
        )
    wheel = wheels[0]
    sdists = list((ROOT / "dist").glob("*.tar.gz"))
    if len(sdists) != 1:
        raise SystemExit("Expected exactly one sdist; clean dist/ and build first")
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    for kind in ("wheel", "sdist"):
        (reports / f"{kind}.json").unlink(missing_ok=True)
    env = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "PIP_CONSTRAINT",
        "PIP_BUILD_CONSTRAINT",
    ):
        env.pop(name, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PIP_CONFIG_FILE"] = os.devnull
    git = shutil.which("git")
    if git is None:
        raise SystemExit(
            "Installed coding acceptance requires explicitly installed Git"
        )
    with tempfile.TemporaryDirectory(prefix="agentforge installed ") as directory:
        outside = Path(directory).resolve()
        if outside.is_relative_to(ROOT):
            raise SystemExit(
                "Wheel smoke requires a temporary directory outside checkout"
            )
        # Copy only release helpers and sanitized examples. Acceptance cannot read
        # source, local configuration or undeclared developer files from checkout.
        shutil.copytree(
            ROOT / "scripts",
            outside / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (outside / "config").mkdir()
        for example in (ROOT / "config").glob("*.example.toml"):
            shutil.copyfile(example, outside / "config" / example.name)
        for kind, distribution in (("wheel", wheel), ("sdist", sdists[0])):
            verify(kind, distribution, outside, reports, env, git)


def verify(kind, distribution, outside, reports, env, git):
    venv = outside / (kind + " venv")
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)], check=True, env=env, timeout=60
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = env.copy()
    # Keep only the selected venv, declared Git dependency and OS helper tools.
    helpers = (
        str(Path(os.environ["SystemRoot"]) / "System32")
        if os.name == "nt"
        else os.defpath
    )
    env["PATH"] = os.pathsep.join((str(python.parent), str(Path(git).parent), helpers))

    def run(*args, capture=False):
        return subprocess.run(
            [str(python), *map(str, args)],
            cwd=outside,
            env=env,
            check=True,
            timeout=300,
            capture_output=capture,
            text=True,
        )

    # A directly named sdist remains buildable; its dependencies/build backend
    # must resolve to published wheels. Never reuse caches or a local constraint.
    run("-m", "pip", "install", "--no-cache-dir", "--only-binary=:all:", distribution)
    run("-m", "pip", "check")
    if os.name == "nt":
        powershell = (
            Path(os.environ["SystemRoot"])
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(outside / "scripts/verify_windows_setup.ps1"),
                "-Python",
                str(python),
                "-Git",
                git,
                "-Root",
                str(outside / (kind + " PowerShell state")),
            ],
            cwd=outside,
            env=env,
            check=True,
            timeout=90,
        )
    run(outside / "scripts" / "verify_install.py")
    (reports / f"{kind}-dependencies.txt").write_text(
        run("-m", "pip", "freeze", capture=True).stdout, encoding="utf-8"
    )
    (reports / f"{kind}.json").write_text(
        json.dumps(
            {
                "status": "passed",
                kind: distribution.name,
                "sha256": hashlib.sha256(distribution.read_bytes()).hexdigest(),
                "outside_checkout": True,
                "binary_dependencies": True,
                "developer_path_removed": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
