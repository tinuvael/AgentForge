"""Compact operator projections; execution and observations stay in Application."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from agentforge.application.contracts import TaskProgress, TaskSnapshot
from agentforge.coding.models import CodingError, CodingResult
from agentforge.projects.errors import ProjectNotFound
from agentforge.tasks.observation import TimelineEvent
from agentforge.telemetry.models import TaskTelemetry

if TYPE_CHECKING:
    from agentforge.application.service import Application


def progress_label(event: TimelineEvent) -> str:
    if event.kind == "model_request":
        return f"Running model turn {event.step}"
    if event.kind == "tool_request":
        verb = {"read_file": "Reading repository with", "search_code": "Searching with"}
        action = verb.get(event.tool_name, "Running tool")
        return f"{action} {event.tool_name or 'unknown'}"
    if event.kind == "tool_result":
        return (
            "Tool completed"
            if event.success is True
            else "Tool failed"
            if event.success is False
            else "Tool finished (outcome unknown)"
        )
    if event.kind == "workspace_provisioned":
        return "Coding workspace created"
    if event.kind == "validation_started":
        return "Running validation"
    if event.kind == "validation_completed":
        return (
            "Validation passed"
            if event.success is True
            else "Validation failed"
            if event.success is False
            else "Validation finished (outcome unknown)"
        )
    if event.kind == "model_response":
        return "Model turn completed"
    return (event.reason or "Execution stopped").replace("_", " ").capitalize()


@dataclass(frozen=True)
class CompanionTask:
    task: TaskSnapshot
    project_name: str | None
    model: str | None
    elapsed_seconds: float
    progress: TaskProgress
    label: str
    tool_calls: int | None
    telemetry: TaskTelemetry | None
    coding: CodingResult | None
    council_id: UUID | None
    answer_truncated: bool


class CompanionQueries:
    def __init__(self, application: "Application"):
        self.app = application

    def project_name(self, project_id: UUID) -> str | None:
        try:
            return self.app.projects.get_project(project_id).name
        except ProjectNotFound:
            return None

    def detail(self, task_id: UUID, *, now: datetime | None = None) -> CompanionTask:
        detail = self.app.dashboard.detail(task_id)
        task = detail.task
        progress = self.app.task_progress(task_id=task_id)
        label = "Waiting" if task.state == "queued" else "Running"
        if progress.timeline:
            label = progress_label(progress.timeline[-1])
        if progress.terminal:
            label = task.state.capitalize()
        elif progress.cancellation_requested:
            label = "Cancellation requested"
        # Live counts are partial after truncation: never present them as totals.
        calls = (
            task.execution_summary.tool_call_count
            if task.execution_summary
            else None
            if progress.truncated
            else sum(e.kind == "tool_request" for e in progress.timeline)
        )
        coding = None
        if self.app.coding is not None:
            try:
                coding = self.app.get_coding_summary(task_id=task_id)
                coding = coding.model_copy(
                    update={
                        "validation_runs": tuple(
                            run.model_copy(update={"stdout": "", "stderr": ""})
                            for run in coding.validation_runs
                        )
                    }
                )
            except CodingError:
                pass
        end = task.finished_at or now or datetime.now(UTC)
        return CompanionTask(
            task.model_copy(
                update={"final_answer": detail.answer, "coding_result": None}
            ),
            detail.project_name,
            detail.model,
            max(0.0, (end - (task.started_at or task.created_at)).total_seconds()),
            progress,
            label,
            calls,
            detail.telemetry,
            coding,
            self.app.councils.for_task(task_id),
            task.final_answer is not None and len(task.final_answer) > 65_536,
        )

    def active(self, *, limit=25, offset=0) -> tuple[CompanionTask, ...]:
        # Bounded independent pages for queued and running work; no history scan.
        return tuple(
            self.detail(item.task_id)
            for state in ("running", "queued")
            for item in self.app.tasks.history(state=state, limit=limit, offset=offset)
        )
