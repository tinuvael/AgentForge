"""Bounded director-facing contracts, independent of MCP protocol/transport."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
)

from agentforge.agents.models import RuntimeLimits
from agentforge.coding.models import CodingResult
from agentforge.core.capabilities import Capability, CapabilityDescriptor
from agentforge.core.policy import ResourceBudget, TaskPolicy
from agentforge.councils.models import MAX_PARTICIPANTS, CouncilHistoryItem
from agentforge.tasks.models import TaskReason, TaskState
from agentforge.tasks.observation import TRACE_LIMIT, Notice, TimelineEvent

Identifier = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=100)
]
RequestText = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=32_768, pattern=r"\S")
]
ErrorCode = Literal[
    "access_denied",
    "resource_limit",
    "policy_denied",
    "invalid_arguments",
    "coding_unavailable",
    "project_not_found",
    "worker_not_found",
    "agent_not_found",
    "invalid_execution_binding",
    "task_not_found",
    "invalid_council",
    "council_not_found",
    "storage_unavailable",
    "service_unavailable",
    "internal_error",
    "specialist_unavailable",
    "specialist_error",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NoArguments(Contract):
    pass


class PageArguments(Contract):
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 100
    offset: Annotated[StrictInt, Field(ge=0, le=1_000_000)] = 0


class DelegateArguments(Contract):
    project_id: UUID
    agent_id: Identifier
    worker_id: Identifier
    task: RequestText
    task_policy: TaskPolicy = TaskPolicy()


class TaskArguments(Contract):
    task_id: UUID


class CleanupCodingArguments(TaskArguments):
    workspace_id: UUID


class DelegateCouncilArguments(Contract):
    project_id: UUID
    agent_id: Identifier
    task: RequestText
    task_policy: TaskPolicy = TaskPolicy()
    worker_ids: tuple[Identifier, ...] = Field(
        min_length=2, max_length=MAX_PARTICIPANTS
    )

    @field_validator("worker_ids")
    @classmethod
    def distinct_workers(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Worker IDs must be distinct")
        return value


class CouncilArguments(Contract):
    council_id: UUID


class CouncilsPage(Contract):
    councils: tuple[CouncilHistoryItem, ...] = Field(max_length=100)
    next_offset: int | None


class SafeError(Contract):
    code: ErrorCode
    message: str


class Status(Contract):
    client_id: str | None = None
    available: bool
    version: str
    database_available: Literal[True] = True
    task_engine_available: bool
    project_count: int
    worker_count: int
    agent_count: int
    queued_tasks: int
    running_tasks: int


class Capabilities(Contract):
    responsibility: str
    worker_selection: Literal["explicit_project_agent_worker_required"]
    agent_discovery_tool: Literal["list_agents"] = "list_agents"
    worker_discovery_tool: Literal["list_workers"] = "list_workers"
    task_operations: tuple[str, ...]
    task_progress: Literal["request_scoped_mcp_progress; bounded_safe_snapshots"] = (
        "request_scoped_mcp_progress; bounded_safe_snapshots"
    )
    advisory_operations: tuple[str, ...] = ("suggest_workers",)
    council_operations: tuple[str, ...]
    coding_operations: tuple[str, ...] = ()
    specialist_operations: tuple[str, ...] = ()
    council_max_participants: Literal[16] = MAX_PARTICIPANTS
    repository_access: Literal[
        "central_host_agent_allowlisted_read_only",
        "central_host_agent_allowlisted_isolated_write",
    ]
    telemetry: Literal["terminal_task_status_only; metrics_via_python_service"]
    limitations: tuple[str, ...]


class ProjectInfo(Contract):
    project_id: UUID
    name: str = Field(max_length=255)
    root_path: str = Field(max_length=4096)
    created_at: datetime
    git_status: Literal["unavailable"] = "unavailable"
    git_unavailable_reason: Literal["not_probed"] = "not_probed"


class WorkerInfo(Contract):
    worker_id: Identifier
    kind: Literal["inference_worker"] = "inference_worker"
    transport: Literal["text_and_tools"] = "text_and_tools"
    provider_connection: str | None = None
    trust: Literal["local", "cloud", "unknown"] = "unknown"
    capabilities: dict[Capability, CapabilityDescriptor] = Field(default_factory=dict)
    provider: Identifier
    model: str = Field(max_length=512)
    context_window: int | None
    supports_tools: bool | None
    supports_streaming: bool
    deployment_label: str | None = Field(max_length=512)
    health_status: Literal["not_probed"] = "not_probed"


class AgentInfo(Contract):
    agent_id: Identifier
    name: str = Field(max_length=200)
    description: str = Field(max_length=2000)
    allowed_tools: tuple[Identifier, ...] = Field(max_length=100)
    limits: RuntimeLimits
    workspace_mode: Literal["project_readonly", "isolated_write"] = "project_readonly"


class ProjectsPage(Contract):
    projects: tuple[ProjectInfo, ...] = Field(max_length=100)
    next_offset: int | None


class WorkersPage(Contract):
    workers: tuple[WorkerInfo, ...] = Field(max_length=100)
    next_offset: int | None


class AgentsPage(Contract):
    agents: tuple[AgentInfo, ...] = Field(max_length=100)
    next_offset: int | None


class ExecutionSummary(Contract):
    steps: int
    tool_call_count: int
    tool_output_bytes: int


class TaskSnapshot(Contract):
    provider: str | None = None
    model: str | None = None
    provider_connection: str | None = None
    task_id: UUID
    project_id: UUID
    agent_id: str
    worker_id: str
    state: TaskState
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancellation_requested_at: datetime | None
    reason: TaskReason | None
    error_code: TaskReason | None
    coding_result: CodingResult | None = None
    execution_summary: ExecutionSummary | None
    final_answer: str | None
    telemetry_status: Literal["pending", "recorded", "unavailable"]


class TaskProgress(Contract):
    """Replacement metadata snapshot, never a result/content or replay contract."""

    task_id: UUID
    state: TaskState
    reason: TaskReason | None
    error_code: TaskReason | None
    cancellation_requested: bool
    terminal: bool
    observation: Notice | Literal["snapshot"]
    resync_required: bool
    truncated: bool
    timeline: tuple[TimelineEvent, ...] = Field(max_length=TRACE_LIMIT)


class SuggestArguments(Contract):
    project_id: UUID
    agent_id: Identifier
    required: tuple[Capability, ...] = Field(default=(), max_length=8)
    preferred: tuple[Capability, ...] = Field(default=(), max_length=8)
    min_context_window: Annotated[StrictInt, Field(ge=1, le=10_000_000)] | None = None
    require_tools: bool = False
    task_policy: TaskPolicy = TaskPolicy()
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 100
    offset: Annotated[StrictInt, Field(ge=0, le=1_000_000)] = 0


class Candidate(Contract):
    worker: WorkerInfo
    eligible: bool
    hard_exclusions: tuple[Identifier, ...] = Field(max_length=16)
    unknown_capabilities: tuple[Capability, ...] = Field(max_length=8)
    preferred_supported: tuple[Capability, ...] = Field(max_length=8)
    availability: Literal["unknown", "observed_available", "observed_unavailable"]
    availability_observed_at: datetime | None
    last_failure: str | None = Field(default=None, max_length=40)
    failure_observed_at: datetime | None = None
    cooldown_until: datetime | None = None
    requests_used: int | None = None
    tokens_used: int | None = None
    token_usage_unknown: bool = False
    configured_budget: ResourceBudget
    remaining_quota: Literal["unknown"] = "unknown"
    pricing: Literal["unknown"] = "unknown"


class CandidateSuggestions(Contract):
    candidates: tuple[Candidate, ...] = Field(max_length=100)
    next_offset: int | None
    explanation: str = Field(max_length=500)
