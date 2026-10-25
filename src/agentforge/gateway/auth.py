"""Operator-provisioned opaque bearer credentials, hashed at rest.

No authorization server, token exchange, credential forwarding or network calls.
The private policy file is reloaded for every HTTP request and every operation.
"""

import errno
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from mcp.server.auth.provider import AccessToken, TokenVerifier

from agentforge.application.access import ClientPolicy, Policies
from agentforge.application.errors import ServiceError
from agentforge.projects.errors import UnsafeProjectPath


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PolicyUpdateConflict(ValueError):
    """Another writer won; callers must reload before trying a new update."""


class PolicyPersistenceUncertain(OSError):
    """Atomic publication succeeded, but its durability could not be confirmed."""


class CredentialCleanupFailed(OSError):
    """Unpublished credential output could not be removed; quarantine it."""


class _PolicyChanged(Exception):
    pass


def _policy_bytes(path: Path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            # An atomic replace may unlink the opened old inode. Zero links is
            # valid for that reader; multiple links or non-files never are.
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise ValueError("Use a regular private policy file")
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("Use a private policy file")
            data = os.read(fd, 262_145)
            current = path.lstat()
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise _PolicyChanged
            return data
        finally:
            os.close(fd)
    # CRT open() omits FILE_SHARE_DELETE and can block a separate provisioning
    # process replacing the policy. Read an immutable old/new file with delete
    # sharing, never write sharing, and never follow a leaf reparse point.
    import ctypes

    from agentforge.projects.win32 import Win32

    api = Win32()
    handle = api.api.CreateFileW(
        str(path), 0x80000000, 1 | 4, None, 3, 0x00200000, None
    )
    if handle == ctypes.c_void_p(-1).value:
        api._error()
    try:
        observed = api.info(handle)
        if observed.directory or observed.reparse or observed.links > 1:
            raise ValueError("Use a regular private policy file")
        data = api.read(handle, 262_145)
        # Recheck the name after the read. A reader held across publication must
        # not authorize from its old, now-unlinked snapshot after revocation.
        current = api.api.CreateFileW(
            str(path), 0, 1 | 2 | 4, None, 3, 0x00200000, None
        )
        if current == ctypes.c_void_p(-1).value:
            api._error()
        try:
            latest = api.info(current)
            if latest.directory or latest.reparse or latest.links > 1:
                raise ValueError("Use a regular private policy file")
            if latest.identity != observed.identity:
                raise _PolicyChanged
        finally:
            api.close(current)
        return data
    finally:
        api.close(handle)


class CredentialStore(TokenVerifier):
    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()

    def load(self) -> Policies:
        # Bounded snapshot retries, never stale authorization or an update lock
        # on readers. Heavy policy churn fails authentication closed.
        for _ in range(4):
            try:
                data = _policy_bytes(self.path)
                if len(data) > 262_144:
                    raise ValueError
                return Policies.model_validate_json(data)
            except _PolicyChanged:
                continue
            except (OSError, ValueError, UnsafeProjectPath):
                break
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


@contextmanager
def _policy_writer(path):
    """Nonblocking cross-process writer lock; the stable sidecar is never unlinked."""
    sidecar = path.with_name(path.name + ".agentforge-policy.lock")
    if sidecar.is_symlink():
        raise ValueError("Use a regular private policy lock")
    fd = os.open(
        sidecar,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        0o600,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size:
            raise ValueError("Use a regular private policy lock")
        if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("Use a private policy lock")
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise PolicyUpdateConflict from None
            raise
        yield
    finally:
        os.close(fd)


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _replace_policy(source, target):
    if os.name != "nt":
        os.replace(source, target)
        return
    import ctypes

    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move.restype = ctypes.c_int
    # Same-directory atomic replacement with write-through, never copy/delete.
    if not move(str(source), str(target), 0x1 | 0x8):
        raise ctypes.WinError(ctypes.get_last_error())


def save_policies(path: Path, policies: Policies, *, expected: Policies | None):
    """Compare-and-swap a complete snapshot. None means create-only.

    Read expected before preparing changes; never reload merely to force a stale
    replacement through. All participating writers use this permanent lock.
    """
    path = Path(path).absolute()
    policies = Policies.model_validate(policies.model_dump())
    data = (policies.model_dump_json(indent=2) + "\n").encode("utf-8")
    if len(data) > 262_144:
        raise ValueError("Policy exceeds the read budget")
    if path.is_symlink():
        raise ValueError("Use a regular private policy file")
    published = False
    try:
        with _policy_writer(path):
            current = CredentialStore(path).load() if path.exists() else None
            if current != expected:
                raise PolicyUpdateConflict
            fd, temporary = tempfile.mkstemp(
                prefix=".agentforge-auth-", dir=path.parent
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                for attempt in range(11):
                    try:
                        _replace_policy(temporary, path)
                        published = True
                        break
                    except OSError as error:
                        # Only external transient sharing conflicts need a bounded
                        # retry. Our readers allow DELETE sharing from the outset.
                        if (
                            getattr(error, "winerror", None) not in {32, 33}
                            or attempt == 10
                        ):
                            raise
                        time.sleep(0.02)
                try:
                    _sync_directory(path.parent)
                except OSError:
                    # Rollback could undo a revocation after readers observed it.
                    raise PolicyPersistenceUncertain from None
            finally:
                Path(temporary).unlink(missing_ok=True)
    except BaseException:
        if published:
            raise PolicyPersistenceUncertain from None
        raise


def credential_command(args):
    path = Path(args.auth).absolute()
    outputs = [path]
    if args.operation != "client-revoke":
        outputs.append(Path(args.token_file).absolute())
    for output in outputs:
        if output.is_symlink() or any(
            (parent / ".git").exists() for parent in output.resolve().parents
        ):
            raise ValueError("Use private credential files outside repositories")
    reserved = path.with_name(path.name + ".agentforge-policy.lock")
    if (
        len({output.resolve() for output in outputs}) != len(outputs)
        or len(outputs) == 2
        and outputs[1].resolve() == reserved.resolve()
    ):
        raise ValueError("Policy, lock and credential output must be separate files")
    original = CredentialStore(path).load() if path.exists() else None
    clients = {c.client_id: c for c in original.clients} if original else {}
    current = clients.get(args.client_id)
    token_path = None
    try:
        if args.operation == "client-revoke":
            if current is None:
                raise ValueError("Unknown client")
            clients[args.client_id] = current.model_copy(update={"enabled": False})
        else:
            if (args.operation == "client-add") == (current is not None):
                raise ValueError(
                    "Add requires a new identity; rotate requires an existing one"
                )
            if current is not None and not current.enabled:
                raise ValueError("Rotation cannot re-enable a revoked identity")
            token = secrets.token_urlsafe(48)
            client = ClientPolicy(
                client_id=args.client_id, token_sha256=fingerprint(token)
            )
            if current:
                client = current.model_copy(update={"token_sha256": fingerprint(token)})
            # Never print a credential or overwrite an existing output. Publish
            # policy only after this private output is flushed and durable.
            output = outputs[1]
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            token_path = output  # Cleanup only an output created by this command.
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(token + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(output.parent)
            clients[args.client_id] = client
        save_policies(
            path, Policies(clients=tuple(clients.values())), expected=original
        )
    except PolicyPersistenceUncertain:
        # Publication already happened: retain its credential for operator recovery.
        raise
    except BaseException:
        if token_path is not None:
            try:
                token_path.unlink()
            except OSError:
                raise CredentialCleanupFailed from None
        raise
    print(json.dumps({"client_id": args.client_id, "operation": args.operation}))
    return 0
