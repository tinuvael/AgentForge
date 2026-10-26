"""Disposable, offline UI review server. Never points at operator storage.

Run: python scripts/preview_dashboard.py [--port 8877]
The fixed browser identity, scripted inference and seeded states are review
fixtures only. Production middleware/routes/templates are used unchanged.
"""

import argparse
import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import uvicorn
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import update

from agentforge.agents.models import ExecutionResult
from agentforge.application.access import ClientApplication, ClientPolicy, ProjectGrant
from agentforge.application.service import Application
from agentforge.core.inference import GenerationResult
from agentforge.core.worker import Worker, WorkerHealth
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.migrate import upgrade_database
from agentforge.db.models import TaskRecord, TaskTelemetryRecord
from agentforge.db.tasks import TaskRepository
from agentforge.director.config import DirectorConfig
from agentforge.director.core import Director
from agentforge.director.models import TaskBinding, VisibleMessage
from agentforge.gateway.server import GatewayConfig
from agentforge.mcp.server import TOOL_CONTRACTS, create_server
from agentforge.web.app import create_app
from agentforge.workers.config import WorkersConfig

TOKEN = "a" * 64
OWNER = hashlib.sha256(TOKEN.encode()).hexdigest()
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class OfflineProvider:
    name = "ollama"

    async def health(self, worker):
        return WorkerHealth(backend_available=True, model_available=True)

    async def generate(self, worker, request):
        return GenerationResult(
            content="Offline review fixture. No inference was performed.",
            model=worker.model,
        )


async def serve(port):
    with TemporaryDirectory(prefix="agentforge-ui-") as temporary:
        root = Path(temporary)
        url = f"sqlite:///{root / 'review.db'}"
        upgrade_database(url)
        app = Application(
            create_database_engine(url),
            WorkersConfig(
                workers=[
                    Worker(
                        id=identity,
                        provider="ollama",
                        endpoint="http://127.0.0.1:1",
                        model=model,
                        trust=trust,
                        context_window=context,
                        supports_tools=True,
                        supports_streaming=True,
                        deployment_label=label,
                    )
                    for identity, model, trust, context, label in (
                        (
                            "atlas-gpu",
                            "qwen3-coder:30b",
                            "local",
                            65536,
                            "GPU workstation",
                        ),
                        (
                            "studio-local",
                            "qwen3:8b",
                            "local",
                            32768,
                            "Local workstation",
                        ),
                        (
                            "remote-review",
                            "compatible/reasoning-model",
                            "cloud",
                            None,
                            "Remote inference",
                        ),
                    )
                ]
            ),
            providers={"ollama": OfflineProvider()},
        )
        projects = []
        for name in ("Forge Runtime", "Documentation", "Integration Sandbox"):
            path = root / name.lower().replace(" ", "-")
            path.mkdir()
            (path / "runtime.py").write_text("def health():\n    return True\n")
            projects.append(app.projects.register_project(name, path))
        await app.start()
        # Let the executor reach its empty queue before inserting frozen fixtures.
        await asyncio.sleep(0)
        await app.worker_diagnostics.check("atlas-gpu")
        sessions = create_session_factory(app.database)
        repository = TaskRepository(sessions)
        identities = []
        for index, state in enumerate(
            ("running", "queued", "failed", "completed", "completed", "cancelled")
        ):
            task = repository.add(
                project_id=projects[index % 3].id,
                agent_id="repo_explorer",
                worker_id="atlas-gpu" if index % 2 == 0 else "studio-local",
                provider="ollama",
                model="qwen3-coder:30b",
                request=(
                    "Review runtime.py for cancellation and ownership risks. Cite "
                    "source evidence."
                ),
            )
            identity = task.task_id
            identities.append(identity)
            if state == "cancelled":
                repository.cancel(identity)
            elif state != "queued":
                repository.claim(identity)
                if state == "failed":
                    repository.finish(identity, error_code="provider_error")
                elif state == "completed":
                    repository.finish(
                        identity,
                        result=ExecutionResult(
                            project_id=task.project_id,
                            agent_id=task.agent_id,
                            worker_id=task.worker_id,
                            state="completed",
                            reason="completed",
                            final_answer=(
                                "runtime.py:1 — health returns True. "
                                "No independent runtime "
                                "verification was performed."
                            ),
                            steps=2,
                            tool_call_count=1,
                            tool_output_bytes=64,
                            usage=(),
                            trace=(),
                        ),
                    )
            created = NOW - timedelta(minutes=index * 7)
            started = (
                created + timedelta(seconds=2)
                if state not in {"queued", "cancelled"}
                else None
            )
            finished = (
                started + timedelta(seconds=32)
                if started and state not in {"queued", "running"}
                else created + timedelta(seconds=1)
                if state == "cancelled"
                else None
            )
            with sessions() as session:
                session.execute(
                    update(TaskRecord)
                    .where(TaskRecord.task_id == identity)
                    .values(
                        created_at=created, started_at=started, finished_at=finished
                    )
                )
                session.execute(
                    update(TaskTelemetryRecord)
                    .where(TaskTelemetryRecord.task_id == identity)
                    .values(
                        created_at=created, started_at=started, finished_at=finished
                    )
                )
                session.commit()

        principal = ClientPolicy(
            client_id="offline-review",
            token_sha256="b" * 64,
            tools=tuple(t.name for t in TOOL_CONTRACTS),
            projects=tuple(
                ProjectGrant(
                    project_id=p.id,
                    workers=("atlas-gpu", "studio-local", "remote-review"),
                )
                for p in projects
            ),
            agents=("general_agent", "repo_explorer", "coder"),
            read_shared=True,
        )
        access = ClientApplication(app, lambda: principal, GatewayConfig())
        server = create_server(
            lambda: app, borrowed=True, client_access=lambda _: access
        )

        @asynccontextmanager
        async def connector(_):
            async with create_connected_server_and_client_session(server) as client:
                yield client

        config = DirectorConfig(
            gateway_url="http://127.0.0.1:1/mcp",
            token_env="UNUSED_OFFLINE_FIXTURE",
            journal=root / "journal",
            model="qwen3:8b",
            inference={
                "id": "director",
                "type": "ollama",
                "base_url": "http://127.0.0.1:1",
                "trust": "local",
            },
        )
        async with Director(
            config, provider=OfflineProvider(), connector=connector
        ) as director:
            active = director.create(OWNER)
            active.title = "Investigate cancellation safety"
            active.state = "running"
            # Exercise Director-selected execution without user form pins.
            active.request_id = uuid4()
            active.messages = [
                VisibleMessage(
                    role="user",
                    content=(
                        "Review the runtime for cancellation and ownership risks."
                        " Use the"
                        " local GPU Worker and cite evidence."
                    ),
                ),
                VisibleMessage(
                    role="assistant",
                    content=(
                        "I selected Forge Runtime and atlas-gpu for a read-only "
                        "repository investigation. The Task is running. I will "
                        "distinguish observed source evidence from findings that need "
                        "independent verification."
                    ),
                ),
            ]
            active.tasks = [
                TaskBinding(
                    task_id=identities[0],
                    project_id=projects[0].id,
                    agent_id="repo_explorer",
                    worker_id="atlas-gpu",
                    state="running",
                )
            ]
            active.progress = [
                "Authorized Project and Worker discovered.",
                "Task submitted on Worker atlas-gpu.",
                "Task running; waiting for returned evidence.",
            ]
            director.journal.save(active)
            director.active.add(active.id)
            approval = director.create(OWNER)
            approval.title = "Review isolated coding request"
            approval.project_id = projects[0].id
            approval.worker_id = "atlas-gpu"
            approval.messages = [
                VisibleMessage(
                    role="user",
                    content=(
                        "Fix the cancellation issue in an isolated coding workspace."
                    ),
                )
            ]
            director.approval(
                approval,
                "delegate_task",
                {
                    "project_id": str(projects[0].id),
                    "agent_id": "coder",
                    "worker_id": "atlas-gpu",
                    "task": (
                        "Fix cancellation handling in an isolated workspace; "
                        "run configured validation."
                    ),
                },
                projects[0].id,
            )
            completed = director.create(OWNER)
            completed.title = "Inspect runtime entry point"
            completed.state = "completed"
            completed.messages = [
                VisibleMessage(role="user", content="Inspect the runtime entry point."),
                VisibleMessage(
                    role="assistant",
                    content=(
                        "runtime.py:1 defines health(). Source tool use was "
                        "observed; no "
                        "independent runtime verification was performed."
                    ),
                ),
            ]
            director.journal.save(completed)

            @asynccontextmanager
            async def borrowed():
                yield director

            web = create_app(shared_application=app, director_factory=borrowed)
            original = web.router.lifespan_context

            @asynccontextmanager
            async def lifespan(instance):
                async with original(instance):

                    async def frozen_request():
                        try:
                            await asyncio.Event().wait()
                        finally:
                            director.active.discard(active.id)
                            web.state.director.jobs.pop(active.id, None)

                    web.state.director.jobs[active.id] = asyncio.create_task(
                        frozen_request()
                    )
                    yield

            web.router.lifespan_context = lifespan

            @web.middleware("http")
            async def fixture_identity(request, call_next):
                if request.url.path.startswith("/director"):
                    cookie = request.headers.get("cookie", "")
                    request.scope["headers"] = [
                        (k, v) for k, v in request.scope["headers"] if k != b"cookie"
                    ] + [
                        (
                            b"cookie",
                            (cookie + f"; agentforge_director={TOKEN}").encode(),
                        )
                    ]
                response = await call_next(request)
                if request.url.path.startswith("/director"):
                    response.set_cookie(
                        "agentforge_director",
                        TOKEN,
                        httponly=True,
                        samesite="strict",
                        path="/director",
                    )
                return response

            print(f"OFFLINE FIXTURES ONLY: http://127.0.0.1:{port}")
            print(
                f"Active: /director/{active.id}\nApproval: /director/{approval.id}",
                flush=True,
            )
            try:
                await uvicorn.Server(
                    uvicorn.Config(
                        web,
                        host="127.0.0.1",
                        port=port,
                        access_log=False,
                        log_level="warning",
                    )
                ).serve()
            finally:
                await app.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8877)
    asyncio.run(serve(parser.parse_args().port))
