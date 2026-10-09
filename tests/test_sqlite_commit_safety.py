"""Rejected Task writes must not survive on a pooled SQLite connection."""

import sqlite3
from uuid import uuid4

import pytest

from agentforge.agents.models import ExecutionResult
from agentforge.coding.models import CodingResult
from agentforge.tasks.models import TaskStorageError
from tests.sqlite_busy import blocked_commit, durable_tasks
from tests.test_tasks import run
from tests.test_tasks import setup as setup_fixture

setup = setup_fixture


def unrelated_write(setup):
    # Avoid binding validation's read-only transaction: it can accidentally
    # roll back the leaked work and mask the commit failure.
    return setup.repository.add(
        project_id=setup.project.id,
        agent_id=setup.agent.id,
        worker_id="home-i5",
        request="Unrelated accepted request",
    )


def test_failed_add_never_becomes_durable_or_reaches_provider(setup):
    engine, _, provider = setup.build()
    with blocked_commit(setup.database[0]):
        with pytest.raises(TaskStorageError, match="Could not access Task storage"):
            setup.submit(engine, task="Rejected request")
    assert setup.database[0].pool.checkedout() == 0
    accepted = unrelated_write(setup)
    assert [row[0] for row in durable_tasks(setup.database[0])] == [
        accepted.task_id.hex
    ]

    async def execute():
        async with engine:
            assert (await engine.wait_task(accepted.task_id)).state == "completed"

    run(execute())
    assert len(provider.requests) == 1
    assert "Rejected request" not in str(provider.requests)
    assert len(durable_tasks(setup.database[0])) == 1


@pytest.mark.parametrize(
    "operation", ["claim", "cancel_queued", "cancel_running", "finish", "recover"]
)
def test_failed_lifecycle_write_cannot_leak_into_unrelated_write(setup, operation):
    engine, _, _ = setup.build()
    task = setup.submit(engine)
    if operation in {"cancel_running", "finish", "recover"}:
        setup.repository.claim(task.task_id)
    before = durable_tasks(setup.database[0])
    operations = {
        "claim": lambda: setup.repository.claim(task.task_id),
        "cancel_queued": lambda: setup.repository.cancel(task.task_id),
        "cancel_running": lambda: setup.repository.cancel(task.task_id),
        "finish": lambda: setup.repository.finish(
            task.task_id, error_code="runtime_error"
        ),
        "recover": setup.repository.recover_running,
    }
    with blocked_commit(setup.database[0]):
        with pytest.raises(TaskStorageError):
            operations[operation]()
    assert setup.database[0].pool.checkedout() == 0
    other = unrelated_write(setup)
    rows = durable_tasks(setup.database[0])
    assert [row for row in rows if row[0] != other.task_id.hex] == before
    # Terminal telemetry belongs to the same rejected transaction.
    with sqlite3.connect(setup.database[0].url.database) as reader:
        assert reader.execute("SELECT count(*) FROM task_telemetry").fetchone() == (0,)


def test_failed_coding_attachment_does_not_replace_durable_result(setup):
    engine, _, _ = setup.build()
    task = setup.submit(engine)
    setup.repository.claim(task.task_id)
    result = ExecutionResult(
        project_id=task.project_id,
        agent_id=task.agent_id,
        worker_id=task.worker_id,
        state="completed",
        reason="completed",
        final_answer="Answer",
        steps=1,
        tool_call_count=0,
        tool_output_bytes=0,
        usage=(),
        trace=(),
    )
    setup.repository.finish(task.task_id, result=result)
    before = durable_tasks(setup.database[0])
    coding = CodingResult(
        task_id=task.task_id,
        project_id=task.project_id,
        worker_id=task.worker_id,
        workspace_id=uuid4(),
        branch_name="agentforge/test",
        base_commit="a" * 40,
        state="completed",
        created_at=task.created_at,
    )
    with blocked_commit(setup.database[0]):
        with pytest.raises(TaskStorageError):
            setup.repository.attach_coding_result(task.task_id, coding)
    assert setup.database[0].pool.checkedout() == 0
    other = unrelated_write(setup)
    assert [
        row for row in durable_tasks(setup.database[0]) if row[0] != other.task_id.hex
    ] == before


def test_failed_council_commit_preserves_atomicity(setup):
    engine, _, provider = setup.build()
    with blocked_commit(setup.database[0]):
        with pytest.raises(TaskStorageError):
            engine.submit_council(
                project_id=setup.project.id,
                agent_id=setup.agent.id,
                worker_ids=("home-i5", "local-4080"),
                task="Rejected Council",
            )
    assert setup.database[0].pool.checkedout() == 0
    accepted = unrelated_write(setup)
    assert [row[0] for row in durable_tasks(setup.database[0])] == [
        accepted.task_id.hex
    ]
    with sqlite3.connect(setup.database[0].url.database) as reader:
        for table in ("councils", "council_participants"):
            assert reader.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert not provider.requests
