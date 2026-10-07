"""One bounded, in-process Agent interaction on an explicitly selected Worker."""

import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from time import monotonic
from uuid import UUID

from pydantic import ValidationError

from agentforge.agents.models import (
    Agent,
    CancellationToken,
    ExecutionObservations,
    ExecutionResult,
    ModelTurnObservation,
    RuntimeLimits,
    TerminationReason,
    TraceEvent,
)
from agentforge.agents.tools import Tool, json_text, trace_arguments
from agentforge.coding.models import CodingError, CodingLimit, EditConflict
from agentforge.core.inference import GenerationRequest, Message, Provider, TokenUsage
from agentforge.core.provider_errors import ProviderTimeout
from agentforge.index.models import SymbolNotFound
from agentforge.projects.errors import ProjectError, UnsafeProjectPath
from agentforge.projects.service import ProjectRegistry
from agentforge.tools.errors import (
    GitFailure,
    GitTimeout,
    GitUnavailable,
    InvalidToolArgument,
    NotGitRepository,
    PathNotFound,
    RepositoryIOError,
    SensitivePath,
    UnsupportedTextFile,
)
from agentforge.workers.config import WorkersConfig

# Exact public types, fixed safe codes. Never serialize exception messages/repr.
_RECOVERABLE = {
    PathNotFound: "path_not_found",
    UnsupportedTextFile: "unsupported_text",
    SensitivePath: "sensitive_path",
    InvalidToolArgument: "invalid_arguments",
    NotGitRepository: "not_git_repository",
    GitUnavailable: "git_unavailable",
    GitTimeout: "git_timeout",
    GitFailure: "git_failure",
    RepositoryIOError: "repository_io",
    SymbolNotFound: "symbol_not_found",
    EditConflict: "edit_conflict",
    CodingLimit: "coding_limit",
    CodingError: "coding_unavailable",
}


class _Stop(Exception):
    def __init__(self, reason: TerminationReason):
        self.reason = reason


class AgentRuntime:
    def __init__(
        self,
        *,
        projects: ProjectRegistry,
        workers: WorkersConfig,
        agents: Sequence[Agent],
        providers: Mapping[str, Provider],
        tools: Mapping[str, Tool],
        clock: Callable[[], float] = monotonic,
        coding_tools: Mapping[str, Tool] | None = None,
    ):
        self._projects = projects
        self._workers = {worker.id: worker for worker in workers.workers}
        self._agents = {agent.id: agent for agent in agents}
        if len(self._agents) != len(agents):
            raise ValueError("Agent IDs must be unique")
        self._providers = dict(providers)
        self._tools = dict(tools)
        self._coding_tools = dict(coding_tools or {})
        self._clock = clock

    def workspace_mode(self, agent_id: str) -> str:
        agent = self._agents.get(agent_id)
        return agent.workspace_mode if agent else "project_readonly"

    def execution_target(self, worker_id: str) -> tuple[str | None, str | None]:
        """Configured target only, without probing or selecting another Worker."""
        worker = self._workers.get(worker_id)
        return (worker.provider, worker.model) if worker else (None, None)

    def validate_binding(
        self, *, project_id: UUID | str, agent_id: str, worker_id: str, task: str
    ) -> UUID:
        """Validate configuration only, without probing inference or reading files.

        Task submission and execution share this check. Live root authorization
        and remote availability remain execution-time observations.
        """
        agent = self._agents.get(agent_id)
        worker = self._workers.get(worker_id)
        if (
            agent is None
            or worker is None
            or not isinstance(task, str)
            or not task.strip()
        ):
            raise ValueError("Invalid execution binding")
        tools = (
            self._coding_tools
            if agent.workspace_mode == "isolated_write"
            else self._tools
        )
        provider = self._providers.get(worker.provider_connection or worker.provider)
        if (
            provider is None
            or provider.name != worker.provider
            or len(set(agent.allowed_tools)) != len(agent.allowed_tools)
            or any(
                name not in tools or tools[name].definition.name != name
                for name in agent.allowed_tools
            )
            or (agent.allowed_tools and worker.supports_tools is False)
        ):
            raise ValueError("Invalid execution binding")
        try:
            return self._projects.get_project(project_id).id
        except ProjectError:
            raise ValueError("Invalid execution binding") from None

    async def run(
        self,
        *,
        project_id: UUID | str,
        agent_id: str,
        worker_id: str,
        task: str,
        limits: RuntimeLimits | None = None,
        cancellation: CancellationToken | None = None,
        observations: ExecutionObservations | None = None,
        coding_session=None,
    ) -> ExecutionResult:
        """No default Worker, health-based selection, fallback or persistent Task.

        Synchronous tools are checked before/after each operation, but cannot be
        interrupted mid-call. Their existing subprocess/scan budgets still apply.
        Caller asyncio.CancelledError propagates; token cancellation returns a result.
        """
        step = calls = output_bytes = 0
        observations = (
            observations if observations is not None else ExecutionObservations()
        )
        trace = observations.trace
        usage: list[TokenUsage | None] = []
        cancellation = cancellation or CancellationToken()
        started = self._clock()
        policy = limits or RuntimeLimits()
        deadline = started + policy.timeout_seconds

        def event(kind, **values):
            observations.record_trace(TraceEvent(step=step, kind=kind, **values))

        def check():
            if cancellation.cancelled:
                raise _Stop("cancelled")
            if self._clock() >= deadline:
                raise _Stop("timeout")

        def result(reason: TerminationReason, answer: str | None = None):
            event(
                "termination",
                reason=reason,
                duration_seconds=max(0, self._clock() - started),
            )
            return ExecutionResult(
                project_id=project_id,
                agent_id=agent_id,
                worker_id=worker_id,
                final_answer=answer,
                state="completed"
                if reason == "completed"
                else ("cancelled" if reason == "cancelled" else "failed"),
                reason=reason,
                steps=step,
                tool_call_count=calls,
                tool_output_bytes=output_bytes,
                usage=tuple(usage),
                trace=tuple(trace),
                model_turns=tuple(observations.model_turns),
            )

        try:
            try:
                project_id = self.validate_binding(
                    project_id=project_id,
                    agent_id=agent_id,
                    worker_id=worker_id,
                    task=task,
                )
            except ValueError:
                raise _Stop("invalid_configuration") from None
            agent = self._agents[agent_id]
            worker = self._workers[worker_id]
            provider = self._providers[worker.provider_connection or worker.provider]
            policy = limits or agent.limits
            deadline = started + policy.timeout_seconds
            check()
            if agent.workspace_mode == "isolated_write":
                if coding_session is None or (
                    coding_session.task.project_id != project_id
                    or coding_session.task.agent_id != agent_id
                    or coding_session.task.worker_id != worker_id
                ):
                    raise _Stop("invalid_configuration")
                tool_catalog = coding_session.tools
                roots = coding_session.registry
            else:
                if coding_session is not None:
                    raise _Stop("invalid_configuration")
                tool_catalog = self._tools
                roots = self._projects
            allowed = {name: tool_catalog[name] for name in sorted(agent.allowed_tools)}
            definitions = [tool.definition for tool in allowed.values()]
            messages = [
                Message(role="system", content=agent.system_prompt),
                Message(role="user", content=task),
            ]
            seen_ids: set[str] = set()
            context_tokens = min(
                policy.max_context_tokens,
                worker.context_window or policy.max_context_tokens,
            )

            def context_size():
                # Include opaque reasoning, JSON framing, correlation and schemas.
                return len(
                    json_text(
                        {
                            "messages": [message.model_dump() for message in messages],
                            "tools": [
                                definition.model_dump() for definition in definitions
                            ],
                        }
                    ).encode("utf-8")
                )

            def check_context():
                if (context_size() + 2) // 3 > context_tokens:
                    raise _Stop("context_limit")

            def verify_root():
                # Cached Index queries never authorize a replaced live root.
                with roots.open_root(project_id):
                    pass

            while step < policy.max_steps:
                check()
                verify_root()
                check_context()
                check()
                step += 1
                remaining = deadline - self._clock()
                model_timeout = min(remaining, worker.timeout_seconds)
                request = GenerationRequest(
                    messages=messages, tools=definitions, timeout_seconds=model_timeout
                )
                event("model_request", size_bytes=context_size())
                model_started = self._clock()
                response = None
                try:
                    # The runtime also bounds providers that do not honor overrides.
                    async with asyncio.timeout(model_timeout):
                        response = await provider.generate(worker, request)
                except ProviderTimeout:
                    event(
                        "model_response", success=False, error_code="provider_timeout"
                    )
                    check()
                    raise _Stop("provider_timeout") from None
                except TimeoutError:
                    check()
                    reason = (
                        "timeout"
                        if remaining <= worker.timeout_seconds
                        else "provider_timeout"
                    )
                    event("model_response", success=False, error_code=reason)
                    raise _Stop(reason) from None
                except Exception:
                    check()
                    event("model_response", success=False, error_code="provider_error")
                    raise _Stop("provider_error") from None
                finally:
                    observations.model_turns.append(
                        ModelTurnObservation(
                            request_duration_seconds=max(
                                0, self._clock() - model_started
                            ),
                            token_usage=response.token_usage if response else None,
                            generation_timing=response.generation_timing
                            if response
                            else None,
                        )
                    )
                check()
                usage.append(response.token_usage)
                messages.append(
                    Message(
                        role="assistant",
                        content=response.content,
                        reasoning=response.reasoning,
                        tool_calls=response.tool_calls,
                    )
                )
                check_context()
                event(
                    "model_response",
                    success=True,
                    size_bytes=len(
                        json_text(messages[-1].model_dump()).encode("utf-8")
                    ),
                    duration_seconds=observations.model_turns[
                        -1
                    ].request_duration_seconds,
                )
                if not response.tool_calls:
                    if not response.content.strip():
                        raise _Stop("invalid_response")
                    verify_root()
                    check()
                    return result("completed", response.content)
                # Validate the complete turn's IDs/permissions/count before any I/O.
                identities = [call.id for call in response.tool_calls]
                if len(set(identities)) != len(identities) or seen_ids.intersection(
                    identities
                ):
                    raise _Stop("invalid_response")
                if any(call.name not in allowed for call in response.tool_calls):
                    raise _Stop("tool_not_allowed")
                if calls + len(response.tool_calls) > policy.max_tool_calls:
                    raise _Stop("max_tool_calls")
                seen_ids.update(identities)
                for call in response.tool_calls:
                    check()
                    budget = min(
                        policy.max_tool_result_bytes,
                        policy.max_tool_output_bytes - output_bytes,
                    )
                    if budget < 256:
                        raise _Stop("tool_output_limit")
                    tool = allowed[call.name]
                    calls += 1
                    tool_started = self._clock()
                    code = None
                    try:
                        arguments = tool.arguments.model_validate(call.arguments)
                        arguments.check_policy()
                    except (
                        ValidationError,
                        ValueError,
                        InvalidToolArgument,
                        UnsafeProjectPath,
                    ):
                        code = "invalid_arguments"
                    except SensitivePath:
                        code = "sensitive_path"
                    event(
                        "tool_request",
                        tool_call_id=call.id,
                        tool_name=call.name,
                        arguments=trace_arguments(arguments) if code is None else None,
                    )
                    if code is None:
                        check()
                        verify_root()
                        try:
                            value = tool.execute(project_id, arguments, budget)
                            if inspect.isawaitable(value):
                                async with asyncio.timeout(
                                    max(0.001, deadline - self._clock())
                                ):
                                    value = await value
                            content = json_text({"ok": True, "result": value})
                        except Exception as error:
                            check()
                            code = _RECOVERABLE.get(type(error))
                            if code is None:
                                reason = (
                                    "security_error"
                                    if isinstance(error, UnsafeProjectPath)
                                    else "tool_error"
                                )
                                event(
                                    "tool_result",
                                    tool_call_id=call.id,
                                    tool_name=call.name,
                                    success=False,
                                    error_code=reason,
                                    duration_seconds=max(
                                        0, self._clock() - tool_started
                                    ),
                                )
                                raise _Stop(reason) from None
                        check()
                        verify_root()
                    if code is not None:
                        content = json_text({"ok": False, "error": {"code": code}})
                    check()
                    size = len(content.encode("utf-8"))
                    event(
                        "tool_result",
                        tool_call_id=call.id,
                        tool_name=call.name,
                        success=code is None,
                        error_code=code,
                        size_bytes=size,
                        duration_seconds=max(0, self._clock() - tool_started),
                    )
                    if size > policy.max_tool_result_bytes:
                        raise _Stop("tool_result_limit")
                    if output_bytes + size > policy.max_tool_output_bytes:
                        raise _Stop("tool_output_limit")
                    output_bytes += size
                    observations.tool_output_bytes = output_bytes
                    messages.append(
                        Message(
                            role="tool",
                            content=content,
                            tool_call_id=call.id,
                            tool_name=call.name,
                        )
                    )
                    check_context()
                    check()
            raise _Stop("max_steps")
        except _Stop as stop:
            return result(stop.reason)
        except UnsafeProjectPath:
            return result("security_error")
        except Exception:
            # Unexpected internal/storage faults must not reveal diagnostics.
            return result("tool_error")
