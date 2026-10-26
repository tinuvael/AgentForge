"""Specialist application behavior, executed only by shared TaskEngine slots."""

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import uuid4

from agentforge.projects.errors import ProjectError
from agentforge.specialists.adapters import ADAPTERS
from agentforge.specialists.filesystem import PrivateFiles
from agentforge.specialists.formats import validate
from agentforge.specialists.models import (
    Artifact,
    ArtifactChunk,
    ArtifactPage,
    ArtifactQuery,
    JobPage,
    RetrieveArtifact,
    SpecialistError,
    SubmitToolJob,
    ToolPage,
    ToolQuery,
)


class SpecialistService:
    def __init__(self, config, repository, projects):
        self.config, self.repository, self.projects = config, repository, projects
        self.tools = {tool.tool_id: tool for tool in config.tools}
        self.files = PrivateFiles(config.artifact_directory)
        self._active = {}
        self.engine = None

    def _private(self):
        path = self.files.path
        if any((parent / ".git").exists() for parent in (path, *path.parents)):
            raise SpecialistError("artifact_store_unsafe")
        for project in self.projects.list_projects():
            if path.is_relative_to(
                project.root_path
            ) or project.root_path.is_relative_to(path):
                raise SpecialistError("artifact_store_unsafe")
        with self.files.root():
            pass

    def authorize(self, project_id, tool_id=None):
        self._private()
        # Live root identity, not simply the historical project UUID.
        with self.projects.open_root(project_id) as (project, _):
            if tool_id is None:
                return project
            tool = self.tools.get(tool_id)
            if tool is None or not tool.enabled:
                raise SpecialistError("specialist_unavailable")
            if tool.trust == "cloud" and (
                project.egress_policy.mode != "cloud_allowed"
                or tool.connection_id not in project.egress_policy.allowed_connections
            ):
                raise SpecialistError("specialist_policy_denied")
            return tool

    def recover(self):
        self._private()
        self.repository.pin_store(self.files.path, self.files.identity.as_json())
        # Queued work may run only after current policy + fingerprint revalidation.
        self.repository.recover_running()
        while staged := self.repository.artifacts(staged=True):
            for artifact in staged:
                self.files.remove(artifact.artifact_id)
                self.repository.expire(artifact.artifact_id)
        self.cleanup()

    def list_tools(self, *, authorized_ids=None, **fields):
        query = ToolQuery.model_validate(fields)
        self.authorize(query.project_id)
        values = []
        for tool in sorted(self.tools.values(), key=lambda t: t.tool_id):
            if authorized_ids is not None and tool.tool_id not in authorized_ids:
                continue
            try:
                self.authorize(query.project_id, tool.tool_id)
            except SpecialistError as error:
                if error.code in {"specialist_unavailable", "specialist_policy_denied"}:
                    continue
                raise
            values.append(tool)
        return ToolPage(
            tools=tuple(values[query.offset : query.offset + query.limit]),
            next_offset=query.offset + query.limit
            if len(values) > query.offset + query.limit
            else None,
        )

    def submit(self, *, client_id=None, **fields):
        arguments = SubmitToolJob.model_validate(fields)
        tool = self.authorize(arguments.project_id, arguments.tool_id)
        if existing := self.repository.existing(arguments, client_id):
            return existing
        expected = 0 if tool.adapter == "fake_comfyui" else 1
        if len(arguments.input_artifact_ids) != expected:
            raise SpecialistError("specialist_input_invalid")
        for aid in arguments.input_artifact_ids:
            artifact = self.inspect(project_id=arguments.project_id, artifact_id=aid)
            if artifact.status != "ready" or artifact.format not in tool.input_formats:
                raise SpecialistError("specialist_input_invalid")
        if arguments.task_id is not None:
            task = self.engine.get_task(arguments.task_id)
            if task.project_id != arguments.project_id or task.client_id != client_id:
                raise SpecialistError("specialist_input_invalid")
        with self.engine.storage_access():
            job = self.repository.submit(arguments, tool, client_id)
            self.engine.work_changed()
            return job

    def get(self, *, tool_job_id):
        job = self.repository.get(tool_job_id)
        self.authorize(job.project_id)
        return job

    def cancel(self, *, tool_job_id):
        self.get(tool_job_id=tool_job_id)
        with self.engine.storage_access():
            job = self.repository.transition(tool_job_id, state="cancel_requested")
            if child := self._active.get(tool_job_id):
                child.cancel()
            self.engine.work_changed()
            return job

    def jobs(self, **fields):
        query = ToolQuery.model_validate(fields)
        self.authorize(query.project_id)
        values = self.repository.jobs(
            project_id=query.project_id, limit=query.limit + 1, offset=query.offset
        )
        return JobPage(
            jobs=tuple(values[: query.limit]),
            next_offset=query.offset + query.limit
            if len(values) > query.limit
            else None,
        )

    def inspect(self, **fields):
        query = ArtifactQuery.model_validate(fields)
        self.authorize(query.project_id)
        artifact = self.repository.artifact(query.project_id, query.artifact_id)
        if artifact.status == "expired" or datetime.now(UTC) >= artifact.expires_at:
            return artifact.model_copy(update={"status": "expired"})
        try:
            self._bytes(artifact)
        except SpecialistError as error:
            return artifact.model_copy(
                update={
                    "status": "missing"
                    if error.code == "artifact_missing"
                    else "corrupt"
                }
            )
        return artifact

    def _bytes(self, artifact):
        if artifact.status != "ready" or datetime.now(UTC) >= artifact.expires_at:
            raise SpecialistError("artifact_expired")
        try:
            data = self.files.read(artifact.artifact_id, artifact.size_bytes)
            if (
                len(data) != artifact.size_bytes
                or hashlib.sha256(data).hexdigest() != artifact.sha256
            ):
                raise SpecialistError("artifact_integrity")
            if validate(data, artifact.format) != (
                artifact.mime_type,
                artifact.geometry,
            ):
                raise SpecialistError("artifact_integrity")
            return data
        except (OSError, ProjectError):
            raise SpecialistError("artifact_integrity") from None

    def retrieve(self, **fields):
        query = RetrieveArtifact.model_validate(fields)
        self.authorize(query.project_id)
        artifact = self.repository.artifact(query.project_id, query.artifact_id)
        data = self._bytes(artifact)
        if query.offset > len(data):
            raise SpecialistError("specialist_input_invalid")
        end = min(len(data), query.offset + query.max_bytes)
        return ArtifactChunk(
            artifact_id=artifact.artifact_id,
            offset=query.offset,
            data_base64=base64.b64encode(data[query.offset : end]).decode(),
            next_offset=end if end < len(data) else None,
            size_bytes=len(data),
            sha256=artifact.sha256,
        )

    def artifacts(self, *, authorized_ids=None, client_id=None, **fields):
        query = ToolQuery.model_validate(fields)
        self.authorize(query.project_id)
        values = self.repository.artifacts(
            project_id=query.project_id,
            limit=query.limit + 1,
            offset=query.offset,
            authorized_ids=authorized_ids,
            client_id=client_id,
        )
        # Lists are descriptors; integrity is checked by explicit inspect/retrieve.
        return ArtifactPage(
            artifacts=tuple(values[: query.limit]),
            next_offset=query.offset + query.limit
            if len(values) > query.limit
            else None,
        )

    def cleanup(self, *, now=None):
        """Trusted operator maintenance, never a model tool. Preserve tombstones."""
        with self.engine.storage_access():
            return self._cleanup(now=now)

    def _cleanup(self, *, now=None):
        self._private()
        now = now or datetime.now(UTC)
        offset, removed = 0, 0
        while values := self.repository.artifacts(limit=100, offset=offset):
            for artifact in values:
                if artifact.status != "expired" and artifact.expires_at <= now:
                    self.files.remove(artifact.artifact_id)
                    self.repository.expire(artifact.artifact_id)
                    removed += 1
            offset += len(values)
        return removed

    async def execute_next(self):
        queued = self.repository.jobs(states=("queued",), limit=1)
        if not queued:
            return False
        job = self.repository.transition(queued[0].tool_job_id, state="running")
        staged = []
        try:
            tool = self.authorize(job.project_id, job.tool_id)
            deadline = monotonic() + tool.limits.runtime_seconds
            if tool.fingerprint() != job.tool_fingerprint:
                raise SpecialistError("specialist_configuration_changed")
            inputs = tuple(
                self._bytes(self.repository.artifact(job.project_id, aid))
                for aid in job.input_artifact_ids
            )
            async with asyncio.timeout(tool.limits.runtime_seconds):
                child = asyncio.create_task(
                    ADAPTERS[tool.adapter]().run(job.parameters, inputs)
                )
                self._active[job.tool_job_id] = child
                try:
                    outputs = await child
                finally:
                    self._active.pop(job.tool_job_id, None)
                if (
                    tuple(output.format for output in outputs) != tool.output_formats
                    or sum(len(o.data) for o in outputs) > tool.limits.max_output_bytes
                ):
                    raise SpecialistError("specialist_output_invalid")
                for output in outputs:
                    mime, geometry = validate(output.data, output.format)
                    if mime != output.mime_type or not output.name.endswith(
                        "." + output.format
                    ):
                        raise SpecialistError("artifact_format_invalid")
                    now = datetime.now(UTC)
                    artifact = Artifact(
                        artifact_id=uuid4(),
                        project_id=job.project_id,
                        tool_job_id=job.tool_job_id,
                        tool_id=job.tool_id,
                        input_artifact_ids=job.input_artifact_ids,
                        created_at=now,
                        expires_at=now
                        + timedelta(seconds=self.config.retention_seconds),
                        name=output.name,
                        format=output.format,
                        mime_type=mime,
                        geometry=geometry,
                        size_bytes=len(output.data),
                        sha256=hashlib.sha256(output.data).hexdigest(),
                    )
                    self.repository.reserve(artifact, self.config)
                    staged.append(artifact.artifact_id)
                    self.files.write(artifact.artifact_id, output.data)
                    self._bytes(artifact)
                    # Let durable cancellation win even after atomic file publication.
                    await asyncio.sleep(0)
                self.authorize(job.project_id, job.tool_id)
                if monotonic() >= deadline:
                    raise TimeoutError
                self.repository.transition(
                    job.tool_job_id, state="completed", output_ids=tuple(staged)
                )
        except asyncio.CancelledError:
            current = self.repository.get(job.tool_job_id)
            self.repository.transition(
                job.tool_job_id, state="interrupted", failure="executor_stopped"
            )
            if not current.cancellation_requested_at:
                raise
        except TimeoutError:
            self.repository.transition(
                job.tool_job_id, state="failed", failure="specialist_timeout"
            )
        except SpecialistError as error:
            self.repository.transition(
                job.tool_job_id, state="failed", failure=error.code
            )
        except (OSError, ProjectError, ValueError):
            self.repository.transition(
                job.tool_job_id, state="failed", failure="specialist_validation_failed"
            )
        except Exception:
            self.repository.transition(
                job.tool_job_id, state="failed", failure="specialist_adapter_failed"
            )
        finally:
            if self.repository.get(job.tool_job_id).state != "completed":
                for aid in staged:
                    self.files.remove(aid)
                    self.repository.expire(aid)
            self.engine.work_changed()
        return True
