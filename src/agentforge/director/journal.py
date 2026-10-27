"""Bounded atomic JSON journal, separate from AgentForge task storage."""

import os
import stat
import tempfile
from pathlib import Path
from uuid import UUID

from agentforge.director.models import Conversation


class Journal:
    def __init__(self, root, limit=32):
        root = Path(root).absolute()
        if root.is_symlink() or any(
            (p / ".git").exists() for p in (root, *root.parents)
        ):
            raise ValueError(
                "Director journal must be private and outside repositories"
            )
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("Director journal directory must be private")
        self.root, self.limit = root.resolve(), limit
        self.lock = None

    def open(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.root / ".owner", flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Invalid journal owner file")
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise ValueError("Director journal already owned or unavailable") from None
        self.lock = fd

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def path(self, identity):
        return self.root / f"{UUID(str(identity))}.json"

    def read(self, identity, owner, gateway_identity):
        path = self.path(identity)
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(path, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError
                data = os.read(fd, 262_145)
            finally:
                os.close(fd)
            if len(data) > 262_144:
                raise ValueError
            item = Conversation.model_validate_json(data)
            if (
                item.id != UUID(str(identity))
                or item.owner != owner
                or (item.gateway_identity != gateway_identity)
            ):
                raise ValueError
            return item
        except (OSError, ValueError):
            raise ValueError("Conversation unavailable") from None

    def save(self, item):
        while (
            len(item.messages) > 1
            and sum(len(m.content.encode()) for m in item.messages) > 96_000
        ):
            item.messages.pop(0)
        item.revision += 1
        data = item.model_dump_json().encode()
        if len(data) > 262_144:
            raise ValueError("Conversation storage limit")
        fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path(item.id))
            if os.name != "nt":
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def list(self, owner, gateway_identity):
        result = []
        for path in sorted(self.root.glob("*.json"))[: self.limit]:
            try:
                result.append(self.read(path.stem, owner, gateway_identity))
            except ValueError:
                continue
        return result

    def create(self, owner, gateway_identity):
        if sum(1 for _ in self.root.glob("*.json")) >= self.limit:
            raise ValueError("Conversation capacity reached; clear old conversations")
        item = Conversation(
            owner=owner, gateway_identity=gateway_identity, workflow_reservations={}
        )
        self.save(item)
        return item
