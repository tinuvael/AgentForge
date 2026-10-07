"""Single-process bounded executor; the director supplies every execution binding."""

import asyncio
from collections.abc import Callable
from threading import Lock, get_ident
from time import monotonic
from uuid import UUID

from pydantic import ValidationError

from agentforge.agents.models import (
    CancellationToken,
    ExecutionObservations,
    ExecutionResult,
)
from agentforge.agents.runtime import AgentRuntime
from agentforge.councils.models import MAX_PARTICIPANTS, Council, InvalidCouncil
from agentforge.db.tasks import TaskRepository
from agentforge.tasks.models import (
    TERMINAL_STATES,
    Task,
    TaskNotFound,
    TaskState,
    TaskStorageError,
    TaskValidationError,
)
from agentforge.tasks.observation import TaskObserver

_OWNERS: set[object] = set()
_OWNERS_LOCK = Lock()


class TaskEngine:
    """Use one engine per database and call its operations on its owning thread.

    Submission may precede start. start()/close() explicitly own the executor;
    no schema creation, health probes or inference happen during construction.
    """

    def __init__(
        self,
        repository: TaskRepository,
        runtime: AgentRuntime,
        *,
        concurrency: int = 1,
        clock: Callable[[], float] = monotonic,
        workspaces=None,
    ) -> None:
        if type(concurrency) is not int or not 1 <= concurrency <= 32:
            raise TaskValidationError("Concurrency must be between 1 and 32")
        self._repository = repository
        self._runtime = runtime
        self._workspaces = workspaces
        self._concurrency = concurrency
        self._thread = get_ident()
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        self._loops: list[asyncio.Task] = []
        self._tokens: dict[UUID, CancellationToken] = {}
        self._started = False
        self._closed = False
        self._shutdown_task: asyncio.Task | None = None
        self._clock = clock
        self._queued_at: dict[UUID, float] = {}
        self.observer = TaskObserver()

    def _check_thread(self):
        if get_ident() != self._thread:
            raise RuntimeError("Task Engine operations require the owning thread")
        if (
            self._started
            and not self._closed
            and asyncio.get_running_loop() is not self._event_loop
        ):
            raise RuntimeError("Task Engine operations require the owning event loop")

    @staticmethod
    def _id(task_id: UUID | str) -> UUID:
        try:
            return task_id if isinstance(task_id, UUID) else UUID(task_id)
        except (ValueError, TypeError, AttributeError):
            raise TaskNotFound("Task ID is not registered") from None

    def submit(
        self, *, project_id: UUID | str, agent_id: str, worker_id: str, task: str
    ) -> Task:
        self._check_thread()
        if self._closed:
            raise RuntimeError("Task Engine is closed")
        try:
            identity = self._runtime.validate_binding(
                project_id=project_id, agent_id=agent_id, worker_id=worker_id, task=task
            )
        except ValueError:
            raise TaskValidationError("Invalid execution binding") from None
        provider, model = self._runtime.execution_target(worker_id)
        queued_at = self._clock()
        submitted = self._repository.add(
            project_id=identity,
            agent_id=agent_id,
            worker_id=worker_id,
            request=task,
            provider=provider,
            model=model,
        )
        self._queued_at[submitted.task_id] = queued_at
        self._wake.set()
        self._changed.set()
        self.observer.notify(submitted.task_id)
        return submitted

    @property
    def available(self) -> bool:
        self._check_thread()
        return (
            self._started
            and not self._closed
            and all(not loop.done() for loop in self._loops)
        )

    def submit_council(
        self, *, project_id: UUID, agent_id: str, worker_ids: tuple[str, ...], task: str
    ) -> tuple[Council, tuple[Task, ...]]:
        self._check_thread()
        if self._closed:
            raise RuntimeError("Task Engine is closed")
        if not 2 <= len(worker_ids) <= MAX_PARTICIPANTS or len(set(worker_ids)) != len(
            worker_ids
        ):
            raise InvalidCouncil("Supply 2 to 16 distinct Workers")
        if (
            getattr(self._runtime, "workspace_mode", lambda _: "project_readonly")(
                agent_id
            )
            == "isolated_write"
        ):
            raise InvalidCouncil("Council requires a read-only Agent")
        # Validate every participant before any write, probe or inference.
        targets = []
        for worker_id in worker_ids:
            try:
                identity = self._runtime.validate_binding(
                    project_id=project_id,
                    agent_id=agent_id,
                    worker_id=worker_id,
                    task=task,
                )
            except ValueError:
                raise TaskValidationError("Invalid execution binding") from None
            targets.append((worker_id, *self._runtime.execution_target(worker_id)))
        queued_at = self._clock()
        council, tasks = self._repository.add_council(
            project_id=identity,
            agent_id=agent_id,
            request=task,
            targets=tuple(targets),
        )
        for participant in tasks:
            self._queued_at[participant.task_id] = queued_at
            self.observer.notify(participant.task_id)
        self._wake.set()
        self._changed.set()
        return council, tasks

    def active_counts(self) -> dict[TaskState, int]:
        self._check_thread()
        return self._repository.active_counts()

    def state_counts(self) -> dict[TaskState, int]:
        self._check_thread()
        return self._repository.state_counts()

    def history(self, **filters):
        self._check_thread()
        return self._repository.history(**filters)

    def get_task(self, task_id: UUID | str) -> Task:
        self._check_thread()
        return self._repository.get(self._id(task_id))

    def list_tasks(
        self,
        *,
        state: TaskState | None = None,
        project_id: UUID | str | None = None,
        agent_id: str | None = None,
        worker_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Task]:
        self._check_thread()
        return self._repository.list(
            state=state,
            project_id=self._id(project_id) if project_id is not None else None,
            agent_id=agent_id,
            worker_id=worker_id,
            limit=limit,
            offset=offset,
        )

    def cancel_task(self, task_id: UUID | str) -> Task:
        self._check_thread()
        identity = self._id(task_id)
        queued_at = self._queued_at.get(identity)
        task = self._repository.cancel(
            identity,
            queue_duration_seconds=(
                max(0, self._clock() - queued_at) if queued_at is not None else None
            ),
        )
        if task.state in TERMINAL_STATES:
            self._queued_at.pop(identity, None)
        token = self._tokens.get(task.task_id)
        if token is not None and task.cancellation_requested_at is not None:
            token.cancel()
        self._wake.set()
        self._changed.set()
        self.observer.notify(
            identity, "terminal" if task.state in TERMINAL_STATES else "refresh"
        )
        return task

    async def start(self) -> None:
        self._check_thread()
        if self._closed:
            raise RuntimeError("Task Engine is closed")
        if self._started:
            return
        key = self._repository.ownership_key
        with _OWNERS_LOCK:
            if key in _OWNERS:
                raise RuntimeError("A Task Engine already owns this database")
            _OWNERS.add(key)
        try:
            self._repository.recover_running()
            if self._workspaces is not None:
                self._workspaces.recover(self._repository)
        except BaseException:
            with _OWNERS_LOCK:
                _OWNERS.remove(key)
            raise
        self._event_loop = asyncio.get_running_loop()
        self._started = True
        self._loops = [
            asyncio.create_task(
                self._execute_loop(), name=f"agentforge-executor-{index}"
            )
            for index in range(self._concurrency)
        ]
        for loop in self._loops:
            loop.add_done_callback(lambda _: self._changed.set())
        self._changed.set()

    def _validated_result(self, task: Task, result: ExecutionResult) -> ExecutionResult:
        # Serialize only the Phase 05 result schema, never provider/history objects.
        result = ExecutionResult.model_validate(
            {name: getattr(result, name) for name in ExecutionResult.model_fields}
        )
        expected_state = (
            "completed"
            if result.reason == "completed"
            else "cancelled"
            if result.reason == "cancelled"
            else "failed"
        )
        if (
            UUID(str(result.project_id)) != task.project_id
            or result.agent_id != task.agent_id
            or result.worker_id != task.worker_id
            or result.state != expected_state
            or min(result.steps, result.tool_call_count, result.tool_output_bytes) < 0
        ):
            raise ValueError("Invalid runtime result")
        if result.state != "completed":
            result = result.model_copy(update={"final_answer": None})
        return result.model_copy(update={"coding_result": None})

    async def _execute_loop(self):
        while not self._closed:
            self._wake.clear()
            identity = self._repository.next_queued_id()
            if identity is None:
                await self._wake.wait()
                continue
            queued = self._repository.get(identity)
            queued_at = self._queued_at.pop(identity, None)
            task = self._repository.claim(
                identity,
                target=self._runtime.execution_target(queued.worker_id),
                queue_duration_seconds=(
                    max(0, self._clock() - queued_at) if queued_at is not None else None
                ),
            )
            if task is None:
                continue
            token = CancellationToken()
            # No await between claim and registration: cancellation cannot miss start.
            self._tokens[identity] = token
            self._changed.set()
            self.observer.begin(identity)
            observations = ExecutionObservations(
                on_trace=lambda event, identity=identity: self.observer.record(
                    identity, event
                )
            )
            execution_started = self._clock()

            def finish(
                identity=identity,
                observations=observations,
                execution_started=execution_started,
                **outcome,
            ):
                return self._repository.finish(
                    identity,
                    observations=observations,
                    execution_duration_seconds=max(
                        0, self._clock() - execution_started
                    ),
                    **outcome,
                )

            coding = False
            provisioned = False
            try:
                try:
                    coding = (
                        getattr(
                            self._runtime,
                            "workspace_mode",
                            lambda _: "project_readonly",
                        )(task.agent_id)
                        == "isolated_write"
                    )
                    arguments = {}
                    if coding:
                        if self._workspaces is None:
                            raise ValueError("Coding workspaces are not configured")
                        self._workspaces.create(task)
                        arguments["coding_session"] = self._workspaces.bind(task, token)
                        provisioned = True
                    result = await self._runtime.run(
                        **arguments,
                        project_id=task.project_id,
                        agent_id=task.agent_id,
                        worker_id=task.worker_id,
                        task=task.request,
                        cancellation=token,
                        observations=observations,
                    )
                except asyncio.CancelledError:
                    finish(error_code="executor_cancelled")
                    raise
                except Exception:
                    finish(
                        error_code="workspace_provisioning_failed"
                        if coding and not provisioned
                        else "runtime_error"
                    )
                else:
                    try:
                        result = self._validated_result(task, result)
                    except (ValidationError, ValueError, TypeError, AttributeError):
                        finish(error_code="invalid_runtime_result")
                    else:
                        finish(result=result)
            finally:
                if (
                    self._workspaces is not None
                    and getattr(
                        self._runtime, "workspace_mode", lambda _: "project_readonly"
                    )(task.agent_id)
                    == "isolated_write"
                ):
                    try:
                        self._workspaces.finalize(self._repository.get(identity))
                        self._repository.attach_coding_result(
                            identity, self._workspaces.get(identity)
                        )
                    except Exception:
                        pass  # Partial workspace remains; no destructive recovery.
                self._tokens.pop(identity, None)
                self._changed.set()
                self.observer.finish(identity)
            # Even a fully synchronous scripted Provider must yield to callers.
            await asyncio.sleep(0)

    async def wait_task(self, task_id: UUID | str) -> Task:
        """Wait for a terminal snapshot; cancelling this waiter cancels no work."""
        self._check_thread()
        while True:
            self._changed.clear()
            task = self.get_task(task_id)
            if task.state in TERMINAL_STATES:
                return task
            if not self._started or self._closed:
                raise RuntimeError("Task Engine is not running")
            if any(loop.done() for loop in self._loops):
                # Retrieve failures without exposing SQL/exception details.
                raise TaskStorageError("Task executor stopped before completion")
            await self._changed.wait()

    async def _shutdown(self):
        try:
            for token in self._tokens.values():
                token.cancel()
            for loop in self._loops:
                loop.cancel()
            results = await asyncio.gather(*self._loops, return_exceptions=True)
            if any(isinstance(result, Exception) for result in results):
                raise TaskStorageError("Task executor could not finish cleanup")
        finally:
            with _OWNERS_LOCK:
                _OWNERS.discard(self._repository.ownership_key)
            self._changed.set()

    async def close(self) -> None:
        """Cancel local executor work and await Provider cleanup; never replay it.

        Queued rows survive shutdown. Active rows fail as executor_cancelled,
        unless an explicit cancel request already won. Remote inference may continue.
        """
        self._check_thread()
        self._closed = True
        self._queued_at.clear()
        self.observer.close()
        if not self._started:
            return
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown())
        # Cancellation of this caller must not interrupt cleanup/release ownership.
        await asyncio.shield(self._shutdown_task)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.close()
