"""Gateway security and real HTTP subprocess acceptance; no live services."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from time import time

import pytest

from agentforge.application.access import Policies
from agentforge.application.errors import ServiceError
from agentforge.cli import main
from agentforge.gateway.auth import CredentialStore, save_policies
from agentforge.gateway.server import GatewayConfig


def test_real_multiclient_streamable_http_acceptance():
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/verify_gateway.py"),
        ],
        capture_output=True,
        text=True,
        timeout=70,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for scenario in "ABCDEFG":
        assert scenario + ":" in result.stdout, result.stdout
    assert "Traceback" not in result.stderr
    assert "test-" + "a" * 48 not in result.stderr
    assert "test-" + "b" * 48 not in result.stderr


@pytest.mark.parametrize(
    "options",
    [
        {"host": "0.0.0.0"},
        {"host": "::"},
        {"host": "8.8.8.8"},
        {"host": "192.168.1.2"},
        {"host": "100.64.0.1", "trusted_tls_proxy": True},
        {"allowed_hosts": ("*",)},
        {"allowed_hosts": ("evil/host",)},
        {"allowed_origins": ("*",)},
        {"tls_cert": "certificate-only"},
        {"max_sessions": 0},
        {"max_sessions": 257},
        {"session_idle_seconds": float("nan")},
        {"watch_seconds": 31},
        {"max_pending_tasks": 0},
    ],
)
def test_network_configuration_fails_closed(options):
    with pytest.raises(ValueError):
        GatewayConfig(**options)


def test_secure_remote_configuration_is_explicit():
    assert GatewayConfig(trusted_tls_proxy=True).host == "127.0.0.1"
    direct = GatewayConfig(
        host="100.64.0.1",
        tls_cert="cert",
        tls_key="key",
        allowed_hosts=("workstation.example.ts.net",),
    )
    assert direct.allowed_hosts == ("workstation.example.ts.net",)


def test_private_provisioning_rotation_revocation_and_malformed_policy(
    tmp_path, capsys
):
    auth = tmp_path / "clients.json"
    first, second = tmp_path / "first.token", tmp_path / "second.token"
    base = ["gateway", "client-add", "--auth", str(auth), "--client-id", "macbook"]
    assert main([*base, "--token-file", str(first)]) == 0
    token = first.read_text().strip()
    assert len(token) >= 48 and token not in capsys.readouterr().out
    assert token not in auth.read_text()
    if os.name != "nt":
        assert first.stat().st_mode & 0o077 == auth.stat().st_mode & 0o077 == 0
    store = CredentialStore(auth)
    client = asyncio.run(store.verify_token(token))
    assert client.client_id == "macbook" and client.scopes == []
    policy = (
        store.load().clients[0].model_copy(update={"tools": ("agentforge_status",)})
    )
    save_policies(auth, Policies(clients=(policy,)))
    assert (
        main(["gateway", "client-rotate", *base[2:], "--token-file", str(second)]) == 0
    )
    rotated = second.read_text().strip()
    assert rotated != token and asyncio.run(store.verify_token(token)) is None
    assert asyncio.run(store.verify_token(rotated)).scopes == ["agentforge_status"]
    assert main(["gateway", "client-revoke", *base[2:]]) == 0
    assert asyncio.run(store.verify_token(rotated)) is None
    expired = (
        store.load()
        .clients[0]
        .model_copy(update={"enabled": True, "expires_at": int(time()) - 1})
    )
    save_policies(auth, Policies(clients=(expired,)))
    assert asyncio.run(store.verify_token(rotated)) is None
    auth.write_text('{"clients": [{"tools": ["shell"]}]}')
    with pytest.raises(ServiceError, match="access_denied"):
        store.load()
    assert asyncio.run(store.verify_token(rotated)) is None
    for output in (capsys.readouterr().out, auth.read_text()):
        assert token not in output and rotated not in output


def test_credentials_refuse_repository_and_overwrite(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    token = tmp_path / "key"
    token.write_text("existing private value")
    assert (
        main(
            [
                "gateway",
                "client-add",
                "--client-id",
                "client",
                "--auth",
                str(tmp_path / "auth.json"),
                "--token-file",
                str(token),
            ]
        )
        == 2
    )
    assert token.read_text() == "existing private value"
    assert "existing private value" not in capsys.readouterr().err
    assert not (tmp_path / "auth.json").exists()


def test_duplicate_identity_or_token_rejected():
    row = {"client_id": "a", "token_sha256": "a" * 64}
    with pytest.raises(ValueError):
        Policies.model_validate({"clients": [row, row]})
    with pytest.raises(ValueError):
        Policies.model_validate({"clients": [row, {**row, "client_id": "b"}]})
