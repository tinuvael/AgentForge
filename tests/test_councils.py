"""Offline Councils on real migrated SQLite and the existing bounded executor."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from agentforge.application.service import ServiceError
from agentforge.core.inference import GenerationResult
from agentforge.core.provider_errors import ProviderTimeout
from agentforge.councils.models import CouncilNotFound, InvalidCouncil
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.models import CouncilParticipantRecord, CouncilRecord, TaskRecord
from agentforge.db.tasks import TaskRepository
from agentforge.projects.errors import ProjectNotFound
from agentforge.tasks.models import TaskStorageError, TaskValidationError
from tests.test_mcp import PRIVATE, run
from tests.test_mcp import setup as setup_fixture

setup = setup_fixture


def arguments(setup, workers=("ai395", "home-i5"), **overrides):
    return (
        {key: value for key, value in setup.binding.items() if key != "worker_id"}
        | {"worker_ids": workers}
        | overrides
    )


def row_counts(setup):
    with create_session_factory(setup.app.database)() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(table))
            for table in (CouncilRecord, CouncilParticipantRecord, TaskRecord)
        )


async def finish(app, council):
    for participant in council.participants:
        await app.tasks.wait_task(participant.task_id)
    return app.councils.get(council.council_id)


@pytest.mark.parametrize(
    "workers", [("home-i5", "local-4080"), ("cloud", "ai395", "local-4080", "home-i5")]
)
def test_order_explicit_bindings_independence_and_telemetry(setup, workers):
    async def execute():
        app = setup.app
        await app.start()
        try:
            council = app.councils.submit(**arguments(setup, workers))
            assert not council.terminal and council.participant_counts["queued"] == len(
                workers
            )
            assert [p.worker_id for p in council.participants] == list(workers)
            assert len({p.task_id for p in council.participants}) == len(workers)
            assert all(
                p.provider == "fake" and p.model == "configured-model"
                for p in council.participants
            )
            assert row_counts(setup) == (1, len(workers), len(workers))
            result = await finish(app, council)
            assert result.terminal and result.participant_counts["completed"] == len(
                workers
            )
            assert set(w.id for w in setup.provider.workers) == set(workers)
            assert len(setup.provider.requests) == len(workers)
            messages = [r.messages for r in setup.provider.requests]
            assert all(m == messages[0] for m in messages)
            assert all(
                len(m) == 2 and m[1].content == setup.binding["task"] for m in messages
            )
            assert all(p.final_answer == "Evidence found." for p in result.participants)
            for p in result.participants:
                assert p.telemetry_status == "recorded"
                assert app.telemetry.get_for_task(p.task_id).worker_id == p.worker_id
                assert app.councils.for_task(p.task_id) == council.council_id
            assert len(app.tasks.history()) == len(workers)
            assert PRIVATE not in result.model_dump_json()
            assert "request" not in result.model_dump()
        finally:
            await app.close()

    run(execute())


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"worker_ids": []}, InvalidCouncil),
        ({"worker_ids": ["ai395"]}, InvalidCouncil),
        ({"worker_ids": ["ai395", "ai395"]}, InvalidCouncil),
        ({"worker_ids": [str(i) for i in range(17)]}, InvalidCouncil),
        ({"worker_ids": ["ai395", "missing"]}, ServiceError),
        ({"agent_id": "missing"}, ServiceError),
        ({"project_id": str(uuid4())}, ProjectNotFound),
        ({"task": ""}, InvalidCouncil),
        ({"task": "   "}, InvalidCouncil),
        ({"task": "x" * 32769}, InvalidCouncil),
        ({"worker_ids": ["ai395", 1]}, InvalidCouncil),
        ({"worker_ids": ["ai395", "all workers"]}, ServiceError),
    ],
)
def test_invalid_request_never_creates_any_rows(setup, overrides, error):
    async def execute():
        await setup.app.start()
        try:
            with pytest.raises(error):
                setup.app.councils.submit(**arguments(setup, **overrides))
            assert row_counts(setup) == (0, 0, 0)
            assert not setup.provider.requests
        finally:
            await setup.app.close()

    run(execute())


def test_late_binding_validation_failure_is_atomic(setup, monkeypatch):
    async def execute():
        await setup.app.start()
        runtime = setup.app.tasks._runtime
        original = runtime.validate_binding

        def validate(**binding):
            if binding["worker_id"] == "home-i5":
                raise ValueError(PRIVATE)
            return original(**binding)

        monkeypatch.setattr(runtime, "validate_binding", validate)
        try:
            with pytest.raises(TaskValidationError):
                setup.app.councils.submit(**arguments(setup))
            assert row_counts(setup) == (0, 0, 0)
        finally:
            await setup.app.close()

    run(execute())


@pytest.mark.parametrize("stage", ["tasks", "members", "commit"])
def test_transaction_failure_rolls_back_all_rows(setup, stage):
    async def execute():
        await setup.app.start()
        db = setup.app.database

        def fail_sql(_conn, _cursor, statement, *_):
            if (
                stage == "tasks"
                and statement.startswith("INSERT INTO tasks")
                or stage == "members"
                and statement.startswith("INSERT INTO council_participants")
            ):
                raise OperationalError("private statement", {}, Exception(PRIVATE))

        def fail_commit(_conn):
            raise OperationalError("private commit", {}, Exception(PRIVATE))

        hook, handler = (
            ("commit", fail_commit)
            if stage == "commit"
            else ("before_cursor_execute", fail_sql)
        )
        event.listen(db, hook, handler)
        try:
            with pytest.raises(TaskStorageError) as error:
                setup.app.councils.submit(**arguments(setup))
            assert PRIVATE not in str(error.value)
        finally:
            event.remove(db, hook, handler)
        try:
            assert row_counts(setup) == (0, 0, 0)
            assert not setup.app.tasks._queued_at
            assert not setup.provider.requests
            result = setup.app.councils.submit(**arguments(setup))
            assert (await finish(setup.app, result)).terminal
        finally:
            await setup.app.close()

    run(execute())


def test_partial_failure_retains_success_and_no_replacement(setup):
    async def execute():
        setup.provider.turns = [
            GenerationResult(content="Opinion one", model="fake"),
            ProviderTimeout(PRIVATE),
            GenerationResult(content="Opinion three", model="fake"),
        ]
        await setup.app.start()
        try:
            council = setup.app.councils.submit(
                **arguments(setup, ("ai395", "home-i5", "cloud"))
            )
            result = await finish(setup.app, council)
            assert result.terminal
            assert result.participant_counts == {
                "queued": 0,
                "running": 0,
                "completed": 2,
                "failed": 1,
                "cancelled": 0,
            }
            assert sorted(
                p.final_answer for p in result.participants if p.final_answer
            ) == ["Opinion one", "Opinion three"]
            failed = next(p for p in result.participants if p.state == "failed")
            assert failed.error_code == "provider_timeout"
            assert len(setup.provider.workers) == 3
            assert PRIVATE not in result.model_dump_json()
        finally:
            await setup.app.close()

    run(execute())


def test_submit_during_execution_respects_engine_concurrency(setup, monkeypatch):
    async def execute():
        app = setup.build(concurrency=2)
        active = maximum = entered = 0
        both = asyncio.Event()
        release = asyncio.Event()
        original = setup.provider.generate

        async def generate(worker, request):
            nonlocal active, maximum, entered
            active += 1
            entered += 1
            maximum = max(maximum, active)
            if active == 2:
                both.set()
            try:
                await release.wait()
                return await original(worker, request)
            finally:
                active -= 1

        monkeypatch.setattr(setup.provider, "generate", generate)
        await app.start()
        try:
            ordinary = app.delegate_task(**setup.binding)
            council = app.councils.submit(
                **arguments(setup, ("cloud", "ai395", "home-i5", "local-4080"))
            )
            await both.wait()
            assert entered == 2 and maximum == 2
            assert (
                app.councils.get(council.council_id).participant_counts["queued"] == 3
            )
            second = app.councils.submit(**arguments(setup))
            assert row_counts(setup) == (2, 6, 7)
            release.set()
            await app.tasks.wait_task(ordinary.task_id)
            assert (await finish(app, council)).terminal
            assert (await finish(app, second)).terminal
            assert entered == 7 and maximum == 2
        finally:
            await app.close()

    run(execute())


def test_queued_cancellation_is_idempotent(setup):
    async def execute():
        await setup.app.start()
        try:
            council = setup.app.councils.submit(**arguments(setup))
            cancelled = setup.app.councils.cancel(council.council_id)
            assert cancelled.terminal and cancelled.participant_counts["cancelled"] == 2
            assert setup.app.councils.cancel(council.council_id) == cancelled
            await asyncio.sleep(0)
            assert not setup.provider.requests
        finally:
            await setup.app.close()

    run(execute())


def test_running_individual_and_group_cancellation(setup):
    async def execute():
        setup.provider.release.clear()
        await setup.app.start()
        try:
            council = setup.app.councils.submit(**arguments(setup))
            await setup.provider.entered.wait()
            current = setup.app.councils.get(council.council_id)
            queued = next(p for p in current.participants if p.state == "queued")
            setup.app.cancel_task(task_id=queued.task_id)
            assert (
                setup.app.councils.get(council.council_id).participant_counts[
                    "cancelled"
                ]
                == 1
            )
            cancelled = setup.app.councils.cancel(council.council_id)
            assert not cancelled.terminal
            running = next(p for p in cancelled.participants if p.state == "running")
            assert running.cancellation_requested_at
            assert setup.app.councils.cancel(council.council_id) == cancelled
            setup.provider.release.set()
            done = await finish(setup.app, council)
            assert done.terminal and done.participant_counts["cancelled"] == 2
            assert all(p.final_answer is None for p in done.participants)
        finally:
            await setup.app.close()

    run(execute())


@pytest.mark.parametrize("cancel_first", [True, False])
def test_cancellation_completion_race_preserves_task_winner(setup, cancel_first):
    async def execute():
        await setup.app.start()
        try:
            council = setup.app.councils.submit(**arguments(setup))
            repository = TaskRepository(create_session_factory(setup.app.database))
            first = council.participants[0].task_id
            repository.claim(first)
            if cancel_first:
                setup.app.councils.cancel(council.council_id)
                repository.finish(first, error_code="provider_timeout")
            else:
                repository.finish(first, error_code="provider_timeout")
                setup.app.councils.cancel(council.council_id)
            done = setup.app.councils.get(council.council_id)
            assert done.terminal
            assert done.participants[0].state == (
                "cancelled" if cancel_first else "failed"
            )
            assert done.participants[1].state == "cancelled"
            assert setup.app.councils.cancel(council.council_id) == done
        finally:
            await setup.app.close()

    run(execute())


@pytest.mark.parametrize("claimed", [True, False])
def test_cancellation_claim_race_uses_normal_semantics(setup, claimed):
    async def execute():
        await setup.app.start()
        try:
            council = setup.app.councils.submit(**arguments(setup))
            repository = TaskRepository(create_session_factory(setup.app.database))
            first = council.participants[0].task_id
            if claimed:
                repository.claim(first)
            setup.app.councils.cancel(council.council_id)
            if claimed:
                repository.finish(first, error_code="runtime_error")
            else:
                assert repository.claim(first) is None
            assert (
                setup.app.councils.get(council.council_id).participant_counts[
                    "cancelled"
                ]
                == 2
            )
        finally:
            await setup.app.close()

    run(execute())


def test_reopen_recovery_and_project_deregistration(setup):
    async def execute():
        app = setup.app
        await app.start()
        council = app.councils.submit(**arguments(setup))
        repository = TaskRepository(create_session_factory(app.database))
        running = council.participants[0].task_id
        repository.claim(running)
        # Simulate process loss before executors claim anything else.
        for loop in app.tasks._loops:
            loop.cancel()
        await app.close()
        reopened = setup.build()
        before = reopened.councils.get(council.council_id)
        assert [p.state for p in before.participants] == ["running", "queued"]
        await reopened.start()
        try:
            recovered = reopened.councils.get(council.council_id)
            assert recovered.participants[0].error_code == "execution_interrupted"
            assert recovered.participants[1].state == "queued"
            done = await finish(reopened, council)
            assert (
                done.terminal
                and done.participant_counts["failed"] == 1
                and done.participant_counts["completed"] == 1
            )
            assert len(setup.provider.requests) == 1
            setup.registry.remove_project(setup.project.id)
            assert reopened.councils.get(council.council_id) == done
            assert reopened.councils.list().councils[0].project_name is None
            assert len(reopened.tasks.history()) == 2
        finally:
            await reopened.close()

    run(execute())


@pytest.mark.parametrize(
    "limit,offset", [(0, 0), (101, 0), (True, 0), (1, -1), (1, 1000001)]
)
def test_history_bounds(setup, limit, offset):
    with pytest.raises(InvalidCouncil):
        setup.app.councils.list(limit=limit, offset=offset)


def test_deterministic_history_compact_query_and_pagination(setup):
    async def execute():
        await setup.app.start()
        try:
            ids = [
                setup.app.councils.submit(**arguments(setup)).council_id
                for _ in range(3)
            ]
            with create_session_factory(setup.app.database)() as session:
                created = session.get(CouncilRecord, ids[0]).created_at
                session.execute(update(CouncilRecord).values(created_at=created))
                session.commit()
            queries = []

            def capture(_conn, _cursor, statement, *_):
                queries.append(statement)

            event.listen(setup.app.database, "before_cursor_execute", capture)
            try:
                page = setup.app.councils.list(limit=2)
            finally:
                event.remove(setup.app.database, "before_cursor_execute", capture)
            assert len(queries) == 1
            assert "request" not in queries[0] and "execution_result" not in queries[0]
            assert [c.council_id for c in page.councils] == sorted(ids, reverse=True)[
                :2
            ]
            assert page.next_offset == 2
            last = setup.app.councils.list(limit=2, offset=2)
            assert len(last.councils) == 1 and last.next_offset is None
            assert (
                setup.app.councils.list().councils[0].participant_counts["queued"] == 2
            )
            for council in setup.app.councils.list().councils:
                setup.app.councils.cancel(council.council_id)
        finally:
            await setup.app.close()

    run(execute())


@pytest.mark.parametrize("identity", ["malformed", str(uuid4())])
def test_missing_council(setup, identity):
    with pytest.raises(CouncilNotFound):
        setup.app.councils.get(identity)
    with pytest.raises(CouncilNotFound):
        setup.app.councils.cancel(identity)


def test_fk_history_protection(setup):
    async def execute():
        await setup.app.start()
        council = setup.app.councils.submit(**arguments(setup))
        setup.app.councils.cancel(council.council_id)
        await setup.app.close()
        engine = create_database_engine(setup.database[1])
        try:
            with create_session_factory(engine)() as session:
                with pytest.raises(IntegrityError):
                    session.execute(
                        delete(TaskRecord).where(
                            TaskRecord.task_id == council.participants[0].task_id
                        )
                    )
                    session.commit()
                session.rollback()
                with pytest.raises(IntegrityError):
                    session.execute(delete(CouncilRecord))
                    session.commit()
                session.rollback()
                assert session.scalar(select(func.count()).select_from(TaskRecord)) == 2
                assert (
                    session.scalar(
                        select(func.count()).select_from(CouncilParticipantRecord)
                    )
                    == 2
                )
        finally:
            engine.dispose()

    run(execute())


@pytest.mark.parametrize("count", [8, 16])
def test_larger_councils_use_same_executor_bound(setup, count):
    from agentforge.application.service import Application
    from agentforge.workers.config import WorkersConfig

    async def execute():
        workers = WorkersConfig(
            workers=[
                setup.workers.workers[0].model_copy(update={"id": f"worker-{i}"})
                for i in range(count)
            ]
        )
        app = Application(
            create_database_engine(setup.database[1]),
            workers,
            providers={"fake": setup.provider},
            concurrency=2,
        )
        both = asyncio.Event()
        release = asyncio.Event()
        active = maximum = 0
        original = setup.provider.generate

        async def generate(worker, request):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            if active == 2:
                both.set()
            try:
                await release.wait()
                return await original(worker, request)
            finally:
                active -= 1

        setup.provider.generate = generate
        await app.start()
        try:
            council = app.councils.submit(
                **arguments(setup, tuple(w.id for w in workers.workers))
            )
            await both.wait()
            current = app.councils.get(council.council_id)
            assert current.participant_counts["running"] == 2
            assert current.participant_counts["queued"] == count - 2
            release.set()
            done = await finish(app, council)
            assert done.terminal and done.participant_counts["completed"] == count
            assert maximum == 2 and len(setup.provider.requests) == count
            assert [p.worker_id for p in done.participants] == [
                w.id for w in workers.workers
            ]
        finally:
            await app.close()

    run(execute())


def test_mixed_completed_running_queued_cancellation_preserves_answer(
    setup, monkeypatch
):
    async def execute():
        second_started = asyncio.Event()
        original = setup.provider.generate
        turns = 0

        async def generate(worker, request):
            nonlocal turns
            turns += 1
            if turns == 2:
                setup.provider.release.clear()
                second_started.set()
            return await original(worker, request)

        monkeypatch.setattr(setup.provider, "generate", generate)
        await setup.app.start()
        try:
            council = setup.app.councils.submit(
                **arguments(setup, ("ai395", "home-i5", "cloud"))
            )
            await second_started.wait()
            current = setup.app.councils.get(council.council_id)
            assert all(
                current.participant_counts[state] == 1
                for state in ["completed", "running", "queued"]
            )
            completed = next(p for p in current.participants if p.state == "completed")
            during = setup.app.councils.cancel(council.council_id)
            assert (
                next(p for p in during.participants if p.task_id == completed.task_id)
                == completed
            )
            assert not during.terminal
            setup.provider.release.set()
            done = await finish(setup.app, council)
            assert (
                done.terminal
                and done.participant_counts["completed"] == 1
                and done.participant_counts["cancelled"] == 2
            )
            assert (
                next(p for p in done.participants if p.task_id == completed.task_id)
                == completed
            )
        finally:
            await setup.app.close()

    run(execute())


def test_cancellation_storage_error_can_be_retried(setup, monkeypatch):
    async def execute():
        await setup.app.start()
        try:
            council = setup.app.councils.submit(**arguments(setup))
            original = setup.app.tasks.cancel_task
            calls = 0

            def cancel(task_id):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise TaskStorageError(PRIVATE)
                return original(task_id)

            with monkeypatch.context() as patch:
                patch.setattr(setup.app.tasks, "cancel_task", cancel)
                with pytest.raises(TaskStorageError):
                    setup.app.councils.cancel(council.council_id)
            current = setup.app.councils.get(council.council_id)
            assert (
                current.participant_counts["cancelled"]
                == current.participant_counts["queued"]
                == 1
            )
            assert setup.app.councils.cancel(council.council_id).terminal
        finally:
            await setup.app.close()

    run(execute())


@pytest.mark.parametrize(
    "violation", ["worker", "task", "ordinal", "bound", "foreign_key"]
)
def test_membership_database_constraints(setup, violation):
    async def execute():
        await setup.app.start()
        try:
            council = setup.app.councils.submit(**arguments(setup))
            ordinary = setup.app.delegate_task(**setup.binding)
            row = dict(
                council_id=council.council_id,
                ordinal=2,
                task_id=ordinary.task_id,
                worker_id="new-worker",
            )
            if violation == "worker":
                row["worker_id"] = council.participants[0].worker_id
            elif violation == "task":
                row["task_id"] = council.participants[0].task_id
            elif violation == "ordinal":
                row["ordinal"] = 0
            elif violation == "bound":
                row["ordinal"] = 16
            else:
                row["task_id"] = uuid4()
            with create_session_factory(setup.app.database)() as session:
                session.add(CouncilParticipantRecord(**row))
                with pytest.raises(IntegrityError):
                    session.commit()
                session.rollback()
            assert row_counts(setup) == (1, 2, 3)
            setup.app.councils.cancel(council.council_id)
            setup.app.tasks.cancel_task(ordinary.task_id)
        finally:
            await setup.app.close()

    run(execute())


def test_completed_results_survive_full_application_reopen(setup):
    async def execute():
        await setup.app.start()
        council = setup.app.councils.submit(**arguments(setup))
        done = await finish(setup.app, council)
        await setup.app.close()
        reopened = setup.build()
        try:
            assert reopened.councils.get(council.council_id) == done
            assert all(
                reopened.telemetry.get_for_task(p.task_id).state == "completed"
                for p in done.participants
            )
        finally:
            await reopened.close()

    run(execute())


@pytest.mark.parametrize("operation", ["claim", "finish"])
def test_concurrent_council_cancel_and_task_checkpoint(setup, monkeypatch, operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from agentforge.agents.models import ExecutionResult

    async def execute():
        await setup.app.start()
        try:
            council = setup.app.councils.submit(**arguments(setup))
            repository = TaskRepository(create_session_factory(setup.app.database))
            first = council.participants[0]
            if operation == "finish":
                repository.claim(first.task_id)
            barrier = Barrier(2)
            original = setup.app.tasks._repository.cancel

            def cancel(task_id, **kwargs):
                if task_id == first.task_id:
                    barrier.wait(timeout=5)
                return original(task_id, **kwargs)

            def checkpoint():
                barrier.wait(timeout=5)
                if operation == "claim":
                    return repository.claim(first.task_id)
                return repository.finish(
                    first.task_id,
                    result=ExecutionResult(
                        project_id=setup.project.id,
                        agent_id="repo_explorer",
                        worker_id=first.worker_id,
                        state="completed",
                        reason="completed",
                        final_answer="Race opinion",
                        steps=1,
                        tool_call_count=0,
                        tool_output_bytes=0,
                        usage=(),
                        trace=(),
                    ),
                )

            with (
                monkeypatch.context() as patch,
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                patch.setattr(setup.app.tasks._repository, "cancel", cancel)
                future = pool.submit(checkpoint)
                setup.app.councils.cancel(council.council_id)
                raced = future.result(timeout=5)
            if operation == "claim" and raced is not None:
                repository.finish(first.task_id, error_code="runtime_error")
            done = setup.app.councils.get(council.council_id)
            assert done.terminal and done.participants[1].state == "cancelled"
            assert done.participants[0].state in {"completed", "cancelled"}
            assert done.participants[0].final_answer == (
                "Race opinion" if done.participants[0].state == "completed" else None
            )
            assert setup.app.councils.cancel(council.council_id) == done
        finally:
            await setup.app.close()

    run(execute())
