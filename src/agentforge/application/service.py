"""One composition root and shared application operations for external adapters."""

import asyncio
from collections.abc import Mapping, Sequence
from importlib.metadata import version
from uuid import UUID

from sqlalchemy import Engine

from agentforge.agents import REPO_EXPLORER, Agent, AgentRuntime, repository_toolset
from agentforge.application.contracts import (
    AgentInfo,
    AgentsPage,
    Capabilities,
    ExecutionSummary,
    ProjectInfo,
    ProjectsPage,
    Status,
    TaskSnapshot,
    WorkerInfo,
    WorkersPage,
)
from agentforge.application.councils import CouncilService
from agentforge.application.dashboard import DashboardQueries
from agentforge.application.errors import ServiceError
from agentforge.core.inference import Provider
from agentforge.db.councils import CouncilRepository
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.index import IndexRepository
from agentforge.db.projects import ProjectRepository
from agentforge.db.tasks import TaskRepository
from agentforge.db.telemetry import TelemetryRepository
from agentforge.index.service import ProjectIndex
from agentforge.projects.service import ProjectRegistry
from agentforge.providers.factory import create_providers
from agentforge.tasks.engine import TaskEngine
from agentforge.tasks.models import Task
from agentforge.telemetry.service import TelemetryService
from agentforge.tools.service import RepositoryTools
from agentforge.workers.config import WorkersConfig, load_workers


def snapshot(task: Task) -> TaskSnapshot:
    result = task.execution_result
    return TaskSnapshot(
        **{
            name: getattr(task, name)
            for name in TaskSnapshot.model_fields
            if name != "execution_summary"
        },
        execution_summary=ExecutionSummary(
            steps=result.steps,
            tool_call_count=result.tool_call_count,
            tool_output_bytes=result.tool_output_bytes,
        )
        if result
        else None,
    )


class Application:
    """Construct once, start once, close once. No migrations or health probes.

    The Database Engine passed here is owned by this application. TaskEngine's
    existing per-database process guard also rejects independently composed owners.
    """

    def __init__(
        self,
        database: Engine,
        workers: WorkersConfig,
        *,
        agents: Sequence[Agent] = (REPO_EXPLORER,),
        providers: Mapping[str, Provider] | None = None,
        concurrency: int = 1,
    ):
        self.database = database
        sessions = create_session_factory(database)
        self.projects = ProjectRegistry(ProjectRepository(sessions))
        self.index = ProjectIndex(self.projects, IndexRepository(sessions))
        self.repository_tools = RepositoryTools(self.projects)
        self.telemetry = TelemetryService(TelemetryRepository(sessions))
        # Discovery and execution consume the same definitions, not MCP copies.
        self._workers = {w.id: w for w in workers.workers}
        self._agents = {a.id: a for a in agents}
        self._worker_info = tuple(
            WorkerInfo(
                worker_id=w.id,
                provider=w.provider,
                model=w.model,
                context_window=w.context_window,
                supports_tools=w.supports_tools,
                supports_streaming=w.supports_streaming,
                deployment_label=w.deployment_label,
            )
            for w in sorted(workers.workers, key=lambda w: w.id)
        )
        self._agent_info = tuple(
            AgentInfo(
                agent_id=a.id,
                name=a.name,
                description=a.description[:2000],
                allowed_tools=a.allowed_tools,
                limits=a.limits,
            )
            for a in sorted(agents, key=lambda a: a.id)
        )
        # Only composed resources are owned here; injected Providers retain caller
        # ownership. Factory constructors are lazy, without open HTTP resources.
        self._owned_providers = create_providers(workers) if providers is None else {}
        runtime = AgentRuntime(
            projects=self.projects,
            workers=workers,
            agents=agents,
            providers=providers if providers is not None else self._owned_providers,
            tools=repository_toolset(self.index, self.repository_tools),
        )
        task_repository = TaskRepository(sessions)
        self.tasks = TaskEngine(task_repository, runtime, concurrency=concurrency)
        self.councils = CouncilService(
            CouncilRepository(sessions),
            self.tasks,
            self.projects,
            self._agents,
            self._workers,
            self.telemetry,
        )
        self.dashboard = DashboardQueries(self.projects, self.tasks, self.telemetry)
        self._started = False
        self._closed = False
        self._shutdown_task: asyncio.Task | None = None

    @classmethod
    def from_config(cls, *, database_url: str, workers_path: str, concurrency: int = 1):
        workers = load_workers(workers_path)
        database = create_database_engine(database_url)
        try:
            return cls(database, workers, concurrency=concurrency)
        except BaseException:
            database.dispose()
            raise

    async def start(self):
        if self._closed:
            raise ServiceError("service_unavailable")
        if not self._started:
            try:
                await self.tasks.start()  # Includes interrupted-task recovery, once.
            except BaseException:
                await self.close()
                raise
            self._started = True

    async def _shutdown(self):
        try:
            await self.tasks.close()
        finally:
            try:
                results = await asyncio.gather(
                    *(
                        provider.aclose()
                        for provider in self._owned_providers.values()
                        if hasattr(provider, "aclose")
                    ),
                    return_exceptions=True,
                )
                if any(isinstance(result, BaseException) for result in results):
                    raise ServiceError("service_unavailable")
            finally:
                self.database.dispose()

    async def close(self):
        self._closed = True
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown())
        # Shield both executor cleanup and database disposal from client disconnect.
        await asyncio.shield(self._shutdown_task)

    def status(self) -> Status:
        counts = self.tasks.active_counts()
        available = self.tasks.available
        return Status(
            available=available,
            task_engine_available=available,
            version=version("agentforge"),
            project_count=self.projects.count_projects(),
            worker_count=len(self._workers),
            agent_count=len(self._agents),
            queued_tasks=counts.get("queued", 0),
            running_tasks=counts.get("running", 0),
        )

    def capabilities(self) -> Capabilities:
        return Capabilities(
            responsibility=(
                "Durable execution/control plane; the external "
                "director evaluates results."
            ),
            worker_selection="explicit_project_agent_worker_required",
            task_operations=("delegate_task", "get_task", "cancel_task"),
            council_operations=("delegate_council", "get_council", "cancel_council"),
            repository_access="central_host_agent_allowlisted_read_only",
            telemetry="terminal_task_status_only; metrics_via_python_service",
            limitations=(
                "No routing, ranking, fallback, judging or answer sharing.",
                "Worker capabilities are configuration, not observed availability.",
                "Delegation submits asynchronously; the director polls or cancels.",
                "Repository tools require POSIX descriptors or native Windows "
                "local NTFS handles; unsupported filesystems fail closed.",
                "Ollama and the text/tool Chat Completions protocol are shipped.",
                "Local trusted stdio only; one process per database, no "
                "distributed lease.",
                "No trace, reasoning, raw responses or telemetry analytics over MCP.",
            ),
        )

    def list_projects(self, *, limit: int = 100, offset: int = 0) -> ProjectsPage:
        # Registry only. No disk discovery, Git subprocesses, index scan or file reads.
        values = self.projects.list_projects(limit=limit + 1, offset=offset)
        return ProjectsPage(
            projects=tuple(
                ProjectInfo(
                    project_id=p.id,
                    name=p.name,
                    root_path=str(p.root_path),
                    created_at=p.created_at,
                )
                for p in values[:limit]
            ),
            next_offset=offset + limit if len(values) > limit else None,
        )

    def list_workers(self, *, limit: int = 100, offset: int = 0) -> WorkersPage:
        return WorkersPage(
            workers=self._worker_info[offset : offset + limit],
            next_offset=offset + limit
            if len(self._worker_info) > offset + limit
            else None,
        )

    def list_agents(self, *, limit: int = 100, offset: int = 0) -> AgentsPage:
        return AgentsPage(
            agents=self._agent_info[offset : offset + limit],
            next_offset=offset + limit
            if len(self._agent_info) > offset + limit
            else None,
        )

    def delegate_task(
        self, *, project_id: UUID, agent_id: str, worker_id: str, task: str
    ) -> TaskSnapshot:
        if not self.tasks.available:
            raise ServiceError("service_unavailable")
        # Preserve missing/storage error categories.
        self.projects.get_project(project_id)
        if agent_id not in self._agents:
            raise ServiceError("agent_not_found")
        if worker_id not in self._workers:
            raise ServiceError("worker_not_found")
        return snapshot(
            self.tasks.submit(
                project_id=project_id,
                agent_id=agent_id,
                worker_id=worker_id,
                task=task,
            )
        )

    def get_task(self, *, task_id: UUID) -> TaskSnapshot:
        return snapshot(self.tasks.get_task(task_id))

    def cancel_task(self, *, task_id: UUID) -> TaskSnapshot:
        return snapshot(self.tasks.cancel_task(task_id))

    def delegate_council(self, **arguments):
        return self.councils.submit(**arguments)

    def get_council(self, *, council_id: UUID):
        return self.councils.get(council_id)

    def cancel_council(self, *, council_id: UUID):
        return self.councils.cancel(council_id)
