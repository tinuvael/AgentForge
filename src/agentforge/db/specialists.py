"""Short specialist transactions in the executor's existing SQLite database."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from agentforge.db.models import ArtifactRecord, SpecialistStoreRecord, ToolJobRecord
from agentforge.specialists.models import (
    Artifact,
    ExecutionEvidence,
    SpecialistError,
    ToolJob,
)


def request_digest(arguments):
    # Preserve Phase A request identities when new optional parameters are added.
    value = arguments.model_dump(mode="json")
    value["parameters"] = {
        "seed": arguments.parameters.seed,
        **arguments.parameters.model_dump(exclude_defaults=True),
    }
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class SpecialistRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    @staticmethod
    def commit(session):
        try:
            session.commit()
        except SQLAlchemyError:
            session.invalidate()
            raise

    @staticmethod
    def writer(session):
        session.execute(
            update(SpecialistStoreRecord)
            .values(id=1)
            .where(SpecialistStoreRecord.id == 1)
        )

    def pin_store(self, path, identity):
        with self.sessions() as session:
            row = session.get(SpecialistStoreRecord, 1)
            if row is None:
                session.add(
                    SpecialistStoreRecord(
                        id=1, root_path=str(path), root_identity=identity
                    )
                )
                self.commit(session)
            elif (row.root_path, row.root_identity) != (str(path), identity):
                raise SpecialistError("artifact_store_changed")

    def submit(self, arguments, tool, client_id):
        scope = "client:" + client_id if client_id is not None else "local"
        digest = request_digest(arguments)
        with self.sessions() as session:
            self.writer(session)
            existing = session.scalar(
                select(ToolJobRecord).where(
                    ToolJobRecord.owner_scope == scope,
                    or_(
                        ToolJobRecord.idempotency_key == arguments.idempotency_key,
                        ToolJobRecord.request_id == arguments.request_id,
                    ),
                )
            )
            if existing:
                if existing.request_hash != digest:
                    raise SpecialistError("idempotency_conflict")
                return ToolJob.model_validate(existing.payload)
            active = session.scalar(
                select(func.count())
                .select_from(ToolJobRecord)
                .where(ToolJobRecord.state.in_(("queued", "running")))
            )
            requests = session.scalar(
                select(func.count())
                .select_from(ToolJobRecord)
                .where(ToolJobRecord.tool_id == tool.tool_id)
            )
            if active >= 256 or requests >= tool.limits.max_requests:
                raise SpecialistError("specialist_resource_limit")
            job = ToolJob(
                **arguments.model_dump(),
                tool_job_id=uuid4(),
                client_id=client_id,
                kind=tool.kind,
                adapter=tool.adapter,
                connection_id=tool.connection_id,
                authorized_host=tool.authorized_host,
                connection_origin=str(tool.comfyui.base_url) if tool.comfyui else None,
                trust=tool.trust,
                limits=tool.limits,
                simulated=tool.simulated,
                state="queued",
                created_at=datetime.now(UTC),
                tool_fingerprint=tool.fingerprint(),
            )
            session.add(
                ToolJobRecord(
                    tool_job_id=job.tool_job_id,
                    project_id=job.project_id,
                    tool_id=job.tool_id,
                    owner_scope=scope,
                    idempotency_key=job.idempotency_key,
                    request_id=job.request_id,
                    request_hash=digest,
                    state=job.state,
                    created_at=job.created_at,
                    payload=job.model_dump(mode="json"),
                )
            )
            self.commit(session)
            return job

    def existing(self, arguments, client_id):
        scope = "client:" + client_id if client_id is not None else "local"
        digest = request_digest(arguments)
        with self.sessions() as session:
            rows = session.scalars(
                select(ToolJobRecord).where(
                    ToolJobRecord.owner_scope == scope,
                    or_(
                        ToolJobRecord.idempotency_key == arguments.idempotency_key,
                        ToolJobRecord.request_id == arguments.request_id,
                    ),
                )
            ).all()
            if not rows:
                return None
            if len(rows) != 1 or rows[0].request_hash != digest:
                raise SpecialistError("idempotency_conflict")
            return ToolJob.model_validate(rows[0].payload)

    def recover_running(self):
        while jobs := self.jobs(states=("running",), limit=100):
            for job in jobs:
                self.transition(
                    job.tool_job_id, state="interrupted", failure="recovery_required"
                )

    def get(self, identity):
        with self.sessions() as session:
            row = session.get(ToolJobRecord, identity)
            if row is None:
                raise SpecialistError("tool_job_not_found")
            return ToolJob.model_validate(row.payload)

    def evidence(self, identity, **fields):
        """Commit intent before I/O and receipt before polling; never store bodies."""
        with self.sessions() as session:
            self.writer(session)
            row = session.get(ToolJobRecord, identity)
            job = ToolJob.model_validate(row.payload)
            value = ExecutionEvidence.model_validate(
                {
                    **job.execution.model_dump(),
                    **fields,
                    "updated_at": datetime.now(UTC),
                }
            )
            if (
                job.execution.remote_job_id
                and value.remote_job_id != job.execution.remote_job_id
            ):
                raise SpecialistError("execution_receipt_changed")
            if (
                job.execution.intent_sha256
                and value.intent_sha256 != job.execution.intent_sha256
            ):
                raise SpecialistError("execution_intent_changed")
            if job.execution.validated_output_sha256 and (
                value.validated_output_sha256,
                value.validated_output_bytes,
            ) != (
                job.execution.validated_output_sha256,
                job.execution.validated_output_bytes,
            ):
                raise SpecialistError("execution_output_changed")
            job = job.model_copy(update={"execution": value})
            row.payload = job.model_dump(mode="json")
            self.commit(session)
            return job

    def reconcile(self, identity):
        """Only an existing ComfyUI receipt can re-enter the shared queue."""
        with self.sessions() as session:
            self.writer(session)
            row = session.get(ToolJobRecord, identity)
            job = ToolJob.model_validate(row.payload)
            if (
                job.state != "interrupted"
                or job.adapter != "comfyui"
                or not job.execution.remote_job_id
                or job.cancellation_requested_at
            ):
                raise SpecialistError("recovery_required")
            active = session.scalar(
                select(func.count())
                .select_from(ToolJobRecord)
                .where(ToolJobRecord.state.in_(("queued", "running")))
            )
            if active >= 256:
                raise SpecialistError("specialist_resource_limit")
            job = job.model_copy(
                update={
                    "state": "queued",
                    "finished_at": None,
                    "failure_category": None,
                }
            )
            row.state, row.payload = job.state, job.model_dump(mode="json")
            self.commit(session)
            return job

    def jobs(self, *, project_id=None, limit=100, offset=0, states=None):
        with self.sessions() as session:
            query = select(ToolJobRecord).order_by(
                ToolJobRecord.created_at, ToolJobRecord.tool_job_id
            )
            if project_id is not None:
                query = query.where(ToolJobRecord.project_id == project_id)
            if states:
                query = query.where(ToolJobRecord.state.in_(states))
            return [
                ToolJob.model_validate(row.payload)
                for row in session.scalars(query.limit(limit).offset(offset))
            ]

    def transition(self, identity, *, state, failure=None, output_ids=()):
        with self.sessions() as session:
            self.writer(session)
            row = session.get(ToolJobRecord, identity)
            if row is None:
                raise SpecialistError("tool_job_not_found")
            job = ToolJob.model_validate(row.payload)
            if job.state not in {"queued", "running"}:
                return job
            now = datetime.now(UTC)
            if state == "running" and job.state != "queued":
                raise SpecialistError("invalid_tool_job_state")
            fields = {"state": state}
            if state == "running":
                fields["started_at"] = job.started_at or now
            elif state == "cancel_requested":
                fields = {"cancellation_requested_at": now}
                if job.state == "queued":
                    fields.update(state="cancelled", finished_at=now)
            else:
                fields.update(finished_at=now, failure_category=failure)
                if job.cancellation_requested_at:
                    fields.update(state="cancelled", failure_category=None)
                elif state == "completed":
                    if job.state != "running" or not output_ids:
                        raise SpecialistError("invalid_tool_job_state")
                    for aid in output_ids:
                        artifact = session.get(ArtifactRecord, aid)
                        if (
                            artifact is None
                            or artifact.tool_job_id != identity
                            or artifact.state != "staged"
                        ):
                            raise SpecialistError("artifact_integrity")
                        artifact.state = "ready"
                    fields["output_artifact_ids"] = output_ids
            job = job.model_copy(update=fields)
            row.state, row.payload = job.state, job.model_dump(mode="json")
            self.commit(session)
            return job

    def reserve(self, descriptor, config):
        with self.sessions() as session:
            self.writer(session)
            count, used = session.execute(
                select(
                    func.count(), func.coalesce(func.sum(ArtifactRecord.size_bytes), 0)
                ).where(ArtifactRecord.state != "expired")
            ).one()
            if (
                count >= config.max_artifacts
                or used + descriptor.size_bytes > config.max_storage_bytes
            ):
                raise SpecialistError("specialist_resource_limit")
            session.add(
                ArtifactRecord(
                    artifact_id=descriptor.artifact_id,
                    project_id=descriptor.project_id,
                    tool_job_id=descriptor.tool_job_id,
                    state="staged",
                    size_bytes=descriptor.size_bytes,
                    descriptor=descriptor.model_dump(mode="json"),
                )
            )
            self.commit(session)

    def artifact(self, project_id, artifact_id):
        with self.sessions() as session:
            row = session.get(ArtifactRecord, artifact_id)
            if row is None or row.project_id != project_id or row.state == "staged":
                raise SpecialistError("artifact_not_found")
            value = Artifact.model_validate(row.descriptor)
            return (
                value.model_copy(update={"status": "expired"})
                if row.state == "expired"
                else value
            )

    def artifacts(
        self,
        *,
        project_id=None,
        limit=100,
        offset=0,
        staged=False,
        authorized_ids=None,
        client_id=None,
    ):
        with self.sessions() as session:
            query = select(ArtifactRecord).order_by(ArtifactRecord.artifact_id)
            query = query.where(
                ArtifactRecord.state == "staged"
                if staged
                else ArtifactRecord.state != "staged"
            )
            if project_id is not None:
                query = query.where(ArtifactRecord.project_id == project_id)
            if authorized_ids is not None or client_id is not None:
                query = query.join(
                    ToolJobRecord,
                    ToolJobRecord.tool_job_id == ArtifactRecord.tool_job_id,
                )
                if authorized_ids is not None:
                    query = query.where(ToolJobRecord.tool_id.in_(authorized_ids))
                if client_id is not None:
                    query = query.where(
                        ToolJobRecord.owner_scope == "client:" + client_id
                    )
            return [
                Artifact.model_validate(r.descriptor).model_copy(
                    update={"status": "expired" if r.state == "expired" else "ready"}
                )
                for r in session.scalars(query.limit(limit).offset(offset))
            ]

    def expire(self, artifact_id):
        with self.sessions() as session:
            session.execute(
                update(ArtifactRecord)
                .where(ArtifactRecord.artifact_id == artifact_id)
                .values(state="expired")
            )
            self.commit(session)
