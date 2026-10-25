"""Allowlisted environment facts and JUnit status; no env dumps or runtime data."""

import json
import os
import platform
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def environment():
    facts = {
        "os": platform.platform(),
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "git": subprocess.check_output(["git", "--version"], text=True).strip(),
        "checkout_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "run_sha": os.environ.get("GITHUB_SHA"),
        "runner_image": os.environ.get("ImageVersion"),
    }
    (REPORTS / "environment.json").write_text(
        json.dumps(facts, indent=2) + "\n", encoding="utf-8"
    )
    (REPORTS / "dependencies.txt").write_text(
        subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True),
        encoding="utf-8",
    )


def summary():
    lines = ["# Cloud acceptance", ""]
    environment_path = REPORTS / "environment.json"
    if environment_path.exists():
        facts = json.loads(environment_path.read_text(encoding="utf-8"))
        lines += [f"- {key}: `{value}`" for key, value in facts.items()]
    path = REPORTS / "pytest.xml"
    if path.exists():
        suites = ElementTree.parse(path).getroot().iter("testsuite")
        counts = Counter()
        for suite in suites:
            counts.update(
                {
                    key: int(suite.get(key, 0))
                    for key in ("tests", "failures", "errors", "skipped")
                }
            )
        lines += ["", f"Pytest: {dict(counts)}. Exact skip reasons are in pytest.xml."]
        gateway = next(
            (
                case
                for case in ElementTree.parse(path).getroot().iter("testcase")
                if case.get("name")
                == "test_real_multiclient_streamable_http_acceptance"
            ),
            None,
        )
        gateway_passed = gateway is not None and not any(
            gateway.find(tag) is not None for tag in ("failure", "error", "skipped")
        )
        lines += [
            "",
            "Real multi-client HTTP MCP acceptance (#46): "
            + ("passed" if gateway_passed else "not verified")
            + ".",
        ]
    else:
        lines += ["", "Pytest report unavailable; no regression success claim."]
    wheel = REPORTS / "wheel.json"
    lines += [
        "",
        "Clean installed wheel: " + ("passed" if wheel.exists() else "not verified"),
        "",
        "Physical Windows/Ollama/RTX 4080 acceptance (#31): NOT RUN. "
        "Hosted Windows tests exercise runner NTFS/Git/SQLite only. "
        "Native symlink privilege skips remain explicit in JUnit. "
        "Live remote inference is never part of default CI.",
    ]
    text = "\n".join(lines) + "\n"
    (REPORTS / "summary.md").write_text(text, encoding="utf-8")
    if destination := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(destination).open("a", encoding="utf-8") as stream:
            stream.write(text)


if __name__ == "__main__":
    REPORTS.mkdir(exist_ok=True)
    {"environment": environment, "summary": summary}[sys.argv[1]]()
