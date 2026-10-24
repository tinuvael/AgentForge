"""Check committed changes, using event SHAs rather than a clean worktree diff."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


def git(*args, **kwargs):
    return subprocess.run(["git", *args], check=True, capture_output=True, **kwargs)


def commit(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value
    ):
        raise ValueError("Expected a full event commit SHA")
    # Fail closed if checkout/fetch did not supply the requested event object.
    return git("rev-parse", "--verify", value + "^{commit}").stdout.decode().strip()


def empty_tree():
    return (
        git("hash-object", "-t", "tree", "--stdin", input=b"").stdout.decode().strip()
    )


def revision_range(event_name, event, head):
    if event_name == "pull_request":
        pr = event["pull_request"]
        base = commit(pr["base"]["sha"])
        head = commit(pr["head"]["sha"])
        # Exactly the PR's changes, excluding subsequent changes on main.
        return [base + "..." + head]
    if event_name == "push":
        head = commit(event["after"])
        before = event.get("before")
        if not before or re.fullmatch(r"0+", before):
            # New branch / unavailable before SHA: validate the entire snapshot.
            return [empty_tree(), head]
        return [commit(before), head]
    if event_name == "workflow_dispatch":
        # Manual runs have no PR/push range. Check every tracked line at HEAD.
        return [empty_tree(), commit(head)]
    raise ValueError("Unsupported event; use --base and --head for a local range")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Explicit local base commit/ref")
    parser.add_argument("--head", default="HEAD", help="Explicit local head commit/ref")
    args = parser.parse_args()
    try:
        if args.base:
            revisions = [
                git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
                .stdout.decode()
                .strip()
                for ref in (args.base, args.head)
            ]
        else:
            event_path = Path(os.environ["GITHUB_EVENT_PATH"])
            event = json.loads(event_path.read_text(encoding="utf-8"))
            revisions = revision_range(
                os.environ["GITHUB_EVENT_NAME"], event, os.environ["GITHUB_SHA"]
            )
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"Cannot resolve whitespace range: {error}\n")
    print("Checking committed whitespace: " + " ".join(revisions), flush=True)
    return subprocess.run(
        [
            "git",
            "-c",
            "core.whitespace=blank-at-eol,blank-at-eof,space-before-tab",
            "diff",
            "--check",
            "--no-ext-diff",
            "--no-textconv",
            *revisions,
            "--",
        ],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
