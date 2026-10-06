"""Task telemetry through the normal executor, migrated SQLite and offline Providers."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import SQLAlchemyError

from agentforge.agents import repository_toolset
from agentforge.agents.models import RuntimeLimits
from agentforge.core.inference import (
    GenerationResult,
    GenerationTiming,
    TokenUsage,
    ToolCall,
)
from agentforge.core.provider_errors import BackendUnavailable, ProviderTimeout
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.index import IndexRepository
from agentforge.db.models import TaskRecord, TaskTelemetryRecord
from agentforge.db.tasks import TaskRepository
from agentforge.db.telemetry import TelemetryRepository
from agentforge.index.service import ProjectIndex
from agentforge.tasks.engine import TaskEngine
from agentforge.telemetry.models import (
    TelemetryNotFound,
    TelemetryUnavailable,
    TelemetryValidationError,
)
from agentforge.telemetry.service import TelemetryService
from agentforge.tools.service import RepositoryTools
from agentforge.workers.config import WorkersConfig

from . import test_tasks
from .test_tasks import BlockingProvider, Provider, run

setup = test_tasks.setup
KNOWN_USAGE = TokenUsage(input_tokens=10, output_tokens=6)


@pytest.fixture
def telemetry(setup):
    return TelemetryService(TelemetryRepository(setup.sessions))


def execute(engine, task):
    async def work():
        async with engine:
            return await engine.wait_task(task.task_id)

    return run(work())


def response(*, usage=KNOWN_USAGE, duration=2.0, **changes):
    return GenerationResult(
        content="Safe answer",
        model="model",
        token_usage=usage,
        generation_timing=GenerationTiming(
            total_seconds=8.0,
            load_seconds=1.0,
            prompt_seconds=5.0,
            output_seconds=duration,
        ),
        **changes,
    )


class Clock:
    value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_success_identity_monotonic_timings_and_known_counts(setup, telemetry):
    clock = Clock()

    class TimedProvider(Provider):
        async def generate(self, worker, request):
            assert setup.database[0].pool.checkedout() == 0
            clock.advance(8)
            return response()

    _, runtime, _ = setup.build(TimedProvider(), clock=clock)
    engine = TaskEngine(setup.repository, runtime, clock=clock)
    task = setup.submit(engine)
    assert task.provider == "fake" and task.model == "model"
    assert task.telemetry_status == "pending"
    with pytest.raises(TelemetryNotFound):
        telemetry.get_for_task(task.task_id)
    clock.advance(3)
    final = execute(engine, task)
    record = telemetry.get_for_task(str(task.task_id))
    assert final.telemetry_status == "recorded"
    assert (
        record.task_id,
        record.project_id,
        record.agent_id,
        record.worker_id,
        record.provider,
        record.model,
    ) == (task.task_id, setup.project.id, "general", "home-i5", "fake", "model")
    assert (
        record.state == record.reason == "completed" and record.error_category is None
    )
    assert record.created_at == task.created_at
    assert (
        record.started_at == final.started_at
        and record.finished_at == final.finished_at
    )
    assert record.finished_at.tzinfo == UTC
    assert record.queue_duration_seconds == 3
    assert (
        record.execution_duration_seconds == record.model_request_duration_seconds == 8
    )
    assert record.total_duration_seconds == 11
    assert record.model_call_count == 1
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (
        10,
        6,
        16,
    )
    assert record.token_usage_complete
    assert (
        record.observed_prompt_tokens == 10 and record.observed_completion_tokens == 6
    )
    assert record.prompt_observed_turns == record.completion_observed_turns == 1
    assert record.backend_total_duration_seconds == 8
    assert record.model_load_duration_seconds == 1
    assert record.prompt_evaluation_duration_seconds == 5
    assert record.generation_duration_seconds == 2
    assert record.tokens_per_second == 3  # Not 6/8 or 6/11.
    assert record.ttft_seconds is None
    assert (
        record.tool_call_count
        == record.tool_output_bytes
        == record.total_tool_duration_seconds
        == 0
    )


@pytest.mark.parametrize(
    "usage,duration,expected",
    [
        (None, 2.0, (None, None, None, None)),
        (TokenUsage(input_tokens=10), 2.0, (10, None, None, None)),
        (TokenUsage(output_tokens=6), 2.0, (None, 6, None, 3.0)),
        (TokenUsage(input_tokens=0, output_tokens=0), 2.0, (0, 0, 0, 0.0)),
        (TokenUsage(input_tokens=10, output_tokens=6), None, (10, 6, 16, None)),
        (TokenUsage(input_tokens=10, output_tokens=6), 0.0, (10, 6, 16, None)),
    ],
)
def test_missing_partial_zero_counts_and_throughput(
    setup, telemetry, usage, duration, expected
):
    engine, _, _ = setup.build(Provider([response(usage=usage, duration=duration)]))
    task = setup.submit(engine)
    execute(engine, task)
    record = telemetry.get_for_task(task.task_id)
    assert (
        record.prompt_tokens,
        record.completion_tokens,
        record.total_tokens,
        record.tokens_per_second,
    ) == expected
    assert record.ttft_seconds is None
    assert record.token_usage_complete == (expected[2] is not None)


def tool_runtime(setup, provider, clock=None):
    tools = repository_toolset(
        ProjectIndex(setup.registry, IndexRepository(setup.sessions)),
        RepositoryTools(setup.registry),
    )
    if clock is not None:
        read = tools["read_file"]
        original = read.execute

        def timed(*args):
            clock.advance(4)
            return original(*args)

        tools["read_file"] = replace(read, execute=timed)
    agent = setup.agent.model_copy(update={"allowed_tools": ("read_file",)})
    return setup.build(
        provider, tools=tools, agents=[agent], **({"clock": clock} if clock else {})
    )


def tool_turn(usage=KNOWN_USAGE, duration=2.0, calls=2):
    return response(
        usage=usage,
        duration=duration,
        tool_calls=[
            ToolCall(id=f"call-{i}", name="read_file", arguments={"path": "source.py"})
            for i in range(calls)
        ],
    )


@pytest.mark.parametrize(
    "second_usage,second_duration,complete",
    [
        (TokenUsage(input_tokens=5, output_tokens=8), 4.0, True),
        (None, 4.0, False),
        (TokenUsage(input_tokens=5, output_tokens=8), None, True),
    ],
)
def test_multiple_turns_tools_and_partial_totals(
    setup, telemetry, second_usage, second_duration, complete
):
    provider = Provider(
        [tool_turn(), response(usage=second_usage, duration=second_duration)]
    )
    engine, _, _ = tool_runtime(setup, provider)
    task = setup.submit(engine)
    final = execute(engine, task)
    record = telemetry.get_for_task(task.task_id)
    assert record.model_call_count == 2
    assert record.tool_call_count == 2
    durations = [
        e.duration_seconds
        for e in final.execution_result.trace
        if e.kind == "tool_result"
    ]
    assert record.total_tool_duration_seconds == sum(durations)
    assert record.tool_output_bytes == final.execution_result.tool_output_bytes > 0
    assert record.token_usage_complete == complete
    if second_usage is None:
        assert (
            record.prompt_tokens
            is record.completion_tokens
            is record.total_tokens
            is None
        )
        assert (
            record.observed_prompt_tokens == 10
            and record.observed_completion_tokens == 6
        )
        assert record.prompt_observed_turns == record.completion_observed_turns == 1
        assert record.tokens_per_second is None
    else:
        assert (
            record.prompt_tokens,
            record.completion_tokens,
            record.total_tokens,
        ) == (15, 14, 29)
        assert record.tokens_per_second == (14 / 6 if second_duration else None)
    assert record.generation_duration_seconds == (6 if second_duration else None)
    assert record.ttft_seconds is None
    payload = record.model_dump_json()
    for sensitive in (
        "source.py",
        "SOURCE_BODY",
        "Safe answer",
        "path",
        "arguments",
        "reasoning",
        "endpoint",
    ):
        assert sensitive not in payload
    with setup.sessions() as session:
        row = session.get(TaskTelemetryRecord, task.task_id)
        assert all(
            column.type.__class__.__name__ != "JSON" for column in row.__table__.columns
        )


def test_tool_and_model_durations_are_distinct_without_double_counting(
    setup, telemetry
):
    clock = Clock()

    class TimedProvider(Provider):
        async def generate(self, worker, request):
            clock.advance(8)
            return await super().generate(worker, request)

    _, runtime, _ = tool_runtime(
        setup, TimedProvider([tool_turn(), response()]), clock=clock
    )
    engine = TaskEngine(setup.repository, runtime, clock=clock)
    task = setup.submit(engine)
    clock.advance(3)
    execute(engine, task)
    row = telemetry.get_for_task(task.task_id)
    assert row.model_request_duration_seconds == 16
    assert row.total_tool_duration_seconds == 8
    assert row.execution_duration_seconds == 24
    assert row.total_duration_seconds == 27
    assert row.generation_duration_seconds == 4
    assert row.tokens_per_second == 12 / 4


def test_tool_cancellation_without_result_event_leaves_duration_unknown(
    setup, telemetry
):
    provider = Provider([tool_turn(calls=1)])
    tools = repository_toolset(
        ProjectIndex(setup.registry, IndexRepository(setup.sessions)),
        RepositoryTools(setup.registry),
    )
    original = tools["read_file"].execute

    def cancel_during_tool(*args):
        engine.cancel_task(task.task_id)
        return original(*args)

    tools["read_file"] = replace(tools["read_file"], execute=cancel_during_tool)
    agent = setup.agent.model_copy(update={"allowed_tools": ("read_file",)})
    engine, _, _ = setup.build(provider, tools=tools, agents=[agent])
    task = setup.submit(engine)
    execute(engine, task)
    row = telemetry.get_for_task(task.task_id)
    assert row.state == "cancelled" and row.tool_call_count == 1
    assert row.total_tool_duration_seconds is None
    assert row.tool_output_bytes == 0  # Runtime never accepted this output.


def test_raw_provider_compatibility_metrics_and_reasoning_are_never_persisted(
    setup, telemetry
):
    engine, _, _ = setup.build(
        Provider(
            [
                response(
                    reasoning="PRIVATE REASONING",
                    timing={"PRIVATE RAW": "backend secret"},
                ).model_copy(update={"usage": {"PRIVATE RAW": "backend secret"}})
            ]
        )
    )
    task = setup.submit(engine)
    final = execute(engine, task)
    payload = telemetry.get_for_task(task.task_id).model_dump_json()
    assert "PRIVATE" not in payload and "secret" not in payload
    assert "PRIVATE" not in final.model_dump_json()


def test_late_cancel_outcome_matches_task_and_duplicate_finish_retains_metrics(
    setup, telemetry
):
    engine, runtime, _ = setup.build(Provider([response()]))
    task = setup.submit(engine)
    original = runtime.run

    async def late_cancel(**binding):
        result = await original(**binding)
        engine.cancel_task(task.task_id)
        return result

    runtime.run = late_cancel
    final = execute(engine, task)
    before = telemetry.get_for_task(task.task_id)
    assert final.state == before.state == "cancelled"
    assert before.reason == "cancelled" and before.error_category is None
    assert before.completion_tokens == 6 and before.tokens_per_second == 3
    setup.repository.finish(task.task_id, error_code="runtime_error")
    assert telemetry.get_for_task(task.task_id) == before


@pytest.mark.parametrize(
    "error,reason",
    [
        (BackendUnavailable("RAW SECRET backend"), "provider_error"),
        (ProviderTimeout("RAW SECRET backend"), "provider_timeout"),
    ],
)
def test_failure_preserves_explicit_worker_and_unknown_metrics(
    setup, telemetry, error, reason
):
    engine, _, provider = setup.build(Provider([error]))
    task = setup.submit(engine, worker_id="home-i5")
    execute(engine, task)
    record = telemetry.get_for_task(task.task_id)
    assert record.state == "failed" and record.reason == record.error_category == reason
    assert record.model_call_count == 1 and record.model_request_duration_seconds >= 0
    assert (
        record.prompt_tokens is record.completion_tokens is record.total_tokens is None
    )
    assert (
        record.generation_duration_seconds
        is record.ttft_seconds
        is record.tokens_per_second
        is None
    )
    assert [w.id for w in provider.workers] == ["home-i5"]
    assert "SECRET" not in record.model_dump_json()


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("configuration", "invalid_configuration"),
        ("limit", "max_steps"),
        ("security", "tool_not_allowed"),
    ],
)
def test_configuration_limit_security_safe_categories(setup, telemetry, kind, reason):
    provider = Provider([tool_turn(calls=1)])
    engine, _, _ = tool_runtime(setup, provider)
    if kind == "limit":
        agent = setup.agent.model_copy(
            update={
                "allowed_tools": ("read_file",),
                "limits": RuntimeLimits(max_steps=1),
            }
        )
        tools = repository_toolset(
            ProjectIndex(setup.registry, IndexRepository(setup.sessions)),
            RepositoryTools(setup.registry),
        )
        engine, _, _ = setup.build(provider, agents=[agent], tools=tools)
    if kind == "security":
        engine, _, _ = setup.build(provider)
    task = setup.submit(engine)
    if kind == "configuration":
        setup.registry.remove_project(task.project_id)
    final = execute(engine, task)
    record = telemetry.get_for_task(task.task_id)
    assert final.telemetry_status == "recorded"
    assert record.state == "failed" and record.reason == record.error_category == reason
    if kind == "configuration":
        assert record.model_call_count == 0 and not provider.requests
        assert record.prompt_tokens is record.total_tokens is None


def test_failed_second_turn_does_not_imply_complete_task_counts(setup, telemetry):
    engine, _, _ = tool_runtime(
        setup, Provider([tool_turn(), BackendUnavailable("PRIVATE")])
    )
    task = setup.submit(engine)
    execute(engine, task)
    row = telemetry.get_for_task(task.task_id)
    assert row.model_call_count == 2 and row.observed_completion_tokens == 6
    assert (
        row.completion_tokens
        is row.total_tokens
        is row.generation_duration_seconds
        is row.tokens_per_second
        is None
    )
    assert not row.token_usage_complete


def test_step_failing_before_generation_is_not_a_model_call(
    setup, telemetry, monkeypatch
):
    import agentforge.agents.runtime as runtime_module

    def failed_request(**values):
        raise ValueError("PRIVATE construction failure")

    monkeypatch.setattr(runtime_module, "GenerationRequest", failed_request)
    engine, _, provider = setup.build()
    task = setup.submit(engine)
    final = execute(engine, task)
    assert final.execution_result.steps == 1 and not provider.requests
    row = telemetry.get_for_task(task.task_id)
    assert row.state == "failed" and row.model_call_count == 0
    assert row.prompt_tokens is row.completion_tokens is row.total_tokens is None
    assert row.model_request_duration_seconds == 0
    assert "PRIVATE" not in row.model_dump_json()


def test_queued_cancel_zero_attempts_but_no_invented_tokens(setup, telemetry):
    engine, _, provider = setup.build()
    task = setup.submit(engine)
    final = engine.cancel_task(task.task_id)
    row = telemetry.get_for_task(task.task_id)
    assert (
        final.telemetry_status == "recorded" and row.state == row.reason == "cancelled"
    )
    assert row.started_at is None and row.execution_duration_seconds == 0
    assert row.model_call_count == row.tool_call_count == row.tool_output_bytes == 0
    assert (
        row.queue_duration_seconds >= 0
        and row.total_duration_seconds == row.queue_duration_seconds
    )
    assert (
        row.prompt_tokens
        is row.completion_tokens
        is row.total_tokens
        is row.ttft_seconds
        is row.tokens_per_second
        is None
    )
    assert engine.cancel_task(task.task_id) == final and not provider.requests
    assert len(telemetry.list_telemetry()) == 1


@pytest.mark.parametrize(
    "mode", ["cooperative", "shutdown", "shutdown_cancel_requested"]
)
def test_running_cancellation_retains_observations(setup, telemetry, mode):
    provider = BlockingProvider()
    engine, _, _ = setup.build(provider)
    task = setup.submit(engine)

    async def work():
        await engine.start()
        await provider.entered.wait()
        if mode != "shutdown":
            engine.cancel_task(task.task_id)
        if mode == "cooperative":
            provider.release.set()
            await engine.wait_task(task.task_id)
        await engine.close()

    run(work())
    row = telemetry.get_for_task(task.task_id)
    assert row.state == ("failed" if mode == "shutdown" else "cancelled")
    assert row.reason == ("executor_cancelled" if mode == "shutdown" else "cancelled")
    assert row.model_call_count == 1 and row.model_request_duration_seconds >= 0
    assert row.execution_duration_seconds >= row.model_request_duration_seconds
    assert row.completion_tokens == (3 if mode == "cooperative" else None)
    assert row.tokens_per_second is row.ttft_seconds is None
    assert provider.cleaned == 1


def test_restart_unknown_runtime_and_refresh_pending_target(setup, telemetry):
    initial, _, _ = setup.build()
    orphan = setup.submit(initial)
    pending = setup.submit(initial)
    setup.repository.claim(
        orphan.task_id, target=("fake", "old-model"), queue_duration_seconds=2
    )
    new_workers = WorkersConfig(
        workers=[setup.workers.workers[1].model_copy(update={"model": "new-model"})]
    )
    replacement, _, _ = setup.build(workers=new_workers)
    execute(replacement, pending)
    row = telemetry.get_for_task(orphan.task_id)
    assert (
        row.state == "failed"
        and row.reason == row.error_category == "execution_interrupted"
    )
    assert row.model == "old-model" and row.queue_duration_seconds == 2
    for field in (
        "execution_duration_seconds",
        "total_duration_seconds",
        "model_call_count",
        "tool_call_count",
        "tool_output_bytes",
        "model_request_duration_seconds",
        "completion_tokens",
        "tokens_per_second",
    ):
        assert getattr(row, field) is None
    resumed = telemetry.get_for_task(pending.task_id)
    assert resumed.model == "new-model" and resumed.queue_duration_seconds is None
    assert (
        resumed.total_duration_seconds is None
        and resumed.execution_duration_seconds >= 0
    )


def test_distinct_workers_providers_models_no_endpoint_storage_or_routing(
    setup, telemetry
):
    workers = WorkersConfig(
        workers=[
            w.model_copy(update={"provider": f"provider-{i}", "model": f"model-{i}"})
            for i, w in enumerate(setup.workers.workers)
        ]
    )
    providers = {}
    for worker in workers.workers:
        provider = Provider()
        provider.name = worker.provider
        providers[worker.provider] = provider
    engine, _, _ = setup.build(workers=workers, providers=providers)
    tasks = [setup.submit(engine, worker_id=w.id) for w in workers.workers]

    async def work():
        async with engine:
            for task in tasks:
                await engine.wait_task(task.task_id)

    run(work())
    for task, worker in zip(tasks, workers.workers, strict=True):
        row = telemetry.get_for_task(task.task_id)
        assert (row.worker_id, row.provider, row.model) == (
            worker.id,
            worker.provider,
            worker.model,
        )
        assert [w.id for w in providers[worker.provider].workers] == [worker.id]
        assert str(worker.endpoint) not in row.model_dump_json()
    for field in ("worker_id", "provider", "model"):
        expected = sorted(
            getattr(w, "id" if field == "worker_id" else field) for w in workers.workers
        )
        groups = telemetry.compare(group_by=field)
        assert [row.value for row in groups] == expected
        assert all(row.execution_count == row.completed_count == 1 for row in groups)
        assert all(row.failed_count == row.cancelled_count == 0 for row in groups)
        assert telemetry.compare(group_by=field, limit=1, offset=2) == [groups[2]]


def test_missing_worker_after_submit_is_not_replaced_and_comparison_stays_unknown(
    setup, telemetry
):
    initial, _, _ = setup.build()
    task = setup.submit(initial, worker_id="home-i5")
    workers = WorkersConfig(workers=[setup.workers.workers[0]])
    engine, _, provider = setup.build(workers=workers)
    final = execute(engine, task)
    row = telemetry.get_for_task(task.task_id)
    assert final.reason == row.reason == "invalid_configuration"
    assert row.worker_id == "home-i5" and row.provider is row.model is None
    assert row.model_call_count == 0 and not provider.workers
    group = telemetry.compare(group_by="provider")[0]
    assert group.value is None and group.execution_count == group.failed_count == 1
    assert (
        group.observed_prompt_tokens
        is group.observed_completion_tokens
        is group.tokens_per_second
        is None
    )
    assert group.throughput_execution_count == group.token_complete_execution_count == 0


def test_history_reopens_and_survives_config_project_task_removal(setup, telemetry):
    engine, runtime, _ = setup.build()
    task = setup.submit(engine)
    execute(engine, task)
    expected = telemetry.get_for_task(task.task_id)
    setup.registry.remove_project(task.project_id)
    runtime._workers.clear()
    with setup.sessions.begin() as session:
        session.execute(delete(TaskRecord).where(TaskRecord.task_id == task.task_id))
    setup.database[0].dispose()
    reopened = create_database_engine(setup.database[1])
    try:
        new = TelemetryService(TelemetryRepository(create_session_factory(reopened)))
        assert new.get_for_task(task.task_id) == expected
        assert new.list_telemetry(
            worker_id="home-i5", provider="fake", model="model"
        ) == [expected]
    finally:
        reopened.dispose()


def test_filters_time_range_order_ties_and_pagination(setup, telemetry, tmp_path):
    engine, _, _ = setup.build()
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = setup.registry.register_project("Other", other_root)
    tasks = [
        setup.submit(engine),
        setup.submit(engine, agent_id="other-agent", worker_id="ai395"),
        setup.submit(engine, project_id=other.id),
    ]
    for task in tasks:
        engine.cancel_task(task.task_id)
    assert [r.task_id for r in telemetry.list_telemetry()] == [
        t.task_id for t in reversed(tasks)
    ]
    assert (
        telemetry.list_telemetry(
            project_id=setup.project.id,
            agent_id="other-agent",
            worker_id="ai395",
            provider="fake",
            model="model",
            state="cancelled",
        )[0].task_id
        == tasks[1].task_id
    )
    assert telemetry.list_telemetry(provider="missing") == []
    assert telemetry.list_telemetry(limit=1, offset=1)[0].task_id == tasks[1].task_id
    start = tasks[0].created_at
    assert (
        len(
            telemetry.list_telemetry(
                created_from=start, created_before=tasks[2].created_at
            )
        )
        == 2
    )
    tie = datetime.now(UTC)
    with setup.sessions.begin() as session:
        session.execute(update(TaskTelemetryRecord).values(created_at=tie))
    rows = telemetry.list_telemetry(
        created_from=tie, created_before=tie + timedelta(seconds=1)
    )
    assert [r.task_id for r in rows] == sorted([t.task_id for t in tasks], reverse=True)
    assert telemetry.list_telemetry(created_before=tie) == []


@pytest.mark.parametrize(
    "filters",
    [
        {"limit": 0},
        {"limit": 1001},
        {"limit": True},
        {"offset": -1},
        {"offset": 0.5},
        {"state": "invented"},
        {"project_id": "bad"},
        {"unknown": "value"},
        {"created_from": datetime(2026, 1, 1)},
        {
            "created_from": datetime.now(UTC),
            "created_before": datetime(2020, 1, 1, tzinfo=UTC),
        },
    ],
)
def test_invalid_query_bounds_filters_are_safe(telemetry, filters):
    with pytest.raises(TelemetryValidationError):
        telemetry.list_telemetry(**filters)


def test_get_missing_bad_ids_and_invalid_comparison(telemetry):
    with pytest.raises(TelemetryNotFound):
        telemetry.get_for_task(uuid4())
    with pytest.raises(TelemetryValidationError):
        telemetry.get_for_task("invalid")
    with pytest.raises(TelemetryValidationError):
        telemetry.compare(group_by="best_worker")
    with pytest.raises(TelemetryValidationError):
        telemetry.compare(group_by="model", limit=0)


@pytest.mark.parametrize("group", ["worker_id", "model", "provider"])
def test_comparisons_observations_counts_weighted_throughput_and_nulls(
    setup, telemetry, group
):
    engine, _, _ = setup.build(
        Provider(
            [
                response(),
                response(
                    usage=TokenUsage(input_tokens=4, output_tokens=12), duration=6.0
                ),
                response(usage=None),
                BackendUnavailable("PRIVATE"),
            ]
        )
    )
    tasks = [setup.submit(engine) for _ in range(5)]
    engine.cancel_task(tasks[-1].task_id)

    async def work():
        async with engine:
            for task in tasks:
                await engine.wait_task(task.task_id)

    run(work())
    rows = telemetry.compare(group_by=group)
    assert len(rows) == 1
    row = rows[0]
    assert (
        row.execution_count,
        row.completed_count,
        row.failed_count,
        row.cancelled_count,
    ) == (5, 3, 1, 1)
    assert row.success_rate == 3 / 5 and row.runtime_observation_count == 5
    assert row.average_execution_duration_seconds >= 0
    assert row.observed_prompt_tokens == 14 and row.observed_completion_tokens == 18
    assert row.token_complete_execution_count == row.throughput_execution_count == 2
    assert row.tokens_per_second == 18 / 8  # Ratio of sums, not mean of 3 and 2.
    missing = telemetry.compare(group_by=group, state="failed")[0]
    assert (
        missing.observed_prompt_tokens
        is missing.observed_completion_tokens
        is missing.tokens_per_second
        is None
    )
    assert missing.throughput_execution_count == 0
    assert (
        "best" not in row.model_dump_json() and "recommend" not in row.model_dump_json()
    )
    assert telemetry.compare(group_by=group, offset=1) == []


@pytest.mark.parametrize("fault", ["builder", "database"])
def test_telemetry_fault_does_not_corrupt_task_and_is_explicit(
    setup, telemetry, monkeypatch, fault
):
    import agentforge.db.telemetry as storage

    if fault == "builder":

        def broken(*args, **kwargs):
            raise RuntimeError("PRIVATE PROMPT credentials")

        monkeypatch.setattr(storage, "summarize", broken)
    else:
        from sqlalchemy import event

        def broken(connection, cursor, statement, parameters, context, many):
            if statement.startswith("INSERT INTO task_telemetry"):
                raise SQLAlchemyError("PRIVATE DATABASE parameters")

        event.listen(setup.database[0], "before_cursor_execute", broken)
    engine, _, _ = setup.build()
    task = setup.submit(engine)
    final = execute(engine, task)
    assert final.state == "completed" and final.final_answer == "Evidence found."
    assert final.telemetry_status == "unavailable"
    with pytest.raises(TelemetryUnavailable, match="not recorded") as caught:
        telemetry.get_for_task(task.task_id)
    assert (
        "PRIVATE" not in str(caught.value) and "PRIVATE" not in final.model_dump_json()
    )
    assert telemetry.list_telemetry() == []


def test_safe_query_storage_failure(setup, telemetry):
    TaskTelemetryRecord.__table__.drop(setup.database[0])
    with pytest.raises(
        TelemetryUnavailable, match="Could not access telemetry storage"
    ):
        telemetry.list_telemetry()


def test_migration_from_0004_retains_history_and_metadata_agrees(database):
    engine, _ = database
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    identity = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0004_tasks")
        assert "task_telemetry" not in inspect(connection).get_table_names()
        from sqlalchemy import text

        connection.execute(
            text(
                "INSERT INTO tasks (task_id, project_id, agent_id, worker_id, "
                "request, state, created_at, updated_at, reason, finished_at) "
                "VALUES (:id, :project, 'general', 'home-i5', 'PRIVATE old request', "
                "'completed', :now, :now, 'completed', :now)"
            ),
            {"id": identity.hex, "project": uuid4().hex, "now": now.isoformat()},
        )
        command.upgrade(config, "head")
        assert "task_telemetry" in inspect(connection).get_table_names()
        command.check(config)
    repository = TaskRepository(create_session_factory(engine))
    task = repository.get(identity)
    assert (
        task.request == "PRIVATE old request" and task.telemetry_status == "unavailable"
    )
    assert task.provider is task.model is None
    with pytest.raises(TelemetryUnavailable):
        TelemetryService(
            TelemetryRepository(create_session_factory(engine))
        ).get_for_task(identity)
    with engine.connect() as connection:
        assert connection.scalar(select(TaskTelemetryRecord.task_id)) is None
