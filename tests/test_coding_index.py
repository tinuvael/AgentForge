"""Compare the manager's initial index with Git's own tree/index semantics."""

import hashlib
import os
import shutil
import subprocess

import pytest

from agentforge.coding.git import initial_index


@pytest.mark.parametrize("selection", ["all", "subtree", "none", "empty"])
def test_initial_index_matches_git(tmp_path, selection):
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = dict(os.environ, GIT_INDEX_FILE=str(tmp_path / "reference-index"))

    def git(*args, input=None):
        return subprocess.check_output(
            [shutil.which("git"), "-C", str(repository), *args],
            env=environment,
            input=input,
            stderr=subprocess.PIPE,
        )

    git("init")
    blob = git("hash-object", "-w", "--stdin", input=b"source\n").decode().strip()
    entries = (
        {}
        if selection == "empty"
        else {
            "source.py": ("100644", "blob", blob),
            "allowed/é.py": ("100755", "blob", blob),
            "allowed/z.py": ("100644", "blob", blob),
            "linked": ("120000", "blob", blob),
            "module": ("160000", "commit", "1" * 40),
            # Exercise the 0xFFF name-length sentinel without OS path I/O.
            "long/" + "a/" * 2050 + "file": ("100644", "blob", blob),
        }
    )
    materialized = {
        path
        for path, (mode, _, _) in entries.items()
        if mode in {"100644", "100755"}
        and (
            selection == "all" or selection == "subtree" and path.startswith("allowed/")
        )
    }
    for path, (mode, _, object_id) in entries.items():
        git("update-index", "--add", "--cacheinfo", mode, object_id, path)
    absent = entries.keys() - materialized
    if absent:
        git("update-index", "--skip-worktree", "--", *sorted(absent))
    reference = (
        git("ls-files", "--stage", "-z"),
        git("ls-files", "-v", "-z"),
        git("write-tree"),
    )
    encoded = initial_index(entries, materialized)
    assert encoded[-20:] == hashlib.sha1(encoded[:-20]).digest()
    candidate = tmp_path / "candidate-index"
    candidate.write_bytes(encoded)
    environment["GIT_INDEX_FILE"] = str(candidate)
    assert (
        git("ls-files", "--stage", "-z"),
        git("ls-files", "-v", "-z"),
        git("write-tree"),
    ) == reference
