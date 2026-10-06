"""Durable Task lifecycle tests: migrated SQLite, scripted inference, no network."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import inspect, select, update

from agentforge.agents import Agent, AgentRuntime, ExecutionResult, repository_toolset
from agentforge.core.inference import GenerationResult, TokenUsage, ToolCall
from agentforge.core.provider_errors import BackendUnavailable, ProviderTimeout
from agentforge.core.worker import Worker
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.index import IndexRepository
from agentforge.db.models import TaskRecord
from agentforge.db.tasks import TaskRepository
from agentforge.index.service import ProjectIndex
from agentforge.tasks.engine import TaskEngine
from agentforge.tasks.models import (
    InvalidTaskTransition,
    TaskNotFound,
    TaskStorageError,
    TaskValidationError,
    validate_transition,
)
from agentforge.tools.service import RepositoryTools
from agentforge.workers.config import WorkersConfig


def run(coroutine):
    async def bounded():
        async with asyncio.timeout(5):
            return await coroutine

    return asyncio.run(bounded())


class Provider:
    name = "fake"

    def __init__(self, turns=()):
        self.turns = list(turns)
        self.workers = []
        self.requests = []

    async def health(self, _):
        pytest.fail("Task Engine must not probe/select Workers")

    async def generate(self, worker, request):
        self.workers.append(worker)
        self.requests.append(request)
        turn = (
            self.turns.pop(0)
            if self.turns
            else GenerationResult(
                content="Evidence found.",
                model=worker.model,
                token_usage=TokenUsage(input_tokens=7, output_tokens=3),
            )
        )
        if isinstance(turn, Exception):
            raise turn
        return turn


class BlockingProvider(Provider):
    def __init__(self, expected=1):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.expected = expected
        self.active = self.maximum = self.cleaned = 0

    async def generate(self, worker, request):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        if self.active == self.expected:
            self.entered.set()
        try:
            await self.release.wait()
            return await super().generate(worker, request)
        finally:
            self.active -= 1
            self.cleaned += 1


@pytest.fixture
def setup(registry, database, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "source.py").write_text("def SOURCE_BODY_PRIVATE():\n    pass\n")
    project = registry.register_project("Scoped", root)
    sessions = create_session_factory(database[0])
    repository = TaskRepository(sessions)
    agent = Agent(
        id="general", name="General", description="Text", system_prompt="Read."
    )
    other_agent = agent.model_copy(update={"id": "other-agent"})
    workers = WorkersConfig(
        workers=[
            Worker(
                id=identity,
                provider="fake",
                model="model",
                endpoint=endpoint,
                supports_tools=True,
            )
            for identity, endpoint in (
                ("local-4080", "http://localhost:11434"),
                ("home-i5", "http://192.168.50.12:11434"),
                ("ai395", "http://100.100.100.10:11434"),
                ("future-cloud-worker", "https://inference.example.invalid"),
            )
        ]
    )

    def build(provider=None, concurrency=1, **overrides):
        provider = provider or Provider()
        arguments = dict(
            projects=registry,
            workers=workers,
            agents=[agent, other_agent],
            providers={"fake": provider},
            tools={},
        )
        arguments.update(overrides)
        runtime = AgentRuntime(**arguments)
        return (
            TaskEngine(repository, runtime, concurrency=concurrency),
            runtime,
            provider,
        )

    def submit(engine, **overrides):
        arguments = dict(
            project_id=project.id,
            agent_id=agent.id,
            worker_id="home-i5",
            task="  Inspect evidence.\n",
        )
        arguments.update(overrides)
        return engine.submit(**arguments)

    return SimpleNamespace(
        root=root,
        project=project,
        registry=registry,
        database=database,
        sessions=sessions,
        repository=repository,
        agent=agent,
        workers=workers,
        build=build,
        submit=submit,
    )


def test_submit_persists_immutable_binding_and_reopens(setup):
    engine, runtime, provider = setup.build()
    task = setup.submit(engine)
    assert task.state == "queued" and task.request == "  Inspect evidence.\n"
    assert task.project_id == setup.project.id
    assert task.agent_id == "general" and task.worker_id == "home-i5"
    assert task.created_at.tzinfo == UTC and task.updated_at == task.created_at
    assert task.started_at is task.finished_at is task.execution_result is None
    assert not provider.requests
    with pytest.raises(ValidationError):
        task.worker_id = "local-4080"
    setup.database[0].dispose()
    reopened = create_database_engine(setup.database[1])
    try:
        service = TaskEngine(TaskRepository(create_session_factory(reopened)), runtime)
        assert service.get_task(str(task.task_id)) == task
    finally:
        reopened.dispose()


def test_list_filter_order_ties_and_bounds(setup, tmp_path):
    engine, _, _ = setup.build()
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = setup.registry.register_project("Other", other_root)
    first = setup.submit(engine)
    second = setup.submit(engine, worker_id="ai395", agent_id="other-agent")
    third = setup.submit(engine, project_id=other.id)
    engine.cancel_task(third.task_id)
    assert [t.task_id for t in engine.list_tasks()] == [
        third.task_id,
        second.task_id,
        first.task_id,
    ]
    assert len(engine.list_tasks(state="queued")) == 2
    assert len(engine.list_tasks(project_id=str(setup.project.id))) == 2
    assert engine.list_tasks(agent_id="other-agent") == [second]
    assert engine.list_tasks(worker_id="ai395") == [second]
    assert engine.list_tasks(state="queued", worker_id="home-i5") == [first]
    assert engine.list_tasks(limit=1, offset=1) == [second]
    tie = datetime.now(UTC)
    with setup.sessions.begin() as session:
        session.execute(update(TaskRecord).values(created_at=tie))
    assert [t.task_id for t in engine.list_tasks()] == sorted(
        [first.task_id, second.task_id, third.task_id], reverse=True
    )
    for invalid in (
        {"limit": 0},
        {"limit": 1001},
        {"limit": True},
        {"offset": -1},
        {"state": "invented"},
    ):
        with pytest.raises(TaskValidationError):
            engine.list_tasks(**invalid)


@pytest.mark.parametrize(
    "source", ["queued", "running", "completed", "failed", "cancelled"]
)
@pytest.mark.parametrize(
    "target", ["queued", "running", "completed", "failed", "cancelled"]
)
def test_centralized_state_machine(source, target):
    legal = {
        ("queued", "running"),
        ("queued", "cancelled"),
        ("running", "completed"),
        ("running", "failed"),
        ("running", "cancelled"),
    }
    if (source, target) in legal:
        validate_transition(source, target)
    else:
        with pytest.raises(InvalidTaskTransition):
            validate_transition(source, target)


@pytest.mark.parametrize(
    "overrides",
    [
        {"project_id": uuid4()},
        {"project_id": "invalid"},
        {"agent_id": "missing"},
        {"worker_id": "missing"},
        {"task": " "},
        {"task": None},
    ],
)
def test_invalid_submit_creates_no_work(setup, overrides):
    engine, _, provider = setup.build()
    with pytest.raises(TaskValidationError):
        setup.submit(engine, **overrides)
    assert engine.list_tasks() == [] and not provider.requests


@pytest.mark.parametrize(
    "configuration", ["provider", "tool", "duplicate", "capability"]
)
def test_submit_checks_existing_runtime_configuration(setup, configuration):
    options = {}
    if configuration == "provider":
        options["providers"] = {}
    else:
        options["agents"] = [
            setup.agent.model_copy(update={"allowed_tools": ("missing",)})
        ]
        if configuration == "duplicate":
            options["tools"] = repository_toolset(
                ProjectIndex(setup.registry, IndexRepository(setup.sessions)),
                RepositoryTools(setup.registry),
            )
            options["agents"][0] = options["agents"][0].model_copy(
                update={"allowed_tools": ("read_file", "read_file")}
            )
        if configuration == "capability":
            options["tools"] = repository_toolset(
                ProjectIndex(setup.registry, IndexRepository(setup.sessions)),
                RepositoryTools(setup.registry),
            )
            options["agents"][0] = options["agents"][0].model_copy(
                update={"allowed_tools": ("read_file",)}
            )
            options["workers"] = WorkersConfig(
                workers=[
                    setup.workers.workers[1].model_copy(
                        update={"supports_tools": False}
                    )
                ]
            )
    engine, _, _ = setup.build(**options)
    with pytest.raises(TaskValidationError):
        setup.submit(engine)
    assert engine.list_tasks() == []


@pytest.mark.parametrize(
    "worker_id", ["local-4080", "home-i5", "ai395", "future-cloud-worker"]
)
def test_execution_keeps_exact_binding_and_result(setup, worker_id):
    engine, runtime, provider = setup.build()
    submitted = setup.submit(engine, worker_id=worker_id)
    calls = []
    original = runtime.run

    async def observe(**binding):
        calls.append(binding)
        # Neither a Session nor transaction is checked out during inference.
        assert setup.database[0].pool.checkedout() == 0
        assert engine.get_task(submitted.task_id).state == "running"
        return await original(**binding)

    runtime.run = observe

    async def execute():
        async with engine:
            return await engine.wait_task(submitted.task_id)

    task = run(execute())
    assert len(calls) == 1
    assert calls[0]["project_id"] == submitted.project_id
    assert calls[0]["agent_id"] == submitted.agent_id
    assert calls[0]["worker_id"] == submitted.worker_id
    assert calls[0]["task"] == submitted.request
    assert [w.id for w in provider.workers] == [worker_id]
    assert task.state == task.reason == "completed"
    assert task.final_answer == "Evidence found." and task.error_code is None
    assert task.created_at <= task.started_at <= task.finished_at
    result = task.execution_result
    assert result.steps == 1 and result.tool_call_count == result.tool_output_bytes == 0
    assert result.usage == (TokenUsage(input_tokens=7, output_tokens=3),)
    assert [e.kind for e in result.trace] == [
        "model_request",
        "model_response",
        "termination",
    ]
    assert engine.get_task(task.task_id) == task
    assert setup.repository.claim(task.task_id) is None


@pytest.mark.parametrize(
    "error,reason",
    [
        (BackendUnavailable("RAW BACKEND SECRET"), "provider_error"),
        (ProviderTimeout("RAW BACKEND SECRET"), "provider_timeout"),
    ],
)
def test_remote_failure_has_safe_diagnostics_no_fallback(setup, error, reason):
    engine, _, provider = setup.build(Provider([error]))
    task = setup.submit(engine, worker_id="future-cloud-worker")

    async def execute():
        async with engine:
            return await engine.wait_task(task.task_id)

    task = run(execute())
    assert task.state == "failed" and task.reason == task.error_code == reason
    assert task.failure_diagnostic == f"Execution failed: {reason}"
    assert task.worker_id == "future-cloud-worker" and task.final_answer is None
    assert [w.id for w in provider.workers] == ["future-cloud-worker"]
    assert "SECRET" not in task.model_dump_json()
    assert engine.cancel_task(task.task_id) == task
    assert setup.repository.claim(task.task_id) is None


def test_queued_cancel_never_executes_and_is_idempotent(setup):
    engine, _, provider = setup.build()
    task = setup.submit(engine)
    cancelled = engine.cancel_task(task.task_id)
    assert cancelled.state == cancelled.reason == "cancelled"
    assert cancelled.started_at is None and cancelled.finished_at is not None
    assert engine.cancel_task(task.task_id) == cancelled
    assert setup.repository.claim(task.task_id) is None

    async def execute():
        async with engine:
            # A second task ensures the executor actually runs through the queue.
            second = setup.submit(engine)
            await engine.wait_task(second.task_id)
            return engine.get_task(task.task_id)

    assert run(execute()) == cancelled
    assert len(provider.requests) == 1


def test_running_cancel_reaches_runtime_token_and_waits_for_boundary(setup):
    provider = BlockingProvider()
    engine, runtime, _ = setup.build(provider)
    submitted = setup.submit(engine)
    tokens = []
    original = runtime.run

    async def observe(**binding):
        tokens.append(binding["cancellation"])
        return await original(**binding)

    runtime.run = observe

    async def execute():
        async with engine:
            await provider.entered.wait()
            pending = engine.cancel_task(submitted.task_id)
            assert pending.state == "running" and pending.finished_at is None
            assert tokens[0].cancelled and pending.cancellation_requested_at is not None
            assert engine.cancel_task(submitted.task_id) == pending
            assert provider.active == 1  # Cooperative signal does not kill inference.
            provider.release.set()
            final = await engine.wait_task(submitted.task_id)
            assert engine.cancel_task(submitted.task_id) == final
            return final

    final = run(execute())
    assert final.state == final.reason == "cancelled"
    assert final.execution_result.state == "cancelled" and final.final_answer is None
    assert provider.cleaned == 1


def test_cancellation_between_start_and_inference_prevents_provider_call(setup):
    engine, runtime, provider = setup.build()
    task = setup.submit(engine)
    original = runtime.run

    async def before_start(**binding):
        engine.cancel_task(task.task_id)
        return await original(**binding)

    runtime.run = before_start

    async def execute():
        async with engine:
            return await engine.wait_task(task.task_id)

    assert run(execute()).state == "cancelled"
    assert not provider.requests


def test_cancel_wins_late_completed_result_and_completion_wins_late_cancel(setup):
    engine, runtime, _ = setup.build()
    first = setup.submit(engine)
    original = runtime.run

    async def late_signal(**binding):
        result = await original(**binding)
        if binding["task"] == first.request:
            engine.cancel_task(first.task_id)
        return result

    runtime.run = late_signal

    async def execute():
        async with engine:
            cancelled = await engine.wait_task(first.task_id)
            assert cancelled.state == cancelled.execution_result.state == "cancelled"
            assert cancelled.execution_result.final_answer is None
            assert cancelled.execution_result.trace[-1].reason == "cancelled"
            second = setup.submit(engine, task="Second")
            completed = await engine.wait_task(second.task_id)
            assert engine.cancel_task(second.task_id) == completed
            # Duplicate/late finish cannot replace a terminal result.
            assert (
                setup.repository.finish(second.task_id, error_code="runtime_error")
                == completed
            )
            return cancelled, completed

    cancelled, completed = run(execute())
    assert cancelled.reason == "cancelled" and completed.state == "completed"


def test_two_claimers_only_one_wins(setup):
    engine, _, _ = setup.build()
    task = setup.submit(engine)
    barrier = Barrier(2)

    def claim():
        repository = TaskRepository(setup.sessions)
        barrier.wait(timeout=3)
        return repository.claim(task.task_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert sum(result is not None for result in results) == 1
    assert engine.get_task(task.task_id).state == "running"


def test_bounded_executor_and_repeated_start(setup):
    provider = BlockingProvider(expected=2)
    engine, _, _ = setup.build(provider, concurrency=2)
    tasks = [
        setup.submit(engine, worker_id=setup.workers.workers[i % 4].id)
        for i in range(6)
    ]

    async def execute():
        async with engine:
            await engine.start()
            await provider.entered.wait()
            assert len(engine._loops) == 2 and len(engine._tokens) == 2
            assert len(engine.list_tasks(state="queued")) == 4
            assert len(engine.list_tasks(state="running")) == 2
            provider.release.set()
            return [await engine.wait_task(t.task_id) for t in tasks]

    completed = run(execute())
    assert all(t.state == "completed" for t in completed)
    assert len(provider.requests) == 6 and provider.maximum == 2
    # Completion order can differ from FIFO claim order during concurrent inference.
    assert sorted(w.id for w in provider.workers) == sorted(t.worker_id for t in tasks)
    assert engine._tokens == {} and all(t.done() for t in engine._loops)


def test_exclusive_in_process_owner_before_recovery(setup):
    provider = BlockingProvider()
    first, runtime, _ = setup.build(provider)
    second = TaskEngine(TaskRepository(setup.sessions), runtime)
    task = setup.submit(first)

    async def execute():
        async with first:
            await provider.entered.wait()
            with pytest.raises(RuntimeError, match="already owns"):
                await second.start()
            assert first.get_task(task.task_id).state == "running"
            provider.release.set()
            await first.wait_task(task.task_id)
        async with second:
            assert second.get_task(task.task_id).state == "completed"

    run(execute())


@pytest.mark.parametrize("explicit_cancel", [False, True])
def test_shutdown_cleans_provider_and_does_not_leave_running(setup, explicit_cancel):
    provider = BlockingProvider()
    engine, _, _ = setup.build(provider)
    first = setup.submit(engine)
    second = setup.submit(engine)

    async def execute():
        await engine.start()
        await provider.entered.wait()
        if explicit_cancel:
            engine.cancel_task(first.task_id)
        await engine.close()
        await engine.close()
        assert engine.get_task(second.task_id).state == "queued"
        return engine.get_task(first.task_id)

    task = run(execute())
    assert task.state == ("cancelled" if explicit_cancel else "failed")
    assert task.reason == ("cancelled" if explicit_cancel else "executor_cancelled")
    assert task.finished_at is not None and provider.cleaned == 1
    assert engine._tokens == {}


def test_waiter_cancellation_leaves_execution_owned_by_engine(setup):
    provider = BlockingProvider()
    engine, _, _ = setup.build(provider)
    task = setup.submit(engine)

    async def execute():
        async with engine:
            await provider.entered.wait()
            waiter = asyncio.create_task(engine.wait_task(task.task_id))
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert engine.get_task(task.task_id).state == "running"
            provider.release.set()
            return await engine.wait_task(task.task_id)

    assert run(execute()).state == "completed"


def test_cancelled_executor_task_persists_failure_and_cleans_resources(setup):
    provider = BlockingProvider()
    engine, _, _ = setup.build(provider)
    task = setup.submit(engine)

    async def execute():
        async with engine:
            await provider.entered.wait()
            engine._loops[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await engine._loops[0]
            return engine.get_task(task.task_id)

    task = run(execute())
    assert task.state == "failed" and task.reason == "executor_cancelled"
    assert provider.cleaned == 1


def test_cancelled_close_caller_cannot_interrupt_cleanup_or_release_owner(setup):
    class SlowCleanupProvider(BlockingProvider):
        def __init__(self):
            super().__init__()
            self.cleanup_entered = asyncio.Event()
            self.cleanup_release = asyncio.Event()

        async def generate(self, worker, request):
            try:
                return await super().generate(worker, request)
            finally:
                self.cleanup_entered.set()
                await self.cleanup_release.wait()

    provider = SlowCleanupProvider()
    engine, runtime, _ = setup.build(provider)
    replacement = TaskEngine(TaskRepository(setup.sessions), runtime)
    task = setup.submit(engine)

    async def execute():
        await engine.start()
        await provider.entered.wait()
        assert setup.database[0].pool.checkedout() == 0
        closing = asyncio.create_task(engine.close())
        await provider.cleanup_entered.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        with pytest.raises(RuntimeError, match="already owns"):
            await replacement.start()
        provider.cleanup_release.set()
        await engine.close()
        async with replacement:
            return replacement.get_task(task.task_id)

    task = run(execute())
    assert task.state == "failed" and task.reason == "executor_cancelled"
    assert provider.cleaned == 1 and engine._tokens == {}


def test_database_cancel_and_finish_race_is_atomic(setup):
    engine, _, _ = setup.build()
    task = setup.submit(engine)
    setup.repository.claim(task.task_id)
    barrier = Barrier(2)
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

    def cancel():
        barrier.wait(timeout=3)
        return TaskRepository(setup.sessions).cancel(task.task_id)

    def finish():
        barrier.wait(timeout=3)
        return TaskRepository(setup.sessions).finish(task.task_id, result=result)

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel_future = executor.submit(cancel)
        finish_future = executor.submit(finish)
        cancel_snapshot = cancel_future.result(timeout=5)
        finish_snapshot = finish_future.result(timeout=5)
    final = engine.get_task(task.task_id)
    assert final == finish_snapshot
    if cancel_snapshot.cancellation_requested_at is None:
        assert final.state == "completed" and final.final_answer == "Answer"
    else:
        assert (
            final.state == "cancelled" and final.execution_result.final_answer is None
        )
    assert engine.cancel_task(task.task_id) == final
    assert setup.repository.claim(task.task_id) is None


def test_restart_orphans_fail_queued_resume_and_history_survives(setup):
    initial, runtime, _ = setup.build()
    interrupted = setup.submit(initial)
    pending = setup.submit(initial)
    cancelled = setup.submit(initial)
    initial.cancel_task(cancelled.task_id)
    assert setup.repository.claim(interrupted.task_id).state == "running"
    setup.repository.cancel(interrupted.task_id)  # Unobserved signal survives crash.
    setup.database[0].dispose()
    reopened_db = create_database_engine(setup.database[1])
    reopened = TaskEngine(TaskRepository(create_session_factory(reopened_db)), runtime)

    async def execute():
        async with reopened:
            orphan = reopened.get_task(interrupted.task_id)
            assert orphan.state == "failed" and orphan.reason == "execution_interrupted"
            assert orphan.execution_result is None and orphan.finished_at is not None
            assert reopened.get_task(cancelled.task_id).state == "cancelled"
            assert reopened.get_task(pending.task_id).state == "queued"
            return await reopened.wait_task(pending.task_id)

    try:
        completed = run(execute())
        assert completed.state == "completed"
        history = reopened.list_tasks()
        reopened_db.dispose()
        assert reopened.list_tasks() == history
    finally:
        reopened_db.dispose()


def test_project_removed_after_submit_fails_without_losing_history(setup):
    engine, _, provider = setup.build()
    task = setup.submit(engine)
    setup.registry.remove_project(task.project_id)

    async def execute():
        async with engine:
            return await engine.wait_task(task.task_id)

    failed = run(execute())
    assert (
        failed.reason == "invalid_configuration"
        and failed.project_id == task.project_id
    )
    assert not provider.requests


def test_live_root_failure_is_execution_time_validation(setup):
    engine, _, provider = setup.build()
    setup.root.rename(setup.root.with_name("moved"))
    task = setup.submit(engine)  # Registration/configuration remains valid.

    async def execute():
        async with engine:
            return await engine.wait_task(task.task_id)

    failed = run(execute())
    assert failed.state == "failed" and not provider.requests


@pytest.mark.parametrize("kind", ["exception", "binding", "state", "unexpected-object"])
def test_unexpected_runtime_failures_never_persist_raw_details(setup, kind):
    engine, runtime, _ = setup.build()
    task = setup.submit(engine)

    async def bad_run(**binding):
        if kind == "exception":
            raise RuntimeError("PRIVATE EXCEPTION AND CREDENTIAL")
        if kind == "unexpected-object":
            return {"reasoning": "PRIVATE"}
        return ExecutionResult(
            project_id=binding["project_id"],
            agent_id=binding["agent_id"],
            worker_id="wrong-worker" if kind == "binding" else binding["worker_id"],
            state="failed" if kind == "state" else "completed",
            reason="completed",
            final_answer="PRIVATE",
            steps=0,
            tool_call_count=0,
            tool_output_bytes=0,
            usage=(),
            trace=(),
        )

    runtime.run = bad_run

    async def execute():
        async with engine:
            return await engine.wait_task(task.task_id)

    failed = run(execute())
    assert failed.state == "failed"
    assert failed.error_code == (
        "runtime_error" if kind == "exception" else "invalid_runtime_result"
    )
    assert "PRIVATE" not in failed.model_dump_json()


def test_safe_tool_trace_and_observed_usage_persist_without_source_or_thinking(setup):
    tools = repository_toolset(
        ProjectIndex(setup.registry, IndexRepository(setup.sessions)),
        RepositoryTools(setup.registry),
    )
    agent = setup.agent.model_copy(update={"allowed_tools": ("read_file",)})
    provider = Provider(
        [
            GenerationResult(
                content="",
                model="model",
                reasoning="OPAQUE_PRIVATE",
                tool_calls=[
                    ToolCall(
                        id="read", name="read_file", arguments={"path": "source.py"}
                    )
                ],
            ),
            GenerationResult(
                content="Found evidence.",
                model="model",
                reasoning="FINAL_PRIVATE",
                token_usage=TokenUsage(output_tokens=4),
            ),
        ]
    )
    engine, runtime, _ = setup.build(provider, tools=tools, agents=[agent])
    task = setup.submit(engine)

    async def execute():
        async with engine:
            return await engine.wait_task(task.task_id)

    completed = run(execute())
    assert completed.execution_result.usage == (None, TokenUsage(output_tokens=4))
    assert completed.execution_result.tool_call_count == 1
    assert completed.execution_result.tool_output_bytes > 0
    requests = [e for e in completed.execution_result.trace if e.kind == "tool_request"]
    assert requests[0].arguments == {
        "path": "<redacted>",
        "start_line": 1,
        "end_line": None,
        "max_lines": 200,
    }
    assert provider.requests[1].messages[-2].reasoning == "OPAQUE_PRIVATE"
    with setup.sessions() as session:
        raw = session.scalar(
            select(TaskRecord.execution_result).where(
                TaskRecord.task_id == task.task_id
            )
        )
    assert "PRIVATE" not in str(raw)
    assert "reasoning" not in str(raw) and "thinking" not in str(raw)
    assert "SOURCE_BODY" not in str(raw)
    assert engine.get_task(task.task_id) == completed
    setup.database[0].dispose()
    reopened_database = create_database_engine(setup.database[1])
    try:
        reopened = TaskEngine(
            TaskRepository(create_session_factory(reopened_database)), runtime
        )
        assert reopened.get_task(task.task_id) == completed
    finally:
        reopened_database.dispose()


def test_task_migration_from_previous_head_and_metadata_match(database):
    engine, _ = database
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0003_project_root_identity")
        assert "tasks" not in inspect(connection).get_table_names()
        command.upgrade(config, "0004_tasks")
        assert "tasks" in inspect(connection).get_table_names()
        command.check(config)


def test_missing_ids_and_safe_storage_errors(setup):
    engine, _, _ = setup.build()
    for identity in ("invalid", uuid4()):
        for operation in (engine.get_task, engine.cancel_task):
            with pytest.raises(TaskNotFound):
                operation(identity)
    TaskRecord.__table__.drop(setup.database[0])
    with pytest.raises(TaskStorageError) as caught:
        engine.list_tasks()
    assert str(caught.value) == "Could not access Task storage"
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("concurrency", [0, 33, True, 1.5])
def test_invalid_concurrency_rejected(setup, concurrency):
    with pytest.raises(TaskValidationError):
        setup.build(concurrency=concurrency)
