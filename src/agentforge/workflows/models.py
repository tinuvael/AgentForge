"""Immutable, bounded workflow contracts. No expressions or implicit bindings."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.agents.models import RuntimeLimits
from agentforge.core.policy import TaskPolicy
from agentforge.specialists.models import Parameters, ToolLimits

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
State = Literal[
    "pending",
    "approval",
    "queued",
    "running",
    "completed",
    "failed",
    "paused",
    "cancelling",
    "cancelled",
    "blocked",
    "recovery_required",
]
TERMINAL = {"completed", "failed", "cancelled", "recovery_required"}


class WorkflowError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class TextReference(Model):
    step_id: Name
    max_chars: int = Field(default=2000, strict=True, ge=1, le=4000)


class StepArtifact(Model):
    source: Literal["step"] = "step"
    step_id: Name
    output: int = Field(default=0, strict=True, ge=0, le=7)
    format: Literal["png", "gltf"]


class ExistingArtifact(Model):
    source: Literal["artifact"]
    artifact_id: UUID
    producer_job_id: UUID
    format: Literal["png", "gltf"]


ArtifactReference = Annotated[
    StepArtifact | ExistingArtifact, Field(discriminator="source")
]


class Retry(Model):
    max_attempts: int = Field(default=1, strict=True, ge=1, le=3)
    # Only safe read-only inference and simulated specialist failures are eligible.
    on: tuple[
        Literal["provider_error", "provider_timeout", "specialist_adapter_failed"], ...
    ] = Field(default=(), max_length=3)


class Step(Model):
    step_id: Name
    depends_on: tuple[Name, ...] = Field(default=(), max_length=8)
    retry: Retry = Retry()
    approval_required: bool = False
    intended_effect: str = Field(min_length=1, max_length=1000)


class InferenceStep(Step):
    kind: Literal["inference"]
    agent_id: Name
    worker_id: Name
    request: str = Field(min_length=1, max_length=8000)
    text_inputs: tuple[TextReference, ...] = Field(default=(), max_length=4)
    limits: RuntimeLimits = RuntimeLimits(
        max_steps=8, timeout_seconds=90.0, max_tool_calls=12
    )
    task_policy: TaskPolicy = TaskPolicy()
    required_validators: tuple[Name, ...] = Field(default=(), max_length=8)


class SpecialistStep(Step):
    kind: Literal["specialist"]
    tool_id: Name
    parameters: Parameters = Parameters()
    artifact_inputs: tuple[ArtifactReference, ...] = Field(default=(), max_length=8)
    limits: ToolLimits = ToolLimits()


WorkflowStep = Annotated[InferenceStep | SpecialistStep, Field(discriminator="kind")]


class Definition(Model):
    project_id: UUID
    steps: tuple[WorkflowStep, ...] = Field(min_length=1, max_length=16)
    max_attempts: int = Field(default=16, strict=True, ge=1, le=48)
    timeout_seconds: int = Field(default=3600, strict=True, ge=1, le=86400)

    @model_validator(mode="after")
    def graph(self):
        steps = {s.step_id: s for s in self.steps}
        if len(steps) != len(self.steps):
            raise ValueError("Duplicate step ID")
        if sum(s.retry.max_attempts for s in self.steps) > self.max_attempts:
            raise ValueError("Attempt budget is smaller than the declared plan")
        seen = set()
        for step in self.steps:
            if len(set(step.depends_on)) != len(step.depends_on) or any(
                d not in seen for d in step.depends_on
            ):
                raise ValueError("Dependencies must name distinct earlier steps")
            refs = (
                step.text_inputs if step.kind == "inference" else step.artifact_inputs
            )
            for ref in refs:
                if isinstance(ref, ExistingArtifact):
                    continue
                if ref.step_id not in step.depends_on:
                    raise ValueError(
                        "References require an explicit earlier dependency"
                    )
                producer = steps[ref.step_id]
                if (isinstance(ref, TextReference)) != (producer.kind == "inference"):
                    raise ValueError("Incompatible producer reference")
            seen.add(step.step_id)
        if len(self.model_dump_json().encode()) > 65536:
            raise ValueError("Definition exceeds 64 KiB")
        return self


class SubmitWorkflow(Model):
    definition: Definition
    idempotency_key: Name


class WorkflowQuery(Model):
    workflow_id: UUID


class Attempt(Model):
    number: int
    dispatch_id: UUID
    task_id: UUID | None = None
    tool_job_id: UUID | None = None
    state: State = "pending"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_category: str | None = None
    result_text: str | None = Field(default=None, max_length=4000)
    artifact_ids: tuple[UUID, ...] = ()
    # Exact resolved text/IDs, never binary content or private paths.
    inputs: dict = Field(default_factory=dict)
    scope_hash: str
    effects: tuple[str, ...] = ()
    deadline_limited: bool = False
    approved_at: datetime | None = None


class StepRun(Model):
    step_id: Name
    state: State = "pending"
    failure_category: str | None = None
    attempts: tuple[Attempt, ...] = ()


class Workflow(Model):
    workflow_id: UUID
    client_id: str | None
    idempotency_key: Name
    definition: Definition
    state: State = "pending"
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    failure_category: str | None = None
    steps: tuple[StepRun, ...]
    # Immutable execution fingerprints captured before acceptance.
    bindings: tuple[str, ...]
    targets: tuple[dict, ...] = ()


class ReviewScope(Model):
    workflow_id: UUID
    project_id: UUID
    client_id: str | None
    step: WorkflowStep
    attempt: Attempt
    binding: str
    target: dict = Field(default_factory=dict)
    effects: tuple[str, ...]
