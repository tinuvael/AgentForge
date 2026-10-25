"""Run installed smoke in a clean external venv on Linux, Windows and macOS."""

import hashlib
import json
import os
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
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    result_path = reports / "wheel.json"
    result_path.unlink(missing_ok=True)
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(name, None)
    env["PYTHONNOUSERSITE"] = "1"
    with tempfile.TemporaryDirectory(prefix="agentforge-wheel-") as directory:
        outside = Path(directory).resolve()
        if outside.is_relative_to(ROOT):
            raise SystemExit(
                "Wheel smoke requires a temporary directory outside checkout"
            )
        venv = outside / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)], check=True, env=env, timeout=60
        )
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

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

        run("-m", "pip", "install", wheel)
        run("-m", "pip", "check")
        # The script imports its sibling verify_companion; cwd and package imports
        # are outside source, while the existing smoke verifies every import path.
        run(ROOT / "scripts" / "verify_install.py")
        (reports / "wheel-dependencies.txt").write_text(
            run("-m", "pip", "freeze", capture=True).stdout, encoding="utf-8"
        )
        result_path.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "wheel": wheel.name,
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    "outside_checkout": True,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
