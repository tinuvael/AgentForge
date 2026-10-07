"""Bounded public results and private durable workspace identities."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

WorkspaceState = Literal[
    "provisioning",
    "ready",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "cleanup_pending",
    "removed",
    "suspicious",
]


class CodingError(Exception):
    """Fixed safe diagnostics only; adapters never expose exception details."""


class EditConflict(CodingError):
    pass


class CodingLimit(CodingError):
    pass


class CodingLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_write_calls: int = Field(default=24, ge=1, le=100)
    max_file_bytes: int = Field(default=262_144, ge=1, le=2_097_152)
    max_patch_bytes: int = Field(default=65_536, ge=1, le=262_144)
    max_total_write_bytes: int = Field(default=1_048_576, ge=1, le=8_388_608)
    max_changed_files: int = Field(default=32, ge=1, le=100)
    max_validations: int = Field(default=8, ge=1, le=32)
    max_validation_seconds: float = Field(default=120.0, gt=0, le=600)
    max_validation_output_bytes: int = Field(default=8192, ge=256, le=65_536)
    max_diff_bytes: int = Field(default=65_536, ge=256, le=262_144)


class ValidationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool = False
    cancelled: bool = False
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class CodingResult(BaseModel):
    """Bounded workspace facts and intentional untrusted validation captures.

    Private workspace paths, reasoning and source tool bodies are not added.
    Validation output is not general secret/host-path classification.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    project_id: UUID
    worker_id: str
    workspace_id: UUID
    branch_name: str
    base_commit: str
    state: WorkspaceState
    termination_reason: str | None = Field(default=None, max_length=40)
    created_at: datetime
    changed_files: tuple[str, ...] = ()
    diff_stat: str = ""
    truncated: bool = False
    inspection_available: bool = True
    validation_runs: tuple[ValidationRun, ...] = ()
    write_calls: int = 0
    bytes_written: int = 0
    validation_duration_seconds: float = 0.0


class CodingDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    workspace_id: UUID
    content: str
    diff_stat: str
    changed_files: tuple[str, ...]
    truncated: bool
