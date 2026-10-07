"""Bounded observer behavior and execution independence, without network services."""

from contextlib import ExitStack
from dataclasses import asdict
from uuid import uuid4

import pytest

from agentforge.agents.models import ExecutionObservations, TraceEvent
from agentforge.tasks.observation import (
    QUEUE_LIMIT,
    SUBSCRIBER_LIMIT,
    TRACE_LIMIT,
    ObservationUnavailable,
    TaskObserver,
    metadata,
)
from tests.test_mcp import PRIVATE, run
from tests.test_mcp import setup as setup_fixture

setup = setup_fixture


def test_live_projection_drops_private_fields_and_limits_names():
    trace = TraceEvent(
        step=1,
        kind="tool_request",
        tool_name="r" * 1000,
        tool_call_id=PRIVATE,
        arguments={PRIVATE: PRIVATE},
        error_code=PRIVATE,
        success=False,
        duration_seconds=0.5,
    )
    public = asdict(metadata(trace))
    assert len(public["tool_name"]) == 100
    assert public["error_code"] == "internal_error"
    assert PRIVATE not in str(public)
    assert set(public) == {
        "step",
        "kind",
        "tool_name",
        "success",
        "error_code",
        "duration_seconds",
        "reason",
    }
    for code in [
        "provider_error",
        "security_error",
        "invalid_arguments",
        "path_not_found",
        "edit_conflict",
        "coding_limit",
        "coding_unavailable",
    ]:
        assert (
            metadata(trace.model_copy(update={"error_code": code})).error_code == code
        )


def test_trace_callback_failure_does_not_change_recording():
    def broken(_):
        raise RuntimeError(PRIVATE)

    observations = ExecutionObservations(on_trace=broken)
    item = TraceEvent(step=1, kind="model_request")
    observations.record_trace(item)
    assert observations.trace == [item]


def test_bounded_buffers_queues_resync_and_terminal_priority():
    hub = TaskObserver()
    identity = uuid4()
    hub.begin(identity)
    with hub.subscribe(identity) as slow, hub.subscribe(identity) as fast:
        assert slow.get_nowait() == "resync"
        assert fast.get_nowait() == "resync"
        for step in range(TRACE_LIMIT * 5):
            hub.record(identity, TraceEvent(step=step, kind="model_request"))
            assert fast.get_nowait() in {"refresh", "resync"}
            assert slow.qsize() <= QUEUE_LIMIT
        timeline, truncated = hub.timeline(identity)
        assert truncated and len(timeline) == TRACE_LIMIT
        assert timeline[0].step == TRACE_LIMIT * 4
        notices = [slow.get_nowait() for _ in range(slow.qsize())]
        assert "resync" in notices
        for _ in range(QUEUE_LIMIT):
            hub.notify(identity)
        hub.finish(identity)
        assert slow.get_nowait() == "terminal"
        assert hub.timeline(identity) == ((), False)
    assert hub.subscriber_count == 0 and not hub._subscribers


def test_subscription_capacity_disconnect_and_shutdown():
    hub = TaskObserver()
    identities = [uuid4() for _ in range(SUBSCRIBER_LIMIT)]
    with ExitStack() as stack:
        queues = [
            stack.enter_context(hub.subscribe(identity)) for identity in identities
        ]
        assert hub.subscriber_count == SUBSCRIBER_LIMIT
        with pytest.raises(ObservationUnavailable):
            with hub.subscribe(uuid4()):
                pytest.fail("Capacity must be enforced")
        for identity in identities:
            for _ in range(QUEUE_LIMIT):
                hub.notify(identity)
        hub.begin(identities[0])
        hub.close()
        for queue in queues:
            # Overflow semantics guarantee shutdown survives slow consumers.
            notices = [queue.get_nowait() for _ in range(queue.qsize())]
            assert "shutdown" in notices
        assert not hub._subscribers and not hub._buffers
        with pytest.raises(ObservationUnavailable):
            with hub.subscribe(uuid4()):
                pass
    assert hub.subscriber_count == 0


def test_slow_subscriber_does_not_block_execution(setup):
    async def execute():
        await setup.app.start()
        try:
            identity = setup.app.delegate_task(**setup.binding).task_id
            with setup.app.tasks.observer.subscribe(identity) as queue:
                for _ in range(1000):
                    setup.app.tasks.observer.notify(identity)
                assert queue.qsize() <= QUEUE_LIMIT
                task = await setup.app.tasks.wait_task(identity)
                assert task.state == "completed"
                notices = [queue.get_nowait() for _ in range(queue.qsize())]
                assert "terminal" in notices
            assert setup.app.tasks.observer.subscriber_count == 0
            assert setup.app.tasks.observer.timeline(identity) == ((), False)
        finally:
            await setup.app.close()

    run(execute())
