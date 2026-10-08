"""Explicit bounded operator probes; never Task execution or selection policy."""

import asyncio
import hashlib
import ipaddress
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import urlsplit

from agentforge.core.inference import (
    GenerationChunk,
    GenerationRequest,
    GenerationResult,
    Message,
    Provider,
    ToolDefinition,
)
from agentforge.core.provider_errors import InvalidProviderResponse, ProviderError
from agentforge.db.diagnostics import DiagnosticsRepository
from agentforge.workers.config import WorkersConfig
from agentforge.workers.diagnostics import (
    DiagnosticBusy,
    DiagnosticConfiguration,
    DiagnosticObservation,
    DiagnosticsPage,
    DiagnosticsUnavailable,
    DiagnosticWorkerNotFound,
    GenerationProbeKind,
    WorkerDiagnostics,
)

PROBE_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 16_384
MAX_CHUNKS = 256
_SAFE_FAILURES = {
    "backend_unavailable",
    "timeout",
    "invalid_response",
    "rejected",
}


def endpoint_class(endpoint):
    """Literal address scope only; arbitrary hostnames stay unknown, without DNS."""
    host = (urlsplit(str(endpoint)).hostname or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return "local"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_loopback:
        return "local"
    private = (
        ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        if address.version == 4
        else ("fc00::/7",)
    )
    if address.is_link_local or any(
        address in ipaddress.ip_network(net) for net in private
    ):
        return "private"
    if address.is_global and not address.is_multicast:
        return "remote"
    return "unknown"


class WorkerDiagnosticsService:
    """Injected Providers remain caller-owned. No probe runs during construction/read.

    One in-flight probe per Worker within this service; independent CLI processes
    can explicitly probe concurrently. Checkpoints are atomic, latest completion
    wins. No persistent lease, background work, Task or cross-process executor.
    """

    def __init__(
        self,
        config: WorkersConfig,
        providers: Mapping[str, Provider],
        repository: DiagnosticsRepository | None = None,
    ):
        self._workers = {w.id: w for w in config.workers}
        self._providers = providers
        self._repository = repository
        self._active: dict[str, asyncio.Task] = {}
        self._closed = False
        connections = {p.id: p for p in config.providers}
        self._configurations = {}
        self._fingerprints = {}
        for worker in config.workers:
            connection = connections.get(worker.provider_connection)
            endpoint = connection.base_url if connection else worker.endpoint
            self._configurations[worker.id] = DiagnosticConfiguration(
                worker_id=worker.id,
                provider=worker.provider,
                model=worker.model,
                deployment_label=worker.deployment_label,
                endpoint_class=endpoint_class(endpoint),
                context_window=worker.context_window,
                supports_tools=worker.supports_tools,
                supports_streaming=worker.supports_streaming,
            )
            # Invalidation only; no URL/options/credential names or values persisted.
            value = {
                "worker": worker.model_dump(mode="json"),
                "connection": connection.model_dump(mode="json")
                if connection
                else None,
            }
            self._fingerprints[worker.id] = hashlib.sha256(
                json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
            ).hexdigest()

    def _worker(self, worker_id):
        if worker_id not in self._workers:
            raise DiagnosticWorkerNotFound("Worker not configured")
        return self._workers[worker_id]

    def get(self, worker_id: str) -> WorkerDiagnostics:
        self._worker(worker_id)
        histories = (
            self._repository.histories(worker_id, self._fingerprints[worker_id])
            if self._repository
            else {}
        )
        return WorkerDiagnostics(
            configuration=self._configurations[worker_id], **histories
        )

    def list(self, *, limit: int = 25, offset: int = 0) -> DiagnosticsPage:
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 1_000_000
        ):
            raise ValueError("Invalid diagnostic query bounds")
        ids = sorted(self._workers)
        return DiagnosticsPage(
            workers=tuple(
                self.get(identity) for identity in ids[offset : offset + limit]
            ),
            next_offset=offset + limit if len(ids) > offset + limit else None,
        )

    async def close(self):
        self._closed = True
        tasks = set(self._active.values()) - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def check(self, worker_id: str) -> DiagnosticObservation:
        return await self._run(worker_id, "health")

    async def probe(
        self, worker_id: str, *, kind: GenerationProbeKind = "generation"
    ) -> DiagnosticObservation:
        if kind not in {"generation", "tools", "streaming"}:
            raise ValueError("Invalid probe kind")
        return await self._run(worker_id, kind)

    async def _run(self, worker_id, kind):
        worker = self._worker(worker_id)
        if self._closed:
            raise DiagnosticsUnavailable("Diagnostic service closed")
        if worker_id in self._active:
            raise DiagnosticBusy("Worker diagnostic already running")
        self._active[worker_id] = asyncio.current_task()
        try:
            values = {}
            if (
                kind == "tools"
                and worker.supports_tools is not True
                or kind == "streaming"
                and not worker.supports_streaming
            ):
                values["status"] = "not_applicable"
            else:
                provider = self._providers[
                    worker.provider_connection or worker.provider
                ]
                started = monotonic()
                timeout = min(
                    5.0 if kind == "health" else PROBE_TIMEOUT_SECONDS,
                    worker.timeout_seconds,
                )
                try:
                    async with asyncio.timeout(timeout):
                        if kind == "health":
                            health = await provider.health(worker)
                            if health.error_code == "not_probed":
                                values["status"] = "not_probed"
                            else:
                                values.update(
                                    status="available"
                                    if health.available
                                    else "failed",
                                    backend_available=health.backend_available,
                                    backend_reachable=health.backend_reachable
                                    if health.backend_reachable is not None
                                    else health.backend_available or None,
                                    model_available=health.model_available,
                                    error_code=None
                                    if health.available
                                    else "model_unavailable"
                                    if health.backend_available
                                    and health.model_available is False
                                    else health.error_code
                                    if health.error_code in _SAFE_FAILURES
                                    else "diagnostic_failed",
                                )
                        else:
                            await self._generation(
                                provider, worker, kind, timeout, started, values
                            )
                except TimeoutError:
                    values.update(status="failed", error_code="timeout")
                except ProviderError as error:
                    values.update(
                        status="failed",
                        error_code=error.code
                        if error.code in _SAFE_FAILURES
                        else "diagnostic_failed",
                    )
                except Exception:
                    values.update(status="failed", error_code="diagnostic_failed")
                # A portable health operation may deliberately make no request.
                if values["status"] != "not_probed":
                    values["request_duration_seconds"] = max(0.0, monotonic() - started)
                if kind != "health" and values["status"] == "failed":
                    values.setdefault("generation_success", False)
                    if kind == "tools":
                        values.setdefault("tool_call_success", False)
                    if kind == "streaming":
                        values.setdefault("streaming_success", False)
            observation = DiagnosticObservation(
                configuration=self._configurations[worker_id],
                probe_kind=kind,
                checked_at=datetime.now(UTC),
                **values,
            )
            if self._repository:
                self._repository.save(
                    observation, self._fingerprints[worker_id], tuple(self._workers)
                )
            return observation
        finally:
            self._active.pop(worker_id, None)

    async def _generation(self, provider, worker, kind, timeout, started, values):
        # Only synthetic messages/options. Configured stop strings or arbitrary
        # backend options cannot become diagnostic prompt data or evade its caps.
        target = worker.model_copy(update={"options": {}})
        request = GenerationRequest(
            messages=[
                Message(
                    role="user",
                    content="Call diagnostic_echo exactly once with value 'agentforge'."
                    if kind == "tools"
                    else "Reply with the word agentforge.",
                )
            ],
            temperature=0.0,
            timeout_seconds=timeout,
            max_output_tokens=64 if kind == "tools" else 32,
            tools=[
                ToolDefinition(
                    name="diagnostic_echo",
                    description="Synthetic diagnostic call; no external effects.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "value": {"type": "string", "enum": ["agentforge"]}
                        },
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                )
            ]
            if kind == "tools"
            else [],
        )
        if kind == "streaming":
            meaningful = False
            size = count = 0
            result = None
            async with provider.stream(target, request) as chunks:
                async for chunk in chunks:
                    if not isinstance(chunk, GenerationChunk):
                        raise InvalidProviderResponse("Invalid diagnostic chunk")
                    values["backend_reachable"] = True
                    size += self._size(chunk)
                    count += 1
                    if size > MAX_OUTPUT_BYTES or count > MAX_CHUNKS:
                        raise InvalidProviderResponse(
                            "Diagnostic output exceeded bounds"
                        )
                    if chunk.content and not meaningful:
                        values["ttft_seconds"] = max(0.0, monotonic() - started)
                        meaningful = True
                    if chunk.tool_calls:
                        raise InvalidProviderResponse("Unexpected diagnostic tool call")
                    if chunk.done:
                        result = chunk
                        break
            if result is None or not meaningful:
                values.update(status="failed", error_code="stream_failed")
                return
            values["streaming_success"] = True
        else:
            result = await provider.generate(target, request)
            if (
                not isinstance(result, GenerationResult)
                or self._size(result) > MAX_OUTPUT_BYTES
            ):
                raise InvalidProviderResponse("Invalid diagnostic result")
            if kind == "generation" and (
                not result.content.strip() or result.tool_calls
            ):
                raise InvalidProviderResponse("Missing diagnostic text")
        values.update(
            status="successful",
            backend_reachable=True,
            generation_success=True,
            token_usage=result.token_usage,
            generation_timing=result.generation_timing,
        )
        if kind == "tools":
            valid = (
                len(result.tool_calls) == 1
                and result.tool_calls[0].name == "diagnostic_echo"
                and result.tool_calls[0].arguments == {"value": "agentforge"}
            )
            values["tool_call_success"] = valid
            if not valid:
                values.update(status="failed", error_code="tool_call_failed")
        # Validate the native call only; there is no tool dispatcher or second turn.

    @staticmethod
    def _size(result):
        return (
            len(result.content.encode())
            + len((result.reasoning or "").encode())
            + sum(len(call.model_dump_json().encode()) for call in result.tool_calls)
        )
