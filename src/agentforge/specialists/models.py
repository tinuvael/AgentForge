"""Bounded, transport-independent specialist and artifact contracts."""

import hashlib
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")]
Format = Literal["png", "gltf", "stl", "blend", "gaussian_splat"]
JobState = Literal[
    "queued", "running", "completed", "failed", "cancelled", "interrupted"
]
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class SpecialistError(Exception):
    """Only fixed safe categories cross adapters and transports."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class ToolLimits(Model):
    runtime_seconds: float = Field(default=30, gt=0, le=3600)
    max_output_bytes: int = Field(
        default=1_048_576, strict=True, ge=1024, le=16_777_216
    )
    max_requests: int = Field(default=1000, strict=True, ge=1, le=1_000_000)


class SpecialistTool(Model):
    tool_id: Name
    kind: Name
    adapter: Literal["fake_comfyui", "fake_blender"]
    connection_id: Name
    authorized_host: Literal["in_process_fake"] = "in_process_fake"
    trust: Literal["local", "cloud"] = "local"
    enabled: bool = False
    capabilities: tuple[Name, ...] = Field(default=(), max_length=16)
    input_formats: tuple[Format, ...] = Field(default=(), max_length=8)
    output_formats: tuple[Format, ...] = Field(min_length=1, max_length=8)
    limits: ToolLimits = ToolLimits()
    simulated: Literal[True] = True
    availability: Literal["deterministic_fake_only"] = "deterministic_fake_only"
    evidence: Literal["adapter_implemented; real_host_not_probed"] = (
        "adapter_implemented; real_host_not_probed"
    )

    @model_validator(mode="after")
    def supported_contract(self):
        expected = (
            ("comfyui_image", (), ("png",))
            if self.adapter == "fake_comfyui"
            else ("blender_scene", ("png",), ("gltf",))
        )
        if (self.kind, self.input_formats, self.output_formats) != expected:
            raise ValueError("Adapter does not implement the declared formats/kind")
        return self

    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class SpecialistConfig(Model):
    artifact_directory: Path
    max_storage_bytes: int = Field(default=67_108_864, strict=True, ge=1024, le=1 << 40)
    max_artifacts: int = Field(default=10_000, strict=True, ge=1, le=100_000)
    retention_seconds: int = Field(default=604800, strict=True, ge=1, le=31536000)
    tools: tuple[SpecialistTool, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def unique_tools(self):
        if not self.artifact_directory.is_absolute():
            raise ValueError("Private artifact directory must be absolute")
        if len({t.tool_id for t in self.tools}) != len(self.tools):
            raise ValueError("Duplicate specialist tool IDs")
        return self


def load_specialists(path):
    with open(path, "rb") as source:
        return SpecialistConfig.model_validate(tomllib.load(source))


class Parameters(Model):
    # No script/workflow/path/URL/endpoint escapes. Phase B adds named operations.
    seed: int = Field(default=0, strict=True, ge=0, le=2**32 - 1)


class Page(Model):
    limit: int = Field(default=100, strict=True, ge=1, le=100)
    offset: int = Field(default=0, strict=True, ge=0, le=1_000_000)


class ToolQuery(Page):
    project_id: UUID


class SubmitToolJob(Model):
    project_id: UUID
    tool_id: Name
    input_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=8)
    parameters: Parameters = Parameters()
    request_id: UUID
    idempotency_key: Name
    task_id: UUID | None = None


class JobQuery(Model):
    tool_job_id: UUID


class ArtifactQuery(Model):
    project_id: UUID
    artifact_id: UUID


class RetrieveArtifact(ArtifactQuery):
    offset: int = Field(default=0, strict=True, ge=0, le=16_777_216)
    max_bytes: int = Field(default=49152, strict=True, ge=1, le=49152)


class ToolJob(Model):
    tool_job_id: UUID
    project_id: UUID
    tool_id: Name
    kind: Name
    adapter: Name
    connection_id: Name
    trust: Literal["local", "cloud"]
    limits: ToolLimits
    client_id: str | None = None
    input_artifact_ids: tuple[UUID, ...]
    parameters: Parameters
    output_artifact_ids: tuple[UUID, ...] = ()
    request_id: UUID
    idempotency_key: Name
    task_id: UUID | None = None
    state: JobState
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancellation_requested_at: datetime | None = None
    failure_category: str | None = None
    tool_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    simulated: bool = True


class Artifact(Model):
    artifact_id: UUID
    project_id: UUID
    tool_job_id: UUID
    tool_id: Name
    input_artifact_ids: tuple[UUID, ...]
    created_at: datetime
    expires_at: datetime
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,99}$")
    format: Format
    mime_type: str
    size_bytes: int = Field(ge=1, le=16_777_216)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    simulated: bool = True
    geometry: Literal["none", "mesh", "gaussian_splat"] = "none"
    printability: Literal["not_assessed"] = "not_assessed"
    status: Literal["ready", "expired", "missing", "corrupt"] = "ready"


class ToolPage(Model):
    tools: tuple[SpecialistTool, ...]
    next_offset: int | None


class JobPage(Model):
    jobs: tuple[ToolJob, ...]
    next_offset: int | None


class ArtifactPage(Model):
    artifacts: tuple[Artifact, ...]
    next_offset: int | None


class ArtifactChunk(Model):
    artifact_id: UUID
    offset: int
    data_base64: str
    next_offset: int | None
    size_bytes: int
    sha256: str
