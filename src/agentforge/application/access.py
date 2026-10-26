"""Default-deny client projections over the shared Application.

Trusted local operator/stdio calls retain their existing full access. The gateway
supplies a credential revalidation callback, never caller-provided permissions.
"""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID

import anyio
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.application.contracts import ProjectInfo, ProjectsPage
from agentforge.application.errors import ServiceError
from agentforge.councils.models import CouncilNotFound
from agentforge.projects.errors import ProjectNotFound
from agentforge.specialists.models import SpecialistError
from agentforge.tasks.models import TaskNotFound

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]
Tool = Literal[
    "agentforge_status",
    "describe_capabilities",
    "list_projects",
    "list_workers",
    "suggest_workers",
    "list_agents",
    "delegate_task",
    "get_task",
    "watch_task",
    "cancel_task",
    "delegate_council",
    "get_council",
    "cancel_council",
    "get_coding_workspace",
    "get_coding_diff",
    "cleanup_coding_workspace",
    "list_specialist_tools",
    "submit_tool_job",
    "get_tool_job",
    "cancel_tool_job",
    "list_artifacts",
    "inspect_artifact",
    "retrieve_artifact",
]


class PolicyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class ProjectGrant(PolicyModel):
    project_id: UUID
    # Additional restriction; Project policy independently authorizes egress.
    # Empty grants allow no Worker.
    workers: tuple[Name, ...] = Field(default=(), max_length=256)
    specialists: tuple[Name, ...] = Field(default=(), max_length=128)


class ClientPolicy(PolicyModel):
    client_id: Name
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    enabled: bool = True
    expires_at: int | None = Field(default=None, ge=1)
    tools: tuple[Tool, ...] = Field(default=(), max_length=32)
    projects: tuple[ProjectGrant, ...] = Field(default=(), max_length=256)
    agents: tuple[Name, ...] = Field(default=(), max_length=256)
    read_shared: bool = False
    cancel_shared: bool = False
    coding_submit: bool = False

    @model_validator(mode="after")
    def unique_projects(self):
        if len({p.project_id for p in self.projects}) != len(self.projects):
            raise ValueError("Duplicate Project grants")
        return self


class Policies(PolicyModel):
    clients: tuple[ClientPolicy, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def unique_clients(self):
        if len({c.client_id for c in self.clients}) != len(self.clients):
            raise ValueError("Duplicate client identities")
        if len({c.token_sha256 for c in self.clients}) != len(self.clients):
            raise ValueError("Credentials cannot be shared between identities")
        return self


class ClientApplication:
    def __init__(self, application, principal: Callable[[], ClientPolicy], limits):
        self.app = application
        self.principal = principal
        self.limits = limits

    def allows(self, tool):
        return tool in self.principal().tools

    def require(self, tool):
        client = self.principal()
        if tool not in client.tools:
            raise ServiceError("access_denied")
        return client

    @staticmethod
    def binding(client, project_id, agent_id, worker_ids):
        grant = next((p for p in client.projects if p.project_id == project_id), None)
        if (
            grant is None
            or agent_id not in client.agents
            or any(w not in grant.workers for w in worker_ids)
        ):
            raise ServiceError("access_denied")

    def task(self, tool, task_id):
        client = self.require(tool)
        try:
            task = self.app.tasks.get_task(task_id)
        except TaskNotFound:
            raise ServiceError("access_denied") from None
        self.binding(client, task.project_id, task.agent_id, (task.worker_id,))
        if task.client_id != client.client_id and not client.read_shared:
            raise ServiceError("access_denied")
        if tool in {"cancel_task", "cleanup_coding_workspace"} and (
            task.client_id != client.client_id and not client.cancel_shared
        ):
            raise ServiceError("access_denied")
        return task

    def council(self, tool, council_id):
        client = self.require(tool)
        try:
            council = self.app.get_council(council_id=council_id)
        except CouncilNotFound:
            raise ServiceError("access_denied") from None
        self.binding(
            client,
            council.project_id,
            council.agent_id,
            tuple(p.worker_id for p in council.participants),
        )
        # Check ALL participants before returning any answer or cancelling any Task.
        for participant in council.participants:
            task = self.app.tasks.get_task(participant.task_id)
            if task.client_id != client.client_id and (
                not client.read_shared
                or tool == "cancel_council"
                and not client.cancel_shared
            ):
                raise ServiceError("access_denied")
        return council

    @staticmethod
    def page(values, cls, field, limit, offset):
        return cls(
            **{
                field: tuple(values[offset : offset + limit]),
                "next_offset": offset + limit if len(values) > offset + limit else None,
            }
        )

    def list_projects(self, *, limit=100, offset=0):
        client = self.require("list_projects")
        values = []
        for grant in client.projects:
            try:
                project = self.app.projects.get_project(grant.project_id)
            except ProjectNotFound:
                continue
            values.append(
                ProjectInfo(
                    project_id=project.id,
                    name=project.name,
                    root_path=str(project.root_path),
                    created_at=project.created_at,
                )
            )
        values.sort(key=lambda p: str(p.project_id))
        return self.page(values, ProjectsPage, "projects", limit, offset)

    def list_workers(self, *, limit=100, offset=0):
        client = self.require("list_workers")
        ids = {w for p in client.projects for w in p.workers}
        return self.app.list_workers(limit=limit, offset=offset, authorized_ids=ids)

    def suggest_workers(self, **fields):
        client = self.require("suggest_workers")
        self.binding(client, fields["project_id"], fields["agent_id"], ())
        grant = next(p for p in client.projects if p.project_id == fields["project_id"])
        result = self.app.suggest_workers(**fields, authorized_ids=grant.workers)
        # Connection-wide counters can include other Projects/clients. Reveal the
        # admission outcome and bounded cooldown, not their aggregate activity.
        return result.model_copy(
            update={
                "candidates": tuple(
                    candidate.model_copy(
                        update={"requests_used": None, "tokens_used": None}
                    )
                    for candidate in result.candidates
                )
            }
        )

    def list_agents(self, *, limit=100, offset=0):
        client = self.require("list_agents")
        return self.app.list_agents(
            limit=limit, offset=offset, authorized_ids=client.agents
        )

    def status(self):
        client = self.require("agentforge_status")
        current = self.app.status()
        counts = self.app.tasks.visible_active_counts(
            project_workers=tuple((p.project_id, p.workers) for p in client.projects),
            agents=client.agents,
            client_id=None if client.read_shared else client.client_id,
        )
        projects = 0
        for grant in client.projects:
            try:
                self.app.projects.get_project(grant.project_id)
                projects += 1
            except ProjectNotFound:
                pass
        worker_count, agent_count = self.app.configuration_counts(
            worker_ids={w for p in client.projects for w in p.workers},
            agent_ids=client.agents,
        )
        return current.model_copy(
            update={
                "client_id": client.client_id,
                "project_count": projects,
                "worker_count": worker_count,
                "agent_count": agent_count,
                "queued_tasks": counts.get("queued", 0),
                "running_tasks": counts.get("running", 0),
            }
        )

    def capabilities(self):
        client = self.require("describe_capabilities")
        current = self.app.capabilities()
        return current.model_copy(
            update={
                field: tuple(t for t in getattr(current, field) if t in client.tools)
                for field in (
                    "task_operations",
                    "advisory_operations",
                    "council_operations",
                    "coding_operations",
                    "specialist_operations",
                )
            }
        )

    def capacity(self, participants=1):
        if (
            sum(self.app.tasks.active_counts().values()) + participants
            > self.limits.max_pending_tasks
        ):
            raise ServiceError("resource_limit")

    def delegate_task(self, **fields):
        client = self.require("delegate_task")
        self.binding(
            client, fields["project_id"], fields["agent_id"], (fields["worker_id"],)
        )
        agent = self.app.agent_definition(fields["agent_id"])
        if (
            agent
            and agent.workspace_mode == "isolated_write"
            and not client.coding_submit
        ):
            raise ServiceError("access_denied")
        self.capacity()
        return self.redact(self.app.delegate_task(**fields, client_id=client.client_id))

    def delegate_council(self, **fields):
        client = self.require("delegate_council")
        self.binding(
            client, fields["project_id"], fields["agent_id"], fields["worker_ids"]
        )
        self.capacity(len(fields["worker_ids"]))
        return self.app.delegate_council(**fields, client_id=client.client_id)

    def redact(self, snapshot):
        if "get_coding_workspace" not in self.principal().tools:
            return snapshot.model_copy(update={"coding_result": None})
        return snapshot

    def get_task(self, *, task_id):
        self.task("get_task", task_id)
        return self.redact(self.app.get_task(task_id=task_id))

    def cancel_task(self, *, task_id):
        self.task("cancel_task", task_id)
        return self.redact(self.app.cancel_task(task_id=task_id))

    def task_progress(self, *, task_id):
        self.task("watch_task", task_id)
        return self.app.task_progress(task_id=task_id)

    @asynccontextmanager
    async def watch_task(self, *, task_id):
        self.task("watch_task", task_id)
        # Bound the actual asynchronous handlers as well as HTTP connections.
        # Closing a POST stream need not cancel its SDK handler immediately.
        key = self.principal().client_id
        counts = self.limits.watchers
        if sum(counts.values()) >= 32 or counts.get(key, 0) >= 4:
            raise ServiceError("resource_limit")
        counts[key] = counts.get(key, 0) + 1
        pending = None
        try:
            async with self.app.watch_task(task_id=task_id) as updates:

                async def checked():
                    nonlocal pending
                    deadline = monotonic() + self.limits.watch_seconds
                    while True:
                        self.task("watch_task", task_id)
                        if pending is None:
                            pending = asyncio.create_task(anext(updates))
                        done, _ = await asyncio.wait(
                            {pending}, timeout=min(1, max(0, deadline - monotonic()))
                        )
                        self.task("watch_task", task_id)  # Reload before sending data.
                        if pending in done:
                            try:
                                value = pending.result()
                            except StopAsyncIteration:
                                return
                            pending = None
                            yield value
                        if monotonic() >= deadline:
                            yield self.app.task_progress(task_id=task_id)
                            return

                stream = checked()
                try:
                    yield stream
                finally:
                    with anyio.CancelScope(shield=True):
                        if pending:
                            pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
                            pending = None
                        await stream.aclose()
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    if pending:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
            finally:
                counts[key] -= 1
                if not counts[key]:
                    del counts[key]

    def get_council(self, *, council_id):
        return self.council("get_council", council_id)

    def cancel_council(self, *, council_id):
        self.council("cancel_council", council_id)
        return self.app.cancel_council(council_id=council_id)

    def get_coding_workspace(self, *, task_id):
        self.task("get_coding_workspace", task_id)
        return self.app.get_coding_workspace(task_id=task_id)

    def get_coding_diff(self, *, task_id):
        self.task("get_coding_diff", task_id)
        return self.app.get_coding_diff(task_id=task_id)

    def cleanup_coding_workspace(self, *, task_id, workspace_id):
        self.task("cleanup_coding_workspace", task_id)
        return self.app.cleanup_coding_workspace(
            task_id=task_id, workspace_id=workspace_id
        )

    def specialist_grant(self, operation, project_id, tool_id=None):
        client = self.require(operation)
        grant = next((p for p in client.projects if p.project_id == project_id), None)
        if grant is None or (tool_id is not None and tool_id not in grant.specialists):
            raise ServiceError("access_denied")
        return client, grant

    def specialist_job(self, operation, tool_job_id):
        self.require(operation)
        try:
            job = self.app.specialist_service().repository.get(tool_job_id)
        except SpecialistError:
            raise ServiceError("access_denied") from None
        client, _ = self.specialist_grant(operation, job.project_id, job.tool_id)
        if job.client_id != client.client_id and (
            not client.read_shared
            or operation == "cancel_tool_job"
            and not client.cancel_shared
        ):
            raise ServiceError("access_denied")
        return job

    def specialist_artifact(self, operation, project_id, artifact_id):
        self.specialist_grant(operation, project_id)
        try:
            artifact = self.app.specialist_service().repository.artifact(
                project_id, artifact_id
            )
        except SpecialistError:
            raise ServiceError("access_denied") from None
        self.specialist_job(operation, artifact.tool_job_id)
        return artifact

    def list_specialist_tools(self, **fields):
        _, grant = self.specialist_grant("list_specialist_tools", fields["project_id"])
        return self.app.specialist_service().list_tools(
            **fields, authorized_ids=grant.specialists
        )

    def submit_tool_job(self, **fields):
        client, _ = self.specialist_grant(
            "submit_tool_job", fields["project_id"], fields["tool_id"]
        )
        for artifact_id in fields.get("input_artifact_ids", ()):
            self.specialist_artifact(
                "submit_tool_job", fields["project_id"], artifact_id
            )
        if fields.get("task_id") is not None:
            self.task("get_task", fields["task_id"])
        return self.app.submit_tool_job(**fields, client_id=client.client_id)

    def get_tool_job(self, *, tool_job_id):
        self.specialist_job("get_tool_job", tool_job_id)
        return self.app.get_tool_job(tool_job_id=tool_job_id)

    def cancel_tool_job(self, *, tool_job_id):
        self.specialist_job("cancel_tool_job", tool_job_id)
        return self.app.cancel_tool_job(tool_job_id=tool_job_id)

    def list_artifacts(self, **fields):
        client, grant = self.specialist_grant("list_artifacts", fields["project_id"])
        return self.app.specialist_service().artifacts(
            **fields,
            authorized_ids=grant.specialists,
            client_id=None if client.read_shared else client.client_id,
        )

    def inspect_artifact(self, **fields):
        self.specialist_artifact(
            "inspect_artifact", fields["project_id"], fields["artifact_id"]
        )
        return self.app.inspect_artifact(**fields)

    def retrieve_artifact(self, **fields):
        self.specialist_artifact(
            "retrieve_artifact", fields["project_id"], fields["artifact_id"]
        )
        return self.app.retrieve_artifact(**fields)
