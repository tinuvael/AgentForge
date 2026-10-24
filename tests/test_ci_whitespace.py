"""Real committed whitespace in a clean disposable checkout must fail CI."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci_check_whitespace.py"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args]).decode().strip()


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Offline Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "example.txt").write_bytes(b"clean\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "example.txt").write_bytes(b"clean\ncommitted trailing space \n")
    git(root, "add", ".")
    git(root, "commit", "-m", "bad whitespace")
    head = git(root, "rev-parse", "HEAD")
    assert git(root, "status", "--porcelain") == ""
    assert git(root, "diff", "--check") == ""  # The old gate misses it.
    return root, base, head


def check(history, tmp_path, event_name, event):
    root, _, head = history
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=root,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_SHA": head,
        },
        capture_output=True,
        timeout=10,
    )


@pytest.mark.parametrize("event_name", ["pull_request", "push", "workflow_dispatch"])
def test_committed_trailing_whitespace_fails(history, tmp_path, event_name):
    _, base, head = history
    event = {
        "before": base,
        "after": head,
        "pull_request": {"base": {"sha": base}, "head": {"sha": head}},
    }
    result = check(history, tmp_path, event_name, event)
    assert result.returncode == 2  # git diff --check's whitespace-error exit code.
    assert b"trailing whitespace" in result.stdout


@pytest.mark.parametrize("before", [None, "", "0" * 40])
def test_new_branch_checks_entire_snapshot(history, tmp_path, before):
    result = check(history, tmp_path, "push", {"before": before, "after": history[2]})
    assert result.returncode != 0
    assert b"trailing whitespace" in result.stdout


def test_missing_commit_fails_closed(history, tmp_path):
    result = check(history, tmp_path, "push", {"before": "f" * 40, "after": history[2]})
    assert result.returncode != 0
    assert b"Cannot resolve whitespace range" in result.stderr


def test_pr_uses_head_and_merge_base_even_on_merge_checkout(history, tmp_path):
    root, base, head = history
    git(root, "checkout", "-b", "updated-main", base)
    (root / "other.txt").write_bytes(b"main-only trailing space \n")
    git(root, "add", ".")
    git(root, "commit", "-m", "main moved")
    updated_base = git(root, "rev-parse", "HEAD")
    # A clean PR at the original base must not be blamed for main-only changes.
    result = check(
        (root, base, head),
        tmp_path,
        "pull_request",
        {"pull_request": {"base": {"sha": updated_base}, "head": {"sha": base}}},
    )
    assert result.returncode == 0


def test_explicit_local_range(history):
    root, base, head = history
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--head", head],
        cwd=root,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert b"trailing whitespace" in result.stdout
