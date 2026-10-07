"""Short consistent reads; no Council execution state or telemetry copies."""

from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError

from agentforge.councils.models import (
    Council,
    CouncilHistoryItem,
    CouncilNotFound,
    CouncilParticipant,
    CouncilSnapshot,
    InvalidCouncil,
)
from agentforge.db.models import (
    CouncilParticipantRecord,
    CouncilRecord,
    ProjectRecord,
    TaskRecord,
)
from agentforge.db.tasks import _utc
from agentforge.tasks.models import TaskStorageError

STATES = ("queued", "running", "completed", "failed", "cancelled")


def snapshot(
    council: Council, participants: tuple[CouncilParticipant, ...]
) -> CouncilSnapshot:
    counts = {state: sum(p.state == state for p in participants) for state in STATES}
    return CouncilSnapshot(
        **council.model_dump(exclude={"request"}),
        participants=participants,
        participant_counts=counts,
        terminal=not counts["queued"] and not counts["running"],
    )


class CouncilRepository:
    def __init__(self, sessions):
        self._sessions = sessions

    @contextmanager
    def _session(self):
        try:
            with self._sessions() as session:
                yield session
        except SQLAlchemyError:
            raise TaskStorageError("Could not access Council storage") from None

    def get(self, council_id: UUID) -> tuple[Council, CouncilSnapshot]:
        with self._session() as session:
            row = session.get(CouncilRecord, council_id)
            if row is None:
                raise CouncilNotFound("Council ID is not registered")
            council = Council(
                **{
                    name: _utc(getattr(row, name))
                    if name == "created_at"
                    else getattr(row, name)
                    for name in Council.model_fields
                }
            )
            task = TaskRecord
            # Read final answers alone from execution JSON; traces never enter this
            # projection. The same transaction covers membership and Task outcomes.
            query = (
                select(
                    task.worker_id,
                    task.task_id,
                    task.state,
                    task.provider,
                    task.model,
                    case(
                        (
                            task.state == "completed",
                            task.execution_result["final_answer"].as_string(),
                        ),
                        else_=None,
                    ).label("final_answer"),
                    task.reason,
                    task.error_code,
                    task.cancellation_requested_at,
                    task.telemetry_status,
                )
                .join(
                    CouncilParticipantRecord,
                    task.task_id == CouncilParticipantRecord.task_id,
                )
                .where(CouncilParticipantRecord.council_id == council_id)
                .order_by(CouncilParticipantRecord.ordinal)
            )
            participants = tuple(
                CouncilParticipant(
                    **dict(values)
                    | {
                        "cancellation_requested_at": _utc(
                            values["cancellation_requested_at"]
                        )
                    }
                )
                for values in session.execute(query).mappings()
            )
            return council, snapshot(council, participants)

    def for_task(self, task_id: UUID) -> UUID | None:
        with self._session() as session:
            return session.scalar(
                select(CouncilParticipantRecord.council_id).where(
                    CouncilParticipantRecord.task_id == task_id
                )
            )

    def history(self, *, limit: int, offset: int) -> list[CouncilHistoryItem]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 101
            or type(offset) is not int
            or not 0 <= offset <= 1_000_000
        ):
            raise InvalidCouncil("Invalid Council history bounds")
        # One extra lookahead row for bounded pagination. Query avoids request,
        # answer and execution JSON even for a full history page.
        council, member, task = CouncilRecord, CouncilParticipantRecord, TaskRecord
        counts = [
            func.sum(case((task.state == state, 1), else_=0)).label(state)
            for state in STATES
        ]
        query = (
            select(
                council.council_id,
                council.project_id,
                council.agent_id,
                council.created_at,
                ProjectRecord.name.label("project_name"),
                func.count(task.task_id).label("participant_count"),
                *counts,
            )
            .join(member, member.council_id == council.council_id)
            .join(task, task.task_id == member.task_id)
            .outerjoin(ProjectRecord, ProjectRecord.id == council.project_id)
            .group_by(
                council.council_id,
                council.project_id,
                council.agent_id,
                council.created_at,
                ProjectRecord.name,
            )
            .order_by(council.created_at.desc(), council.council_id.desc())
            .limit(limit)
            .offset(offset)
        )
        with self._session() as session:
            items = []
            for values in session.execute(query).mappings():
                counts = {state: values[state] for state in STATES}
                items.append(
                    CouncilHistoryItem(
                        **{
                            key: value
                            for key, value in values.items()
                            if key not in STATES and key != "created_at"
                        },
                        created_at=_utc(values["created_at"]),
                        participant_counts=counts,
                        terminal=not counts["queued"] and not counts["running"],
                    )
                )
            return items
