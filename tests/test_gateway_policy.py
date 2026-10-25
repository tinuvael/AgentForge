"""Policy races use real independent OS processes on POSIX and native Windows."""

import asyncio
from pathlib import Path

import pytest

from agentforge.application.access import Policies
from agentforge.application.errors import ServiceError
from agentforge.cli import main
from agentforge.gateway import auth
from tests.test_executor_ownership import Peer

HELPER = Path(__file__).with_name("helpers") / "policy_process.py"


class PolicyPeer(Peer):
    def __init__(self, path, operation, identity, output, *, hold_publish=False):
        # Reuse only bounded pipe reading/guaranteed cleanup, never a lock mock.
        import queue
        import subprocess
        import sys
        import threading

        self.process = subprocess.Popen(
            [sys.executable, str(HELPER), str(path), operation, identity, str(output)]
            + (["hold"] if hold_publish else []),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.messages = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()


def invoke(path, operation, output=None, identity="client"):
    arguments = ["gateway", operation, "--auth", str(path), "--client-id", identity]
    if output is not None:
        arguments += ["--token-file", str(output)]
    return main(arguments)


def authorized(path, token):
    return asyncio.run(auth.CredentialStore(path).verify_token(token)) is not None


def seeded(tmp_path):
    path, output = tmp_path / "clients.json", tmp_path / "original.token"
    assert invoke(path, "client-add", output) == 0
    return path, output, output.read_text().strip()


def finish(peer, code):
    assert peer.receive("done") == {"event": "done", "code": code, "leaked": False}
    peer.finish()


def test_simultaneous_client_adds_conflict_explicitly_and_retry_preserves_both(
    tmp_path,
):
    path, _, old_token = seeded(tmp_path)
    outputs = [tmp_path / "alpha.token", tmp_path / "beta.token"]
    peers = []
    try:
        for identity, output in zip(("alpha", "beta"), outputs, strict=True):
            peers.append(PolicyPeer(path, "client-add", identity, output))
        for peer in peers:
            peer.receive("prepared")  # Both commands captured the same old policy.
        for peer in peers:
            peer.send()
        results = [peer.receive("done") for peer in peers]
        assert sorted(item["code"] for item in results) == [0, 7]
        for peer, result in zip(peers, results, strict=True):
            assert not result["leaked"]
            peer.finish()
        winner = next(
            index for index, result in enumerate(results) if result["code"] == 0
        )
        loser = 1 - winner
        policies = auth.CredentialStore(path).load()
        assert {row.client_id for row in policies.clients} == {
            "client",
            ("alpha", "beta")[winner],
        }
        assert not outputs[loser].exists()
        assert authorized(path, old_token)
        assert invoke(path, "client-add", outputs[loser], ("alpha", "beta")[loser]) == 0
        assert {row.client_id for row in auth.CredentialStore(path).load().clients} == {
            "client",
            "alpha",
            "beta",
        }
    finally:
        for peer in peers:
            peer.cleanup()


@pytest.mark.parametrize("first", ["client-revoke", "client-rotate"])
def test_revocation_rotation_race_never_reverses_committed_revocation(tmp_path, first):
    path, _, token = seeded(tmp_path)
    output = tmp_path / "rotated.token"
    peers = {}
    try:
        for operation in ("client-revoke", "client-rotate"):
            peers[operation] = PolicyPeer(
                path,
                operation,
                "client",
                output if operation == "client-rotate" else tmp_path / "unused",
            )
        for peer in peers.values():
            peer.receive("prepared")
        peers[first].send()
        finish(peers[first], 0)
        second = "client-rotate" if first == "client-revoke" else "client-revoke"
        peers[second].send()
        finish(peers[second], 7)  # A stale administrative snapshot never wins.
        if first == "client-rotate":
            rotated = output.read_text().strip()
            assert authorized(path, rotated) and not authorized(path, token)
            assert invoke(path, "client-revoke") == 0  # Explicit fresh retry.
            assert not authorized(path, rotated)
        else:
            assert not output.exists()
        assert not authorized(path, token)
        assert not auth.CredentialStore(path).load().clients[0].enabled
        assert invoke(path, "client-rotate", tmp_path / "cannot-reenable.token") == 2
        assert not (tmp_path / "cannot-reenable.token").exists()
    finally:
        for peer in peers.values():
            peer.cleanup()


def test_native_reader_held_open_does_not_block_revocation_or_return_old_policy(
    tmp_path,
):
    path, output, token = seeded(tmp_path)
    peer = PolicyPeer(path, "reader", "client", output)
    try:
        peer.receive("reading")  # The old inode/NTFS handle is still open.
        assert invoke(path, "client-revoke") == 0
        assert not authorized(path, token)
        peer.send()
        assert peer.receive("verified") == {"event": "verified", "authorized": False}
        peer.finish()
    finally:
        peer.cleanup()


def test_new_token_is_inactive_until_policy_publication(tmp_path):
    path, _, old_token = seeded(tmp_path)
    output = tmp_path / "rotated.token"
    peer = PolicyPeer(path, "client-rotate", "client", output)
    try:
        peer.receive("prepared")  # Credential output is already flushed/durable.
        token = output.read_text().strip()
        assert not authorized(path, token) and authorized(path, old_token)
        peer.send()
        finish(peer, 0)
        assert authorized(path, token) and not authorized(path, old_token)
        assert token not in path.read_text()
    finally:
        peer.cleanup()


@pytest.mark.parametrize("failure", ["replace", "credential_flush", "policy_flush"])
def test_failed_persistence_preserves_old_credential_and_removes_new_output(
    tmp_path, monkeypatch, failure
):
    path, original, old_token = seeded(tmp_path)
    before = path.read_bytes()
    output = tmp_path / "failed.token"

    if failure == "replace":

        def fail(*_):
            raise PermissionError("Synthetic access denial")

        monkeypatch.setattr(auth, "_replace_policy", fail)
    else:
        original_sync = auth.os.fsync
        calls = 0

        def fail(fd):
            nonlocal calls
            calls += 1
            target = (
                1
                if failure == "credential_flush"
                else (2 if auth.os.name == "nt" else 3)
            )
            if calls == target:
                raise OSError("Synthetic flush failure")
            original_sync(fd)

        monkeypatch.setattr(auth.os, "fsync", fail)
    assert invoke(path, "client-rotate", output) == 3
    assert not output.exists() and not list(tmp_path.glob(".agentforge-auth-*"))
    assert original.read_text().strip() == old_token
    assert path.read_bytes() == before and authorized(path, old_token)


def test_stale_full_snapshot_cannot_undo_revocation(tmp_path):
    path, _, _ = seeded(tmp_path)
    stale = auth.CredentialStore(path).load()
    assert invoke(path, "client-revoke") == 0
    with pytest.raises(auth.PolicyUpdateConflict):
        auth.save_policies(path, stale, expected=stale)
    assert not auth.CredentialStore(path).load().clients[0].enabled
    # The default create-only expectation also cannot overwrite existing policy.
    with pytest.raises(auth.PolicyUpdateConflict):
        auth.save_policies(path, Policies(), expected=None)


def test_exhausted_sharing_conflicts_are_bounded_and_leave_policy_unchanged(
    tmp_path, monkeypatch
):
    path, _, old_token = seeded(tmp_path)
    attempts = []

    def scanning(*_):
        attempts.append(1)
        error = PermissionError("Synthetic external scanner")
        error.winerror = 32
        raise error

    monkeypatch.setattr(auth, "_replace_policy", scanning)
    assert invoke(path, "client-rotate", tmp_path / "failed.token") == 3
    assert len(attempts) == 11
    assert authorized(path, old_token)
    assert not (tmp_path / "failed.token").exists()


@pytest.mark.posix
def test_post_publication_durability_failure_is_explicit_and_retains_recovery_token(
    tmp_path, monkeypatch, capsys
):
    path, _, old_token = seeded(tmp_path)
    output = tmp_path / "recover.token"
    original_sync = auth._sync_directory
    calls = 0

    def uncertain(directory):
        nonlocal calls
        calls += 1
        if calls == 2:  # Credential directory synced; atomic policy already moved.
            raise OSError("Synthetic disk failure")
        original_sync(directory)

    monkeypatch.setattr(auth, "_sync_directory", uncertain)
    assert invoke(path, "client-rotate", output) == 3
    token = output.read_text().strip()
    assert authorized(path, token) and not authorized(path, old_token)
    assert "policy_persistence_uncertain" in capsys.readouterr().err


@pytest.mark.posix
def test_policy_permissions_remain_private_and_unsafe_reads_fail_closed(tmp_path):
    path, _, _ = seeded(tmp_path)
    assert path.stat().st_mode & 0o077 == 0
    path.chmod(0o644)
    with pytest.raises(ServiceError, match="access_denied"):
        auth.CredentialStore(path).load()


def test_native_writer_lock_rejects_overlap_without_blocking_authentication(tmp_path):
    path, _, token = seeded(tmp_path)
    first = PolicyPeer(
        path, "client-add", "alpha", tmp_path / "alpha.token", hold_publish=True
    )
    second = None
    try:
        first.receive("prepared")
        second = PolicyPeer(path, "client-add", "beta", tmp_path / "beta.token")
        second.receive("prepared")
        first.send()
        first.receive("locked")  # Actual OS lock held before atomic publication.
        assert authorized(path, token)
        second.send()
        finish(second, 7)
        assert not (tmp_path / "beta.token").exists()
        assert len(auth.CredentialStore(path).load().clients) == 1
        first.send()
        finish(first, 0)
        assert len(auth.CredentialStore(path).load().clients) == 2
    finally:
        first.cleanup()
        if second:
            second.cleanup()


def test_token_output_cannot_overwrite_reserved_policy_lock(tmp_path):
    path = tmp_path / "clients.json"
    output = path.with_name(path.name + ".agentforge-policy.lock")
    assert invoke(path, "client-add", output) == 2
    assert not path.exists() and not output.exists()


def test_credential_cleanup_failure_is_explicit_and_token_stays_inactive(
    tmp_path, monkeypatch, capsys
):
    path, _, token = seeded(tmp_path)
    output = tmp_path / "inactive.token"
    original_unlink = Path.unlink

    def cannot_remove(candidate, *args, **kwargs):
        if candidate == output:
            raise PermissionError("Synthetic cleanup denial")
        return original_unlink(candidate, *args, **kwargs)

    def cannot_publish(*args):
        raise PermissionError("Synthetic update denial")

    monkeypatch.setattr(Path, "unlink", cannot_remove)
    monkeypatch.setattr(auth, "_replace_policy", cannot_publish)
    try:
        assert invoke(path, "client-rotate", output) == 3
        pending_token = output.read_text().strip()
        assert authorized(path, token) and not authorized(path, pending_token)
        error = capsys.readouterr().err
        assert "credential_cleanup_failed" in error
        assert pending_token not in error and str(output) not in error
    finally:
        original_unlink(output, missing_ok=True)
