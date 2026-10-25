"""Bounded advisory projections, filtered BEFORE loading evidence. No Provider I/O."""

from datetime import UTC

from agentforge.application.contracts import (
    Candidate,
    CandidateSuggestions,
    SuggestArguments,
)
from agentforge.application.errors import ServiceError
from agentforge.core.capabilities import describe
from agentforge.core.policy import PolicyDenied
from agentforge.workers.policy import connection_id


def suggest(app, *, authorized_ids=None, **fields):
    request = SuggestArguments.model_validate(fields)
    app.projects.get_project(request.project_id)
    agent = app.agent_definition(request.agent_id)
    if agent is None:
        raise ServiceError("agent_not_found")
    workers = sorted(
        (
            w
            for w in app._workers.values()
            if authorized_ids is None or w.id in authorized_ids
        ),
        key=lambda w: w.id,
    )
    values = []
    for worker in workers[request.offset : request.offset + request.limit]:
        reasons = []
        diagnostics = app.worker_diagnostics.get(worker.id)
        capabilities = describe(worker, diagnostics)
        required = set(request.required)
        if request.require_tools or agent.allowed_tools:
            required.add("tool_calling")
        for capability in sorted(required):
            if capability in {"vision", "image", "3d", "video", "speech"}:
                reasons.append("transport_not_implemented_" + capability)
            if capabilities[capability].support != "supported":
                reasons.append(
                    "required_" + capability + "_" + capabilities[capability].support
                )
        if request.min_context_window and (
            worker.context_window is None
            or worker.context_window < request.min_context_window
        ):
            reasons.append("context_capacity_unknown_or_insufficient")
        try:
            app.worker_policy.check(request.project_id, worker, request.task_policy)
        except PolicyDenied as error:
            reasons.append(error.code)
        health = diagnostics.health.latest
        availability = "unknown"
        if health and health.backend_available is not None:
            if not health.backend_available or health.model_available is False:
                availability = "observed_unavailable"
                reasons.append("observed_unavailable")
            elif health.backend_available and health.model_available is True:
                availability = "observed_available"
        row = app.worker_policy.resources.get(connection_id(worker))
        # A later runtime outage takes precedence over earlier diagnostic success.
        if (
            row
            and row.observed_at
            and (
                health is None
                or row.observed_at.replace(tzinfo=UTC) > health.checked_at
            )
            and row.last_error
            in {
                "backend_unavailable",
                "timeout",
                "request_interrupted",
                "authentication_failed",
                "not_found",
                "invalid_response",
                "provider_error",
            }
        ):
            availability = "observed_unavailable"
            if "observed_unavailable" not in reasons:
                reasons.append("observed_unavailable")
        values.append(
            Candidate(
                worker=next(
                    w for w in app._worker_info if w.worker_id == worker.id
                ).model_copy(update={"capabilities": capabilities}),
                eligible=not reasons,
                hard_exclusions=tuple(reasons),
                unknown_capabilities=tuple(
                    k for k, v in capabilities.items() if v.support == "unknown"
                ),
                preferred_supported=tuple(
                    sorted(
                        set(
                            k
                            for k in request.preferred
                            if capabilities[k].support == "supported"
                        )
                    )
                ),
                availability=availability,
                availability_observed_at=row.observed_at
                if row and availability == "observed_unavailable"
                else health.checked_at
                if health
                else None,
                last_failure=row.last_error if row else None,
                failure_observed_at=row.observed_at if row and row.last_error else None,
                cooldown_until=row.cooldown_until if row else None,
                requests_used=row.requests_used if row else 0,
                tokens_used=row.tokens_used if row else 0,
                token_usage_unknown=row.usage_unknown if row else False,
                configured_budget=app.worker_policy.budget(worker).model_dump(),
            )
        )
    return CandidateSuggestions(
        candidates=tuple(values),
        next_offset=request.offset + request.limit
        if len(workers) > request.offset + request.limit
        else None,
        explanation=(
            "Advisory evidence only; no inference or reservation. "
            "No eligible Worker on this page; restrictions were not relaxed."
        )
        if not any(v.eligible for v in values)
        else (
            "Explicit selection required. Declarations and historical observations "
            "do not guarantee availability, quality or zero billing."
        ),
    )
