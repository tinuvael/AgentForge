"""Reduce bounded runtime evidence to safe Task-level observations only."""

from agentforge.agents.models import ExecutionObservations, ExecutionResult
from agentforge.tasks.models import Task
from agentforge.telemetry.models import TaskTelemetry


def summarize(
    task: Task,
    *,
    result: ExecutionResult | None = None,
    observations: ExecutionObservations | None = None,
    queue_duration_seconds: float | None = None,
    execution_duration_seconds: float | None = None,
) -> TaskTelemetry:
    """No evidence means unknown, including process-loss recovery.

    Independent input/output completeness requires every attempted call to have
    that count. Partial sums are explicitly named observed_* and never totals.
    """
    turns = (
        result.model_turns
        if result
        else (tuple(observations.model_turns) if observations is not None else None)
    )
    trace = (
        result.trace
        if result
        else (tuple(observations.trace) if observations is not None else ())
    )
    # A runtime step can fail during request construction before generate starts.
    # Count actual request evidence, never equate the step budget with calls.
    if trace or observations is not None:
        count = sum(event.kind == "model_request" for event in trace)
    elif turns:
        count = len(turns)
    else:
        count = 0 if result is not None and result.steps == 0 else None
    calls = (
        result.tool_call_count
        if result
        else (
            sum(event.kind == "tool_request" for event in trace)
            if observations is not None
            else None
        )
    )
    tool_durations = [
        event.duration_seconds for event in trace if event.kind == "tool_result"
    ]
    tool_duration = (
        sum(tool_durations)
        if calls is not None
        and len(tool_durations) == calls
        and all(value is not None for value in tool_durations)
        else None
    )
    values = {}
    for name, field in (("prompt", "input_tokens"), ("completion", "output_tokens")):
        observed = [
            getattr(turn.token_usage, field)
            for turn in turns or ()
            if turn.token_usage is not None
            and getattr(turn.token_usage, field) is not None
        ]
        values[f"{name}_observed_turns"] = len(observed)
        values[f"observed_{name}_tokens"] = sum(observed) if observed else None
        values[f"{name}_tokens"] = (
            sum(observed) if count and len(observed) == count else None
        )
    complete = (
        values["prompt_tokens"] is not None and values["completion_tokens"] is not None
    )
    values["token_usage_complete"] = complete
    values["total_tokens"] = (
        values["prompt_tokens"] + values["completion_tokens"] if complete else None
    )
    totals = [
        turn.token_usage.total_tokens
        for turn in turns or ()
        if turn.token_usage is not None and turn.token_usage.total_tokens is not None
    ]
    if count and len(totals) == count:
        values["total_tokens"] = sum(totals)
    for name, field in (
        ("backend_total_duration_seconds", "total_seconds"),
        ("model_load_duration_seconds", "load_seconds"),
        ("prompt_evaluation_duration_seconds", "prompt_seconds"),
        ("generation_duration_seconds", "output_seconds"),
    ):
        durations = [
            getattr(turn.generation_timing, field)
            for turn in turns or ()
            if turn.generation_timing is not None
            and getattr(turn.generation_timing, field) is not None
        ]
        values[name] = sum(durations) if count and len(durations) == count else None
    compatible = (
        count
        and len(turns or ()) == count
        and all(
            turn.token_usage is not None
            and turn.token_usage.output_tokens is not None
            and turn.generation_timing is not None
            and turn.generation_timing.output_seconds is not None
            and turn.generation_timing.output_seconds > 0
            for turn in turns or ()
        )
    )
    values["tokens_per_second"] = (
        values["completion_tokens"] / values["generation_duration_seconds"]
        if compatible
        else None
    )
    return TaskTelemetry(
        task_id=task.task_id,
        project_id=task.project_id,
        agent_id=task.agent_id,
        worker_id=task.worker_id,
        provider=task.provider,
        model=task.model,
        state=task.state,
        reason=task.reason,
        error_category=task.error_code,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        queue_duration_seconds=queue_duration_seconds,
        execution_duration_seconds=execution_duration_seconds,
        total_duration_seconds=(
            queue_duration_seconds + execution_duration_seconds
            if queue_duration_seconds is not None
            and execution_duration_seconds is not None
            else None
        ),
        model_call_count=count,
        model_request_duration_seconds=(
            sum(turn.request_duration_seconds for turn in turns)
            if turns is not None and count == len(turns)
            else None
        ),
        tool_call_count=calls,
        total_tool_duration_seconds=tool_duration,
        tool_output_bytes=result.tool_output_bytes
        if result
        else (observations.tool_output_bytes if observations is not None else None),
        **values,
    )
