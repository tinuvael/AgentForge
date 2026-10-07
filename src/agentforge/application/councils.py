"""Shared Council operations over ordinary Tasks; the external director judges."""

from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError

from agentforge.application.contracts import (
    CouncilsPage,
    DelegateCouncilArguments,
    PageArguments,
)
from agentforge.application.errors import ServiceError
from agentforge.councils.models import (
    CouncilNotFound,
    CouncilParticipant,
    CouncilSnapshot,
    InvalidCouncil,
)
from agentforge.db.councils import CouncilRepository, snapshot
from agentforge.tasks.models import TERMINAL_STATES
from agentforge.telemetry.models import (
    TaskTelemetry,
    TelemetryNotFound,
    TelemetryUnavailable,
)


@dataclass(frozen=True)
class CouncilDetail:
    council: CouncilSnapshot
    request: str
    answers: tuple[str | None, ...]
    text_truncated: bool
    telemetry: tuple[TaskTelemetry | None, ...]


class CouncilService:
    def __init__(self, repository, tasks, projects, agent_ids, worker_ids, telemetry):
        self._repository: CouncilRepository = repository
        self._tasks = tasks
        self._projects = projects
        self._agent_ids = frozenset(agent_ids)
        self._worker_ids = frozenset(worker_ids)
        self._telemetry = telemetry

    @staticmethod
    def _id(council_id):
        try:
            return UUID(str(council_id))
        except (ValueError, TypeError, AttributeError):
            raise CouncilNotFound("Council ID is not registered") from None

    def submit(self, **arguments) -> CouncilSnapshot:
        try:
            request = DelegateCouncilArguments.model_validate(arguments)
        except ValidationError:
            raise InvalidCouncil("Supply a valid Council request") from None
        if not self._tasks.available:
            raise ServiceError("service_unavailable")
        self._projects.get_project(request.project_id)
        if request.agent_id not in self._agent_ids:
            raise ServiceError("agent_not_found")
        if any(worker not in self._worker_ids for worker in request.worker_ids):
            raise ServiceError("worker_not_found")
        council, tasks = self._tasks.submit_council(**request.model_dump())
        return snapshot(
            council,
            tuple(
                CouncilParticipant(
                    **{
                        field: getattr(task, field)
                        for field in CouncilParticipant.model_fields
                    }
                )
                for task in tasks
            ),
        )

    def get(self, council_id: UUID | str) -> CouncilSnapshot:
        return self._repository.get(self._id(council_id))[1]

    def list(self, *, limit=25, offset=0) -> CouncilsPage:
        try:
            PageArguments(limit=limit, offset=offset)
        except ValidationError:
            raise InvalidCouncil("Invalid Council history bounds") from None
        items = self._repository.history(limit=limit + 1, offset=offset)
        return CouncilsPage(
            councils=tuple(items[:limit]),
            next_offset=offset + limit if len(items) > limit else None,
        )

    def cancel(self, council_id: UUID | str) -> CouncilSnapshot:
        current = self.get(council_id)
        for participant in current.participants:
            if participant.state not in TERMINAL_STATES:
                self._tasks.cancel_task(participant.task_id)
        # Not an atomic all-participant cancellation: durable requests already made
        # survive storage errors; retry is safe via TaskEngine's idempotent path.
        return self.get(council_id)

    def for_task(self, task_id: UUID) -> UUID | None:
        return self._repository.for_task(task_id)

    def detail(self, council_id: UUID) -> CouncilDetail:
        identity, current = self._repository.get(council_id)
        telemetry = []
        for participant in current.participants:
            try:
                telemetry.append(self._telemetry.get_for_task(participant.task_id))
            except (TelemetryNotFound, TelemetryUnavailable):
                telemetry.append(None)
        answers = tuple(
            p.final_answer[:65_536] if p.final_answer is not None else None
            for p in current.participants
        )
        return CouncilDetail(
            current,
            identity.request[:32_768],
            answers,
            len(identity.request) > 32_768
            or any(
                p.final_answer is not None and len(p.final_answer) > 65_536
                for p in current.participants
            ),
            tuple(telemetry),
        )
