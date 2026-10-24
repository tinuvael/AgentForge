"""Independent interpreter peer for ownership acceptance using JSON pipe barriers."""

import asyncio
import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select

from agentforge.agents import Agent
from agentforge.application.service import Application
from agentforge.coding.service import CodingWorkspaceManager
from agentforge.core.inference import GenerationResult
from agentforge.core.worker import Worker
from agentforge.db.coding import WorkspaceRepository
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.models import CodingWorkspaceRecord, TaskRecord
from agentforge.db.ownership import DatabaseOwnership, ExecutorOwnershipError
from agentforge.workers.config import WorkersConfig


def send(**value):
    print(json.dumps(value), flush=True)


async def command():
    line = await asyncio.to_thread(sys.stdin.readline)
    if not line:
        raise RuntimeError("Parent disconnected")
    return json.loads(line)


class Provider:
    name = "scripted"

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, worker, request):
        self.entered.set()
        await self.release.wait()
        return GenerationResult(content="Scripted final answer", model=worker.model)


async def main():
    url, mode = sys.argv[1:]
    provider = Provider()
    database = create_database_engine(url)
    sessions = create_session_factory(database)
    app = Application(
        database,
        WorkersConfig(
            workers=[
                Worker(
                    id=name,
                    provider="scripted",
                    model="offline",
                    endpoint="http://offline.invalid",
                )
                for name in ("one", "two")
            ]
        ),
        agents=[
            Agent(
                id="scripted",
                name="Scripted",
                description="Offline",
                system_prompt="Answer",
            )
        ],
        providers={"scripted": provider},
    )
    # Real workspace metadata recovery with no Git/worktree side effects. Existing
    # coding tests separately exercise real worktree creation and finalization.
    workspaces = WorkspaceRepository(sessions)
    app.tasks._workspaces = SimpleNamespace(
        recover=lambda tasks: CodingWorkspaceManager.recover(
            SimpleNamespace(repository=workspaces, _record=workspaces.get), tasks
        )
    )
    if mode in {"failure", "cancel"}:

        def fail():
            raise (
                asyncio.CancelledError()
                if mode == "cancel"
                else RuntimeError("synthetic")
            )

        app.tasks._repository.check_schema = fail
    send(event="ready")
    await command()  # All cold-start contenders are ready before the release.
    if mode == "lock":
        ownership = DatabaseOwnership(database)
        try:
            ownership.acquire()
            send(event="owned")
            await command()
        finally:
            ownership.release()
            database.dispose()
        send(event="closed")
        return
    try:
        await app.start()
    except ExecutorOwnershipError as error:
        send(event="rejected", message=str(error))
        await app.close()
        return
    except (RuntimeError, asyncio.CancelledError):
        send(event="startup_failed")
        await app.close()
        return
    try:
        send(event="owned")
        if mode == "active":
            arguments = await command()
            council = app.delegate_council(
                project_id=arguments["project_id"],
                agent_id="scripted",
                worker_ids=("one", "two"),
                task="Scripted in-flight request",
            )
            await provider.entered.wait()
            running = council.participants[0].task_id
            # A ready workspace row proves that a losing startup never reaches
            # workspace recovery. No physical coding host is required here.
            workspaces.add(
                task_id=running,
                workspace_id=running,
                project_id=council.project_id,
                worker_id="one",
                branch_name=f"synthetic-{running}",
                base_commit="0" * 40,
                created_at=datetime.now(UTC),
                state="ready",
                repository_path="private",
                worktree_path="private",
                prefix="",
                identities={},
                observations={},
            )
            send(
                event="running",
                task_id=str(running),
                council_id=str(council.council_id),
            )
            await command()
            provider.release.set()
            for participant in council.participants:
                await app.tasks.wait_task(participant.task_id)
            send(
                event="completed",
                states=[
                    app.tasks.get_task(p.task_id).state for p in council.participants
                ],
                answer=app.tasks.get_task(running).final_answer,
            )
        elif mode == "recover":
            # start() performs recovery synchronously before executor loops run.
            with sessions() as session:
                send(
                    event="recovered",
                    tasks=[
                        [str(row.task_id), row.state, row.reason]
                        for row in session.scalars(
                            select(TaskRecord).order_by(TaskRecord.created_at)
                        )
                    ],
                    coding=[
                        row.state
                        for row in session.scalars(select(CodingWorkspaceRecord))
                    ],
                )
        else:
            await command()
    finally:
        await app.close()
        await app.close()
        send(event="closed")


if __name__ == "__main__":
    asyncio.run(main())
