"""Durable bounded plan and attempt snapshots under TaskEngine ownership."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from agentforge.db.models import WorkflowRecord
from agentforge.db.tasks import TaskRepository
from agentforge.workflows.models import StepRun, Workflow, WorkflowError


def digest(value):
    import json

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class WorkflowRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def submit(self, definition, key, client_id, bindings, targets=()):
        owner = "local" if client_id is None else "client:" + client_id
        request_hash = digest(definition.model_dump(mode="json"))
        with self.sessions() as session:
            row = session.scalar(
                select(WorkflowRecord).where(
                    WorkflowRecord.owner_scope == owner,
                    WorkflowRecord.idempotency_key == key,
                )
            )
            if row:
                if row.request_hash != request_hash:
                    raise WorkflowError("idempotency_conflict")
                return Workflow.model_validate(row.payload)
            if (
                session.scalar(
                    select(func.count())
                    .select_from(WorkflowRecord)
                    .where(
                        WorkflowRecord.state.not_in(
                            ("completed", "failed", "cancelled")
                        )
                    )
                )
                >= 128
            ):
                raise WorkflowError("workflow_capacity")
            now = datetime.now(UTC)
            value = Workflow(
                workflow_id=uuid4(),
                client_id=client_id,
                idempotency_key=key,
                definition=definition,
                created_at=now,
                updated_at=now,
                steps=tuple(StepRun(step_id=s.step_id) for s in definition.steps),
                bindings=bindings,
                targets=targets,
            )
            session.add(
                WorkflowRecord(
                    workflow_id=value.workflow_id,
                    owner_scope=owner,
                    idempotency_key=key,
                    request_hash=request_hash,
                    state=value.state,
                    created_at=now,
                    payload=value.model_dump(mode="json"),
                )
            )
            TaskRepository._commit(session)
            return value

    def get(self, identity):
        with self.sessions() as session:
            row = session.get(WorkflowRecord, identity)
            if row is None:
                raise WorkflowError("workflow_not_found")
            return Workflow.model_validate(row.payload)

    def list(self, *, active=False, limit=100, offset=0):
        with self.sessions() as session:
            query = select(WorkflowRecord).order_by(
                WorkflowRecord.created_at, WorkflowRecord.workflow_id
            )
            if active:
                query = query.where(
                    WorkflowRecord.state.not_in(("completed", "failed", "cancelled"))
                )
            return tuple(
                Workflow.model_validate(r.payload)
                for r in session.scalars(query.limit(limit).offset(offset))
            )

    def save(self, value):
        value = value.model_copy(update={"updated_at": datetime.now(UTC)})
        with self.sessions() as session:
            row = session.get(WorkflowRecord, value.workflow_id)
            row.state, row.payload = value.state, value.model_dump(mode="json")
            TaskRepository._commit(session)
        return value
