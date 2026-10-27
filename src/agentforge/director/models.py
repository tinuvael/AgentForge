"""Private recovery records and hard resource bounds."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Limits(Record):
    turns: int = Field(default=12, ge=1, le=24)
    calls: int = Field(default=96, ge=4, le=192)
    tasks: int = Field(default=3, ge=1, le=8)
    seconds: float = Field(default=240, ge=1, le=600, allow_inf_nan=False)
    context_bytes: int = Field(default=96_000, ge=4096, le=192_000)
    result_bytes: int = Field(default=24_000, ge=1024, le=48_000)
    input_chars: int = Field(default=8000, ge=1, le=16_000)
    output_chars: int = Field(default=8000, ge=1, le=16_000)
    sessions: int = Field(default=32, ge=1, le=64)
    active: int = Field(default=2, ge=1, le=8)
    reconnects: int = Field(default=1, ge=0, le=2)


class VisibleMessage(Record):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=16_000)


class Action(Record):
    nonce: str = Field(max_length=64)
    operation: str = Field(max_length=100)
    arguments: dict
    project_id: str
    task_id: str | None = None
    expires: float
    reason: str = "Explicit approval is required for this privileged action."


class TaskBinding(Record):
    task_id: UUID
    project_id: UUID
    agent_id: str
    worker_id: str
    state: str = "queued"
    evidence: Literal["unverified", "source_tools_observed", "no_source_evidence"] = (
        "unverified"
    )


class CouncilBinding(Record):
    council_id: UUID
    project_id: UUID
    agent_id: str
    task_ids: list[UUID] = Field(max_length=8)


class WorkflowBinding(Record):
    workflow_id: UUID
    project_id: UUID
    state: str


class Conversation(Record):
    id: UUID = Field(default_factory=uuid4)
    owner: str
    gateway_identity: str
    client_id: str | None = None
    title: str = Field(default="New conversation", max_length=100)
    state: Literal[
        "idle",
        "running",
        "approval",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "ambiguous",
    ] = "idle"
    messages: list[VisibleMessage] = Field(default_factory=list, max_length=24)
    tasks: list[TaskBinding] = Field(default_factory=list, max_length=16)
    workflows: list[WorkflowBinding] = Field(default_factory=list, max_length=8)
    councils: list[CouncilBinding] = Field(default_factory=list, max_length=8)
    project_id: UUID | None = None
    worker_id: str | None = None
    pending: Action | None = None
    pending_workflow: dict | None = None
    submission: bool = False
    progress: list[str] = Field(default_factory=list, max_length=32)
    error: str | None = None
    revision: int = 0
    request_id: UUID | None = None
    turns: int = 0
    calls: int = 0
    delegated: int = 0
