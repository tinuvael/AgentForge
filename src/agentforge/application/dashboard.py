"""Transport-independent, bounded read projections over existing core services."""

from dataclasses import dataclass
from uuid import UUID

from agentforge.application.contracts import TaskSnapshot
from agentforge.projects.errors import ProjectNotFound
from agentforge.projects.service import ProjectRegistry
from agentforge.tasks.engine import TaskEngine
from agentforge.tasks.observation import TRACE_LIMIT, TimelineEvent, metadata
from agentforge.telemetry.models import (
    TaskTelemetry,
    TelemetryNotFound,
    TelemetryUnavailable,
)
from agentforge.telemetry.service import TelemetryService


@dataclass(frozen=True)
class TaskDetail:
    task: TaskSnapshot
    project_name: str | None
    request: str
    answer: str | None
    text_truncated: bool
    provider: str | None
    model: str | None
    telemetry: TaskTelemetry | None
    timeline: tuple[TimelineEvent, ...]
    timeline_truncated: bool


class DashboardQueries:
    def __init__(
        self,
        projects: ProjectRegistry,
        tasks: TaskEngine,
        telemetry: TelemetryService,
    ):
        self.projects = projects
        self.tasks = tasks
        self.telemetry = telemetry

    def detail(self, task_id: UUID) -> TaskDetail:
        # Import here to keep Application composition independent of this projection.
        from agentforge.application.service import snapshot

        task = self.tasks.get_task(task_id)
        try:
            project_name = self.projects.get_project(task.project_id).name
        except ProjectNotFound:
            project_name = None  # Historical identity survives deregistration.
        try:
            telemetry = self.telemetry.get_for_task(task_id)
        except (TelemetryNotFound, TelemetryUnavailable):
            telemetry = None
        if task.execution_result is not None:
            trace = task.execution_result.trace
            timeline = tuple(metadata(event) for event in trace[-TRACE_LIMIT:])
            truncated = len(trace) > TRACE_LIMIT
        else:
            timeline, truncated = self.tasks.observer.timeline(task_id)
        answer = task.final_answer
        return TaskDetail(
            snapshot(task),
            project_name,
            task.request[:32_768],
            answer[:65_536] if answer is not None else None,
            len(task.request) > 32_768 or answer is not None and len(answer) > 65_536,
            task.provider,
            task.model,
            telemetry,
            timeline,
            truncated,
        )
