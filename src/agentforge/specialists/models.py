"""Bounded, transport-independent specialist and artifact contracts."""

import hashlib
import ipaddress
import json
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

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


class WorkflowField(Model):
    node: Name
    field: Literal["seed", "text"]


class ComfyUIConfig(Model):
    base_url: HttpUrl = Field(repr=False)
    api_key_env: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_]{0,99}$", repr=False
    )
    workflow_json: str = Field(max_length=262144, repr=False)
    seed_field: WorkflowField | None = None
    text_field: WorkflowField | None = None
    output_node: Name
    # SaveImage prefix is replaced with a per-job value, never supplied by a client.
    output_prefix_field: Literal["filename_prefix"] = "filename_prefix"
    request_timeout_seconds: float = Field(default=10, gt=0, le=60)
    poll_seconds: float = Field(default=1, ge=0.01, le=30)
    max_response_bytes: int = Field(default=524288, strict=True, ge=1024, le=1048576)

    @model_validator(mode="after")
    def approved_contract(self):
        url = self.base_url
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {None, "/"}
        ):
            raise ValueError("Connection must be an origin without credentials")
        try:
            loopback = ipaddress.ip_address(url.host.strip("[]")).is_loopback
        except ValueError:
            loopback = False
        # Literal loopback avoids DNS rebinding for cleartext local operation.
        if url.scheme != "https" and not loopback:
            raise ValueError("Non-loopback specialist connections require HTTPS")
        try:
            if len(self.workflow_json.encode("utf-8")) > 262144:
                raise ValueError
            workflow = json.loads(self.workflow_json)
            if not isinstance(workflow, dict) or not 1 <= len(workflow) <= 256:
                raise ValueError
            for node in workflow.values():
                if (
                    set(node) - {"class_type", "inputs", "_meta"}
                    or not isinstance(node["class_type"], str)
                    or not isinstance(node["inputs"], dict)
                ):
                    raise ValueError
            output = workflow[self.output_node]
            if (
                output["class_type"] != "SaveImage"
                or self.output_prefix_field not in output["inputs"]
            ):
                raise ValueError
            for binding, field, kind in (
                (self.seed_field, "seed", int),
                (self.text_field, "text", str),
            ):
                if binding and (
                    binding.field != field
                    or workflow[binding.node]["class_type"]
                    != ("KSampler" if field == "seed" else "CLIPTextEncode")
                    or type(workflow[binding.node]["inputs"][field]) is not kind
                ):
                    raise ValueError
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError(
                "Invalid approved API workflow or parameter mapping"
            ) from None
        return self


class BlenderConfig(Model):
    executable: Path = Field(repr=False)
    executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    staging_directory: Path = Field(repr=False)
    operations: tuple[Literal["reference_blockout"], ...] = ("reference_blockout",)
    max_log_bytes: int = Field(default=16384, strict=True, ge=1024, le=65536)

    @model_validator(mode="after")
    def absolute_paths(self):
        if (
            not self.executable.is_absolute()
            or not self.staging_directory.is_absolute()
            or not self.operations
        ):
            raise ValueError(
                "Blender requires absolute operator paths and approved operations"
            )
        return self


class SpecialistDescription(Model):
    """Public discovery contract; contains no executable/workflow configuration."""

    tool_id: Name
    kind: Name
    adapter: Literal["fake_comfyui", "fake_blender", "comfyui", "blender"]
    connection_id: Name
    authorized_host: Literal[
        "in_process_fake", "configured_http", "application_host"
    ] = "in_process_fake"
    trust: Literal["local", "cloud"] = "local"
    enabled: bool = False
    capabilities: tuple[Name, ...] = Field(default=(), max_length=16)
    input_formats: tuple[Format, ...] = Field(default=(), max_length=8)
    output_formats: tuple[Format, ...] = Field(min_length=1, max_length=8)
    limits: ToolLimits = ToolLimits()
    simulated: bool = True
    availability: Literal["deterministic_fake_only", "configured_untested"] = (
        "deterministic_fake_only"
    )
    evidence: Literal["adapter_implemented; real_host_not_probed"] = (
        "adapter_implemented; real_host_not_probed"
    )


class SpecialistTool(SpecialistDescription):
    """Operator-owned definition. Never accept this as a client argument."""

    comfyui: ComfyUIConfig | None = Field(default=None, exclude=True, repr=False)
    blender: BlenderConfig | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def supported_contract(self):
        expected = (
            ("comfyui_image", (), ("png",))
            if self.adapter in {"fake_comfyui", "comfyui"}
            else ("blender_scene", ("png",), ("gltf",))
        )
        if (self.kind, self.input_formats, self.output_formats) != expected:
            raise ValueError("Adapter does not implement the declared formats/kind")
        real = self.adapter in {"comfyui", "blender"}
        if real:
            expected_host = (
                "configured_http" if self.adapter == "comfyui" else "application_host"
            )
            if (
                self.authorized_host != expected_host
                or self.simulated
                or self.availability != "configured_untested"
            ):
                raise ValueError(
                    "Real adapters need explicit host and untested metadata"
                )
            if (self.comfyui is not None) != (self.adapter == "comfyui") or (
                self.blender is not None
            ) != (self.adapter == "blender"):
                raise ValueError("Real adapter requires its operator configuration")
            if self.blender and self.trust != "local":
                raise ValueError(
                    "Headless Blender executes only on the Application host"
                )
        elif (
            self.comfyui
            or self.blender
            or not self.simulated
            or self.authorized_host != "in_process_fake"
            or self.availability != "deterministic_fake_only"
        ):
            raise ValueError("Fake adapter metadata must describe simulation")
        return self

    def fingerprint(self):
        if self.simulated:
            return hashlib.sha256(self.model_dump_json().encode()).hexdigest()
        binding = self.model_dump(mode="json")
        for field in ("comfyui", "blender"):
            value = getattr(self, field)
            if value:
                binding[field] = value.model_dump(mode="json")
        if self.blender:
            binding["packaged_script_sha256"] = hashlib.sha256(
                Path(__file__).with_name("blender_script.py").read_bytes()
            ).hexdigest()
        return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


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
    seed: int = Field(default=0, strict=True, ge=0, le=2**32 - 1)
    text: str = Field(default="", strict=True, max_length=2000)
    operation: Literal["reference_blockout"] | None = None
    depth: float = Field(default=0.25, strict=True, ge=0.01, le=2)

    def for_tool(self, tool):
        if tool.adapter == "comfyui":
            if (
                self.operation
                or self.depth != 0.25
                or (self.text and not tool.comfyui.text_field)
                or (self.seed and not tool.comfyui.seed_field)
            ):
                raise SpecialistError("specialist_input_invalid")
        elif tool.adapter == "blender":
            if self.operation not in tool.blender.operations or self.seed or self.text:
                raise SpecialistError("specialist_input_invalid")
        elif self.text or self.operation or self.depth != 0.25:
            raise SpecialistError("specialist_input_invalid")


class ExecutionEvidence(Model):
    phase: Literal[
        "not_submitted", "submission_unknown", "accepted", "outputs_validated"
    ] = "not_submitted"
    intent_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    remote_job_id: UUID | None = None
    remote_state: Literal["unknown", "queued", "running", "completed", "failed"] = (
        "unknown"
    )
    remote_may_continue: bool = False
    validated_output_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    validated_output_bytes: int | None = Field(
        default=None, strict=True, ge=1, le=16777216
    )
    local_exit_code: int | None = None
    logs_truncated: bool = False
    # Raw logs, remote bodies, credentials and host paths are never retained.
    updated_at: datetime | None = None


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
    authorized_host: Name = "in_process_fake"
    connection_origin: str | None = None
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
    execution: ExecutionEvidence = ExecutionEvidence()


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
    operation: Literal["reference_blockout"] | None = None
    geometry_source: Literal["known_blockout", "synthetic_fixture"] | None = None
    geometry: Literal["none", "mesh", "gaussian_splat"] = "none"
    printability: Literal["not_assessed"] = "not_assessed"
    status: Literal["ready", "expired", "missing", "corrupt"] = "ready"


class ToolPage(Model):
    tools: tuple[SpecialistDescription, ...]
    next_offset: int | None
    parameter_contracts: dict[str, tuple[str, ...]] = Field(default_factory=dict)


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
