"""Shared egress enforcement and bounded inference admission. Never selects targets."""

import hashlib
import ipaddress
import json
import sys
from datetime import UTC, datetime

from agentforge.core.policy import (
    DEFAULT_TASK_POLICY,
    PolicyDenied,
    ResourceBudget,
    TaskPolicy,
)
from agentforge.core.provider_errors import ProviderError
from agentforge.db.resources import denial


def connection_id(worker):
    return worker.provider_connection or "inline:" + worker.id


def literal_loopback(endpoint):
    host = endpoint.host if endpoint else None
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address((host or "").strip("[]"))
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


class WorkerPolicy:
    def __init__(self, config, projects, resources=None, diagnostics=None):
        self.config = config
        self.projects = projects
        self.resources = resources
        self.diagnostics = diagnostics
        self.connections = {p.id: p for p in config.providers}

    def connection(self, worker):
        return self.connections.get(worker.provider_connection)

    def trust(self, worker):
        connection = self.connection(worker)
        declared = connection.trust if connection else worker.trust
        if declared != "unknown":
            return declared
        # Compatibility only for literal loopback, without DNS or model/type inference.
        endpoint = connection.base_url if connection else worker.endpoint
        return "local" if literal_loopback(endpoint) else "unknown"

    def binding(self, worker):
        connection = self.connection(worker)
        payload = {
            "worker": worker.model_dump(
                mode="json",
                exclude={"workflow_review_required"}
                if not worker.workflow_review_required
                else set(),
            ),
            "connection": connection.model_dump(mode="json") if connection else None,
        }
        return {
            "provider_connection": connection_id(worker),
            "target_fingerprint": hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest(),
        }

    def authorize(self, project_id, worker, task_policy=DEFAULT_TASK_POLICY):
        task_policy = TaskPolicy.model_validate(task_policy)
        project = self.projects.get_project(project_id)
        project_policy = project.egress_policy
        restriction = task_policy.egress
        if restriction and restriction.mode == "cloud_allowed":
            if project_policy.mode != "cloud_allowed" or not set(
                restriction.allowed_connections
            ) <= set(project_policy.allowed_connections):
                raise PolicyDenied("egress_expansion")
        trust = self.trust(worker)
        if trust == "unknown":
            raise PolicyDenied("unclassified_connection")
        if trust == "cloud":
            identity = connection_id(worker)
            if (
                project_policy.mode != "cloud_allowed"
                or identity not in project_policy.allowed_connections
            ):
                raise PolicyDenied("egress_denied")
            if restriction and (
                restriction.mode != "cloud_allowed"
                or identity not in restriction.allowed_connections
            ):
                raise PolicyDenied("egress_denied")
        return project

    def budget(self, worker):
        connection = self.connection(worker)
        return connection.budget if connection else ResourceBudget()

    def check(self, project_id, worker, task_policy=DEFAULT_TASK_POLICY):
        self.authorize(project_id, worker, task_policy)
        if task_policy.max_requests == 0:
            raise PolicyDenied("request_budget")
        if task_policy.max_tokens == 0:
            raise PolicyDenied("token_budget")
        if self.budget(worker).max_requests == 0:
            raise PolicyDenied("request_budget")
        if self.budget(worker).max_tokens == 0:
            raise PolicyDenied("token_budget")
        if self.resources:
            row = self.resources.get(connection_id(worker))
            if row and (code := denial(row, self.budget(worker), datetime.now(UTC))):
                raise PolicyDenied(code)
        elif self.budget(worker) != ResourceBudget():
            # Never silently ignore configured persistent budgets.
            raise PolicyDenied("resource_storage_required")
        if self.diagnostics:
            history = self.diagnostics.get(worker.id)
            observations = [
                getattr(history, k).latest
                for k in ("health", "generation", "tools", "streaming")
            ]
            latest = max(
                (
                    o
                    for o in observations
                    if o and o.status not in {"not_probed", "not_applicable"}
                ),
                key=lambda o: o.checked_at,
                default=None,
            )
            if latest and latest.status == "failed":
                if latest.error_code == "rate_limited" and latest.rate_limits:
                    if (
                        datetime.now(UTC) - latest.checked_at
                    ).total_seconds() < latest.rate_limits.retry_after_seconds:
                        raise PolicyDenied("cooldown")
                # Historical failure is advisory; a fresh explicit submission can
                # recover. Suggestions explain it rather than claiming availability.
        return None

    async def generate(
        self,
        project_id,
        worker,
        provider,
        request,
        task_policy,
        usage,
        on_admitted=None,
    ):
        self.check(project_id, worker, task_policy)
        if (
            task_policy.max_requests is not None
            and usage["requests"] >= task_policy.max_requests
        ):
            raise PolicyDenied("request_budget")
        remaining = None
        if task_policy.max_tokens is not None:
            if usage["unknown"]:
                raise PolicyDenied("token_usage_unknown")
            remaining = task_policy.max_tokens - usage["tokens"]
        if self.resources:
            row = self.resources.get(connection_id(worker))
            budget = self.budget(worker)
            if budget.max_tokens is not None:
                connection_remaining = budget.max_tokens - (
                    row.tokens_used if row else 0
                )
                remaining = (
                    min(remaining, connection_remaining)
                    if remaining is not None
                    else connection_remaining
                )
        if remaining is not None:
            if remaining <= 0:
                raise PolicyDenied("token_budget")
            configured_caps = [
                value
                for key, value in worker.options.items()
                if key in {"max_tokens", "max_completion_tokens", "num_predict"}
                and type(value) is int
                and value > 0
            ]
            remaining = (
                min(remaining, *configured_caps) if configured_caps else remaining
            )
            request = request.model_copy(
                update={
                    "max_output_tokens": min(
                        request.max_output_tokens or remaining, remaining
                    )
                }
            )
        if self.resources:
            reserved = self.resources.admit(connection_id(worker), self.budget(worker))
            if reserved is not None:
                request = request.model_copy(
                    update={
                        "max_output_tokens": min(
                            request.max_output_tokens or reserved, reserved
                        )
                    }
                )
        usage["requests"] += 1
        tokens = None
        error_code = "request_interrupted"
        retry_after = None
        try:
            if on_admitted:
                on_admitted()
            response = await provider.generate(worker, request)
            tokens = (
                response.token_usage.accounted_total if response.token_usage else None
            )
            error_code = None
            return response
        except ProviderError as error:
            error_code = (
                error.code
                if error.code
                in {
                    "backend_unavailable",
                    "timeout",
                    "invalid_response",
                    "rejected",
                    "invalid_request",
                    "authentication_failed",
                    "not_found",
                    "rate_limited",
                }
                else "provider_error"
            )
            if error_code == "rate_limited" and error.rate_limits:
                retry_after = error.rate_limits.retry_after_seconds
            raise
        except Exception:
            error_code = "provider_error"
            raise
        finally:
            if tokens is None:
                usage["unknown"] = True
            else:
                usage["tokens"] += tokens
            if self.resources:
                # Persist before any next turn. Storage failure also fails closed.
                original_failure = sys.exc_info()[0] is not None
                try:
                    self.resources.finish(
                        connection_id(worker),
                        tokens=tokens,
                        error_code=error_code,
                        retry_after=retry_after,
                    )
                except Exception:
                    if not original_failure:
                        raise
                    # The original Provider error/cancellation wins. The reserved
                    # request stays charged; unfinished token reservations fail closed.
