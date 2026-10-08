"""Ephemeral, bounded metadata observation on the executor's owning event loop.

Notifications are hints to reload authoritative snapshots, never Task state.
No replay/event store, timers, browser sessions or execution awaits live here.
"""

import asyncio
from collections import deque
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from math import isfinite
from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import Field

from agentforge.agents.models import TerminationReason, TraceEvent, TraceKind
from agentforge.tasks.models import Task, TaskReason

TRACE_LIMIT = 100
QUEUE_LIMIT = 16
SUBSCRIBER_LIMIT = 128
Notice = Literal["refresh", "resync", "terminal", "shutdown"]
_ERROR_CODES = frozenset(
    code for group in get_args(TaskReason) for code in get_args(group)
) | {
    "path_not_found",
    "unsupported_text",
    "sensitive_path",
    "invalid_arguments",
    "not_git_repository",
    "git_unavailable",
    "git_timeout",
    "git_failure",
    "repository_io",
    "symbol_not_found",
    "edit_conflict",
    "coding_limit",
    "coding_unavailable",
}


@dataclass(frozen=True)
class TimelineEvent:
    step: Annotated[int, Field(ge=0, le=2_147_483_647)]
    kind: TraceKind
    tool_name: Annotated[str, Field(max_length=100)] | None
    success: bool | None
    error_code: Annotated[str, Field(max_length=100)] | None
    duration_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    reason: TerminationReason | None


def metadata(event: TraceEvent) -> TimelineEvent:
    """Explicit allowlist for both live and persisted traces.

    Deliberately drop even redacted tool arguments and provider call IDs.
    No arguments, result, source, reasoning, backend body or exception fields.
    """
    if event.kind not in get_args(TraceKind) or not 0 <= event.step <= 2_147_483_647:
        raise ValueError("Invalid observation metadata")
    return TimelineEvent(
        event.step,
        event.kind,
        event.tool_name[:100] if event.tool_name is not None else None,
        event.success if type(event.success) is bool else None,
        event.error_code
        if event.error_code in _ERROR_CODES
        else "internal_error"
        if event.error_code
        else None,
        event.duration_seconds
        if event.duration_seconds is not None
        and isfinite(event.duration_seconds)
        and event.duration_seconds >= 0
        else None,
        event.reason if event.reason in get_args(TerminationReason) else None,
    )


def task_timeline(
    task: Task, observer: "TaskObserver"
) -> tuple[tuple[TimelineEvent, ...], bool]:
    """Shared bounded projection; a durable result supersedes ephemeral metadata."""
    if task.execution_result is not None:
        trace = task.execution_result.trace
        return tuple(metadata(event) for event in trace[-TRACE_LIMIT:]), len(
            trace
        ) > TRACE_LIMIT
    return observer.timeline(task.task_id)


class ObservationUnavailable(Exception):
    pass


class TaskObserver:
    def __init__(self):
        # Only executing Tasks retain a buffer; bounded by executor concurrency.
        self._buffers: dict[UUID, deque[TimelineEvent]] = {}
        self._truncated: set[UUID] = set()
        self._subscribers: dict[UUID, set[asyncio.Queue[Notice]]] = {}
        self._subscriber_count = 0
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        return self._subscriber_count

    def begin(self, task_id: UUID):
        self._buffers[task_id] = deque(maxlen=TRACE_LIMIT)
        self.notify(task_id)

    def record(self, task_id: UUID, event: TraceEvent):
        buffer = self._buffers.get(task_id)
        if buffer is None or self._closed:
            return
        if len(buffer) == TRACE_LIMIT:
            self._truncated.add(task_id)
        buffer.append(metadata(event))
        self.notify(task_id, "resync" if task_id in self._truncated else "refresh")

    def timeline(self, task_id: UUID) -> tuple[tuple[TimelineEvent, ...], bool]:
        return tuple(self._buffers.get(task_id, ())), task_id in self._truncated

    def notify(self, task_id: UUID, notice: Notice = "refresh"):
        for queue in self._subscribers.get(task_id, ()):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                # Terminal/shutdown must survive overflow so streams can close.
                value = notice if notice in {"terminal", "shutdown"} else "resync"
            else:
                value = notice
            queue.put_nowait(value)

    def finish(self, task_id: UUID):
        # The terminal trace now lives in the existing durable Task result.
        self._buffers.pop(task_id, None)
        self._truncated.discard(task_id)
        self.notify(task_id, "terminal")

    @contextmanager
    def subscribe(self, task_id: UUID):
        with self.subscribe_many((task_id,)) as queue:
            yield queue

    @contextmanager
    def subscribe_many(self, task_ids: Sequence[UUID]):
        """One bounded queue/subscriber observing at most 16 independent Tasks.

        No persisted/in-memory Council map. The caller supplies durable membership;
        individual terminal notices are hints until the whole group is terminal.
        """
        identities = tuple(dict.fromkeys(task_ids))
        if not 1 <= len(identities) <= 16:
            raise ObservationUnavailable("Live observation unavailable")
        if self._closed or self._subscriber_count >= SUBSCRIBER_LIMIT:
            raise ObservationUnavailable("Live observation unavailable")
        queue: asyncio.Queue[Notice] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        for task_id in identities:
            self._subscribers.setdefault(task_id, set()).add(queue)
        self._subscriber_count += 1
        # Always reload on connection/reconnection; no Last-Event-ID replay.
        queue.put_nowait("resync")
        try:
            yield queue
        finally:
            for task_id in identities:
                subscribers = self._subscribers.get(task_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(task_id, None)
            self._subscriber_count -= 1

    def close(self):
        self._closed = True
        for task_id in tuple(self._subscribers):
            self.notify(task_id, "shutdown")
        self._subscribers.clear()
        self._buffers.clear()
        self._truncated.clear()
