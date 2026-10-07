"""Persisted project configuration and separate, ephemeral Git observations."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from agentforge.projects.identity import RootIdentity


@dataclass(frozen=True)
class Project:
    id: UUID
    name: str
    root_path: Path
    created_at: datetime
    root_device: int | None = None
    root_inode: int | None = None
    root_identity: RootIdentity | None = None

    @property
    def filesystem_identity(self) -> RootIdentity | None:
        if self.root_identity is not None:
            return self.root_identity
        if self.root_device is not None and self.root_inode is not None:
            return RootIdentity("posix", str(self.root_device), str(self.root_inode))
        return None


@dataclass(frozen=True)
class GitMetadata:
    """A best-effort observation, never persisted or guaranteed to remain current."""

    status: Literal["repository", "not_repository", "unavailable"]
    observed_at: datetime
    repository_root: Path | None = None
    branch: str | None = None
    head_commit: str | None = None

    @property
    def is_repository(self) -> bool | None:
        if self.status == "unavailable":
            return None
        return self.status == "repository"


@dataclass(frozen=True)
class ProjectInspection:
    project: Project
    git: GitMetadata


@dataclass(frozen=True)
class ProjectSummary:
    project_id: UUID
    name: str
    root_path: str
    indexed_at: datetime | None
    observed_head: str | None
