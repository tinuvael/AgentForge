"""Operator-provisioned opaque bearer credentials, hashed at rest.

No authorization server, token exchange, credential forwarding or network calls.
The private policy file is reloaded for every HTTP request and every operation.
"""

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from mcp.server.auth.provider import AccessToken, TokenVerifier

from agentforge.application.access import ClientPolicy, Policies
from agentforge.application.errors import ServiceError


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CredentialStore(TokenVerifier):
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> Policies:
        try:
            if self.path.is_symlink() or self.path.stat().st_nlink != 1:
                raise ValueError
            # Fixed read budget even when a file is replaced while open.
            with self.path.open("rb") as stream:
                data = stream.read(262_145)
            if len(data) > 262_144:
                raise ValueError
            return Policies.model_validate_json(data)
        except (OSError, ValueError):
            raise ServiceError("access_denied") from None

    def principal(self, digest: str) -> ClientPolicy:
        for client in self.load().clients:
            if hmac.compare_digest(digest, client.token_sha256):
                if client.enabled and (
                    client.expires_at is None or time.time() < client.expires_at
                ):
                    return client
                break
        raise ServiceError("access_denied")

    async def verify_token(self, token: str) -> AccessToken | None:
        if not 32 <= len(token) <= 256 or not token.isascii():
            return None
        digest = fingerprint(token)
        try:
            client = self.principal(digest)
        except ServiceError:
            return None
        return AccessToken(
            token=token,
            client_id=client.client_id,
            subject=digest,  # SDK binds sessions to this exact rotated credential.
            scopes=list(client.tools),
            expires_at=client.expires_at,
        )


def save_policies(path: Path, policies: Policies):
    """Atomic local-operator edit; serialize commands, including manual edits."""
    policies = Policies.model_validate(policies.model_dump())
    if path.is_symlink() or path.exists() and path.stat().st_nlink != 1:
        raise ValueError("Use a regular private policy file")
    fd, temporary = tempfile.mkstemp(prefix=".agentforge-auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(policies.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def credential_command(args):
    path = Path(args.auth)
    outputs = [path]
    if args.operation != "client-revoke":
        outputs.append(Path(args.token_file))
    for output in outputs:
        if output.is_symlink() or any(
            (parent / ".git").exists() for parent in output.resolve().parents
        ):
            raise ValueError("Use private credential files outside repositories")
    policies = CredentialStore(path).load() if path.exists() else Policies()
    clients = {c.client_id: c for c in policies.clients}
    current = clients.get(args.client_id)
    if args.operation == "client-revoke":
        if current is None:
            raise ValueError("Unknown client")
        clients[args.client_id] = current.model_copy(update={"enabled": False})
    else:
        if (args.operation == "client-add") == (current is not None):
            raise ValueError(
                "Add requires a new identity; rotate requires an existing one"
            )
        token = secrets.token_urlsafe(48)
        client = ClientPolicy(client_id=args.client_id, token_sha256=fingerprint(token))
        if current:
            client = current.model_copy(
                update={"token_sha256": fingerprint(token), "enabled": True}
            )
        # Never print a credential or pass one through argv. Refuse overwriting.
        token_path = Path(args.token_file)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
        clients[args.client_id] = client
    save_policies(path, Policies(clients=tuple(clients.values())))
    print(json.dumps({"client_id": args.client_id, "operation": args.operation}))
    return 0
