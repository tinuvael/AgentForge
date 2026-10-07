"""Council identity and safe projections; execution state belongs to Tasks."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentforge.tasks.models import TaskReason, TaskState

MAX_PARTICIPANTS = 16


class CouncilNotFound(Exception):
    pass


class InvalidCouncil(Exception):
    pass


class Council(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    council_id: UUID
    project_id: UUID
    agent_id: str
    request: str
    created_at: datetime


class CouncilParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str
    task_id: UUID
    state: TaskState
    provider: str | None
    model: str | None
    final_answer: str | None
    reason: TaskReason | None
    error_code: TaskReason | None
    cancellation_requested_at: datetime | None
    telemetry_status: Literal["pending", "recorded", "unavailable"]


class CouncilSnapshot(BaseModel):
    """MCP omits request text, consistently with the ordinary Task contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    council_id: UUID
    project_id: UUID
    agent_id: str
    created_at: datetime
    terminal: bool
    participant_counts: dict[TaskState, int]
    participants: tuple[CouncilParticipant, ...] = Field(max_length=MAX_PARTICIPANTS)


class CouncilHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    council_id: UUID
    project_id: UUID
    project_name: str | None
    agent_id: str
    created_at: datetime
    participant_count: int
    terminal: bool
    participant_counts: dict[TaskState, int]
