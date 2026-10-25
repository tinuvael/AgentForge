"""Actual Provider-call denial, scoped MCP evidence and durable resource policy."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agentforge.agents.models import Agent
from agentforge.application.access import ClientApplication, ClientPolicy, ProjectGrant
from agentforge.application.service import Application
from agentforge.core.inference import GenerationResult, TokenUsage
from agentforge.core.policy import (
    EgressPolicy,
    PolicyDenied,
    ResourceBudget,
    TaskPolicy,
)
from agentforge.core.provider_errors import BackendUnavailable, ProviderRejected
from agentforge.core.worker import Worker, WorkerHealth
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.resources import ResourceRepository
from agentforge.mcp.server import create_server
from agentforge.providers.http import check_status
from agentforge.workers.config import ProviderConnection, WorkersConfig
from tests.test_mcp import call, error, run


class Scripted:
    name = "openai_compatible"

    def __init__(self):
        self.calls = []
        self.error = None
        self.usage = TokenUsage(total_tokens=5)
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.available = True

    async def generate(self, worker, request):
        self.calls.append((worker.id, request))
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.error:
            raise self.error
        return GenerationResult(
            content="answer", model="untrusted-model", token_usage=self.usage
        )

    async def health(self, worker):
        return WorkerHealth(
            backend_available=self.available, model_available=self.available
        )


@pytest.fixture
def policy_setup(database, registry, tmp_path):
    root = tmp_path / "policy-project"
    root.mkdir()
    (root / "private.txt").write_text("PRIVATE_PROJECT_DATA")
    project = registry.register_project("Local engineering", root)
    providers = [
        ProviderConnection(
            id="local",
            type="openai_compatible",
            base_url="http://192.168.1.20/v1",
            trust="local",
        ),
        ProviderConnection(
            id="cloud-a",
            type="openai_compatible",
            base_url="https://a.invalid/v1",
            trust="cloud",
        ),
        ProviderConnection(
            id="cloud-b",
            type="openai_compatible",
            base_url="https://b.invalid/v1",
            trust="cloud",
        ),
        ProviderConnection(
            id="unclassified",
            type="openai_compatible",
            base_url="http://local-looking.invalid/v1",
        ),
    ]
    workers = [
        Worker(
            id=p.id,
            provider=p.type,
            provider_connection=p.id,
            model="placeholder",
            supports_tools=True,
            context_window=32768,
            capabilities={"coding": True},
        )
        for p in providers
    ]
    scripted = {p.id: Scripted() for p in providers}
    config = WorkersConfig(providers=providers, workers=workers)

    def build(config=config):
        return Application(
            create_database_engine(database[1]),
            config,
            agents=[
                Agent(
                    id="text",
                    name="Text",
                    description="Text only",
                    system_prompt="Answer",
                )
            ],
            providers=scripted,
            concurrency=4,
        )

    app = build()
    binding = dict(
        project_id=project.id,
        agent_id="text",
        worker_id="local",
        task="PRIVATE_PROJECT_DATA",
    )
    return SimpleNamespace(
        app=app,
        project=project,
        registry=registry,
        config=config,
        providers=scripted,
        binding=binding,
        build=build,
        database=database,
    )


def cloud_authorize(s, *connections):
    return s.registry.set_egress_policy(
        s.project.id,
        EgressPolicy(mode="cloud_allowed", allowed_connections=connections),
    )


def test_local_only_denies_all_cloud_and_council_before_io(policy_setup):
    s = policy_setup

    async def execute():
        await s.app.start()
        try:
            for identity in ("cloud-a", "cloud-b", "unclassified"):
                with pytest.raises(PolicyDenied):
                    s.app.delegate_task(**(s.binding | {"worker_id": identity}))
            with pytest.raises(PolicyDenied):
                s.app.delegate_council(
                    project_id=s.project.id,
                    agent_id="text",
                    worker_ids=("local", "cloud-a"),
                    task="private",
                )
            assert s.app.tasks.list_tasks() == []
            assert all(not p.calls for p in s.providers.values())
            task = s.app.delegate_task(**s.binding)
            done = await s.app.tasks.wait_task(task.task_id)
            assert done.state == "completed"
            assert done.provider_connection == "local" and done.model == "placeholder"
            assert done.worker_id == "local"
        finally:
            await s.app.close()

    run(execute())


def test_outage_no_fallback_or_probe_storm(policy_setup):
    s = policy_setup
    s.providers["local"].error = BackendUnavailable("SECRET_BACKEND_BODY")

    async def execute():
        await s.app.start()
        try:
            task = s.app.delegate_task(**s.binding)
            done = await s.app.tasks.wait_task(task.task_id)
            assert done.error_code == "provider_error" and done.worker_id == "local"
            assert len(s.providers["local"].calls) == 1
            assert not s.providers["cloud-a"].calls and not s.providers["cloud-b"].calls
            for _ in range(10):
                suggested = s.app.suggest_workers(
                    project_id=s.project.id, agent_id="text"
                )
                local = next(
                    c for c in suggested.candidates if c.worker.worker_id == "local"
                )
                assert (
                    not local.eligible and local.availability == "observed_unavailable"
                )
                assert local.last_failure == "backend_unavailable"
                assert "SECRET" not in suggested.model_dump_json()
            assert len(s.providers["local"].calls) == 1
        finally:
            await s.app.close()

    run(execute())


def test_allowlist_and_task_cannot_expand_even_for_local_target(policy_setup):
    s = policy_setup
    cloud_authorize(s, "cloud-a")

    async def execute():
        await s.app.start()
        try:
            for restriction in (
                EgressPolicy(),
                EgressPolicy(mode="cloud_allowed", allowed_connections=("cloud-b",)),
            ):
                with pytest.raises(PolicyDenied):
                    s.app.delegate_task(
                        **(
                            s.binding
                            | {
                                "worker_id": "cloud-a",
                                "task_policy": TaskPolicy(egress=restriction),
                            }
                        )
                    )
            with pytest.raises(PolicyDenied):
                s.app.delegate_task(**(s.binding | {"worker_id": "cloud-b"}))
            task = s.app.delegate_task(**(s.binding | {"worker_id": "cloud-a"}))
            done = await s.app.tasks.wait_task(task.task_id)
            assert done.state == "completed" and done.provider_connection == "cloud-a"
            assert (
                len(s.providers["cloud-a"].calls) == 1
                and not s.providers["cloud-b"].calls
            )
        finally:
            await s.app.close()

    run(execute())


def test_mcp_and_gateway_scope_before_metadata_or_calls(policy_setup):
    s = policy_setup
    cloud_authorize(s, "cloud-a")
    client = ClientPolicy(
        client_id="limited",
        token_sha256="a" * 64,
        tools=("suggest_workers", "list_workers", "delegate_task"),
        agents=("text",),
        projects=(ProjectGrant(project_id=s.project.id, workers=("local",)),),
    )
    scoped = ClientApplication(
        s.app, lambda: client, SimpleNamespace(max_pending_tasks=10)
    )

    async def execute():
        nonlocal client
        await s.app.start()
        try:
            server = create_server(
                lambda: s.app, borrowed=True, client_access=lambda app: scoped
            )
            async with create_connected_server_and_client_session(server) as session:
                discovery = {t.name for t in (await session.list_tools()).tools}
                assert discovery == set(client.tools)
                values = await call(
                    session,
                    "suggest_workers",
                    {"project_id": str(s.project.id), "agent_id": "text"},
                )
                assert [c["worker"]["worker_id"] for c in values["candidates"]] == [
                    "local"
                ]
                assert "cloud-a" not in str(values) and "private.txt" not in str(values)
                assert values["candidates"][0]["requests_used"] is None
                assert values["candidates"][0]["tokens_used"] is None
                await error(
                    session,
                    "suggest_workers",
                    {"project_id": str(uuid4()), "agent_id": "text"},
                    "access_denied",
                )
                await error(
                    session,
                    "delegate_task",
                    s.binding
                    | {"project_id": str(s.project.id), "worker_id": "cloud-a"},
                    "access_denied",
                )
                # Client grant cannot override Project privacy even if it names cloud.
                client = client.model_copy(
                    update={
                        "projects": (
                            ProjectGrant(project_id=s.project.id, workers=("cloud-a",)),
                        )
                    }
                )
                s.registry.set_egress_policy(s.project.id, EgressPolicy())
                await error(
                    session,
                    "delegate_task",
                    s.binding
                    | {"project_id": str(s.project.id), "worker_id": "cloud-a"},
                    "policy_denied",
                )
                assert all(not p.calls for p in s.providers.values())
        finally:
            await s.app.close()

    run(execute())


def test_declared_unknown_synthetic_conflicts_and_health(policy_setup):
    s = policy_setup

    async def execute():
        await s.app.start()
        try:
            initial = s.app.suggest_workers(
                project_id=s.project.id,
                agent_id="text",
                required=("coding",),
                preferred=("reasoning",),
            )
            local = next(c for c in initial.candidates if c.worker.worker_id == "local")
            assert local.eligible and local.availability == "unknown"
            assert local.worker.capabilities["coding"].declaration.source == "declared"
            assert local.worker.capabilities["vision"].support == "unknown"
            # Scripted response lacks the required synthetic tool call.
            await s.app.worker_diagnostics.probe("local", kind="tools")
            suggested = s.app.suggest_workers(
                project_id=s.project.id, agent_id="text", require_tools=True
            )
            local = next(
                c for c in suggested.candidates if c.worker.worker_id == "local"
            )
            evidence = local.worker.capabilities["tool_calling"]
            assert (
                evidence.support == "unknown"
                and evidence.declaration.support == "supported"
            )
            assert evidence.observations[0].source == "measured"
            assert evidence.observations[0].provenance == "synthetic_tool_probe"
            assert not local.eligible
            s.providers["local"].available = False
            await s.app.worker_diagnostics.check("local")
            local = next(
                c
                for c in s.app.suggest_workers(
                    project_id=s.project.id, agent_id="text"
                ).candidates
                if c.worker.worker_id == "local"
            )
            assert local.availability == "observed_unavailable" and not local.eligible
        finally:
            await s.app.close()

    run(execute())


@pytest.mark.parametrize(
    "header,seconds",
    [("10", 10), (None, None), ("bogus", None), ("999999999", None), ("-1", None)],
)
def test_observed_429_cooldown_persists_without_retry(policy_setup, header, seconds):
    s = policy_setup
    headers = {} if header is None else {"Retry-After": header}
    with pytest.raises(ProviderRejected) as error:
        check_status(httpx.Response(429, headers=headers))
    s.providers["local"].error = error.value

    async def execute():
        await s.app.start()
        task = s.app.delegate_task(**s.binding)
        done = await s.app.tasks.wait_task(task.task_id)
        assert done.error_code == "provider_error"
        row = s.app.worker_policy.resources.get("local")
        assert row.last_error == "rate_limited" and row.requests_used == 1
        assert (row.cooldown_until is not None) == (seconds is not None)
        if seconds:
            with pytest.raises(PolicyDenied, match="cooldown"):
                s.app.delegate_task(**s.binding)
        await s.app.close()
        app = s.build()
        try:
            row = app.worker_policy.resources.get("local")
            assert row.last_error == "rate_limited"
            if seconds:
                with pytest.raises(PolicyDenied, match="cooldown"):
                    app.tasks.submit(**s.binding)
        finally:
            await app.close()
        assert len(s.providers["local"].calls) == 1

    run(execute())


def test_atomic_request_budget_and_token_reservation(database):
    repository = ResourceRepository(create_session_factory(database[0]))
    budget = ResourceBudget(max_requests=1)

    def admit(_):
        try:
            repository.admit("shared", budget)
            return True
        except PolicyDenied:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(admit, range(2))) == [False, True]
    assert repository.get("shared").requests_used == 1
    repository.admit("tokens", ResourceBudget(max_tokens=10))
    with pytest.raises(PolicyDenied, match="token_budget_in_flight"):
        repository.admit("tokens", ResourceBudget(max_tokens=10))
    repository.finish("tokens", tokens=10, error_code=None, retry_after=None)
    with pytest.raises(PolicyDenied, match="token_budget"):
        repository.admit("tokens", ResourceBudget(max_tokens=10))


@pytest.mark.parametrize(
    "usage,expected",
    [(TokenUsage(total_tokens=5), "token_budget"), (None, "token_usage_unknown")],
)
def test_persistent_token_budget_uses_only_reported_counts(
    policy_setup, usage, expected
):
    s = policy_setup
    s.providers["local"].usage = usage
    config = s.config.model_copy(
        update={
            "providers": [
                p.model_copy(update={"budget": ResourceBudget(max_tokens=5)})
                if p.id == "local"
                else p
                for p in s.config.providers
            ]
        }
    )

    async def execute():
        await s.app.close()
        app = s.build(config)
        await app.start()
        try:
            task = app.delegate_task(**s.binding)
            done = await app.tasks.wait_task(task.task_id)
            assert done.state == "completed"
            assert s.providers["local"].calls[0][1].max_output_tokens == 5
            with pytest.raises(PolicyDenied, match=expected):
                app.delegate_task(**s.binding)
            assert len(s.providers["local"].calls) == 1
        finally:
            await app.close()

    run(execute())


def test_queued_binding_and_task_restrictions_survive_restart(policy_setup):
    s = policy_setup
    restriction = TaskPolicy(egress=EgressPolicy(), max_requests=1)
    queued = s.app.tasks.submit(**s.binding, task_policy=restriction)
    assert queued.task_policy == restriction

    async def execute():
        await s.app.close()
        # Same Worker ID with a different model cannot inherit the queued request.
        config = s.config.model_copy(
            update={
                "workers": [
                    w.model_copy(update={"model": "replacement"})
                    if w.id == "local"
                    else w
                    for w in s.config.workers
                ]
            }
        )
        app = s.build(config)
        await app.start()
        try:
            done = await app.tasks.wait_task(queued.task_id)
            assert done.reason == "invalid_configuration"
            assert done.worker_id == "local" and done.model == "placeholder"
            assert (
                done.provider_connection == "local" and done.task_policy == restriction
            )
            assert all(not p.calls for p in s.providers.values())
        finally:
            await app.close()

    run(execute())


def test_revoked_project_authorization_checked_again_before_execution(policy_setup):
    s = policy_setup
    cloud_authorize(s, "cloud-a")
    queued = s.app.tasks.submit(**(s.binding | {"worker_id": "cloud-a"}))
    s.registry.set_egress_policy(s.project.id, EgressPolicy())

    async def execute():
        await s.app.start()
        try:
            task = await s.app.tasks.wait_task(queued.task_id)
            assert (
                task.reason == "policy_denied" and task.provider_connection == "cloud-a"
            )
            assert not s.providers["cloud-a"].calls
        finally:
            await s.app.close()

    run(execute())


def test_concurrent_tasks_share_connection_request_budget(policy_setup):
    s = policy_setup
    s.providers["local"].block = True
    config = s.config.model_copy(
        update={
            "providers": [
                p.model_copy(update={"budget": ResourceBudget(max_requests=1)})
                if p.id == "local"
                else p
                for p in s.config.providers
            ]
        }
    )

    async def execute():
        await s.app.close()
        app = s.build(config)
        await app.start()
        try:
            first = app.delegate_task(**s.binding)
            second = app.delegate_task(**s.binding)
            await s.providers["local"].entered.wait()
            denied = await app.tasks.wait_task(second.task_id)
            assert denied.reason == "policy_denied"
            s.providers["local"].release.set()
            assert (await app.tasks.wait_task(first.task_id)).state == "completed"
            assert len(s.providers["local"].calls) == 1
        finally:
            await app.close()

    run(execute())


@pytest.mark.parametrize(
    "budget", [TaskPolicy(max_requests=1), TaskPolicy(max_tokens=5)]
)
def test_task_budget_stops_next_turn_before_provider_io(policy_setup, budget):
    from agentforge.core.inference import ToolCall

    s = policy_setup
    provider = s.providers["local"]
    original = provider.generate

    async def tool_turn(worker, request):
        await original(worker, request)
        return GenerationResult(
            content="",
            model="untrusted",
            token_usage=TokenUsage(total_tokens=5),
            tool_calls=[
                ToolCall(id="read", name="read_file", arguments={"path": "private.txt"})
            ],
        )

    provider.generate = tool_turn

    async def execute():
        await s.app.close()
        app = Application(
            create_database_engine(s.database[1]),
            s.config,
            agents=[
                Agent(
                    id="text",
                    name="Tools",
                    description="Read",
                    system_prompt="Read",
                    allowed_tools=("read_file",),
                )
            ],
            providers=s.providers,
        )
        await app.start()
        try:
            task = app.delegate_task(**s.binding, task_policy=budget)
            done = await app.tasks.wait_task(task.task_id)
            assert done.reason == "policy_denied"
            assert (
                len(provider.calls) == 1 and done.execution_result.tool_call_count == 1
            )
            assert not s.providers["cloud-a"].calls
        finally:
            await app.close()

    run(execute())


def test_synthetic_positive_is_scoped_and_multimodal_transport_is_excluded(
    policy_setup,
):
    from agentforge.core.inference import ToolCall

    s = policy_setup
    provider = s.providers["local"]

    async def synthetic(worker, request):
        provider.calls.append((worker.id, request))
        return GenerationResult(
            content="",
            model="untrusted",
            tool_calls=[
                ToolCall(
                    id="probe",
                    name="diagnostic_echo",
                    arguments={"value": "agentforge"},
                )
            ],
        )

    provider.generate = synthetic

    async def execute():
        try:
            await s.app.worker_diagnostics.probe("local", kind="tools")
            result = s.app.suggest_workers(
                project_id=s.project.id, agent_id="text", required=("vision",)
            )
            local = next(c for c in result.candidates if c.worker.worker_id == "local")
            descriptor = local.worker.capabilities["tool_calling"]
            assert descriptor.declaration.source == "declared"
            assert descriptor.observations[0].source == "measured"
            assert "synthetic" in descriptor.observations[0].provenance
            assert local.worker.capabilities["coding"].observations == ()
            assert "transport_not_implemented_vision" in local.hard_exclusions
            assert len(provider.calls) == 1
        finally:
            await s.app.close()

    run(execute())


def test_operator_probe_cannot_bypass_connection_budget(policy_setup):
    s = policy_setup
    config = s.config.model_copy(
        update={
            "providers": [
                p.model_copy(update={"budget": ResourceBudget(max_requests=0)})
                if p.id == "local"
                else p
                for p in s.config.providers
            ]
        }
    )

    async def execute():
        await s.app.close()
        app = s.build(config)
        try:
            with pytest.raises(PolicyDenied, match="request_budget"):
                await app.worker_diagnostics.probe("local")
            assert not s.providers["local"].calls
        finally:
            await app.close()

    run(execute())


def test_legacy_worker_config_and_conflicting_declaration_validation(
    tmp_path, policy_setup
):
    from pydantic import ValidationError

    from agentforge.core.capabilities import describe
    from agentforge.workers.config import load_workers

    path = tmp_path / "legacy.toml"
    path.write_text(
        '[[workers]]\nid="unchanged-id"\nprovider="ollama"\nmodel="placeholder"\nendpoint="http://127.0.0.1:11434"\n'
    )
    config = load_workers(path)
    worker = config.workers[0]
    assert worker.id == "unchanged-id" and worker.supports_tools is None
    assert describe(worker)["coding"].support == "unknown"
    assert policy_setup.app.worker_policy.trust(worker) == "local"
    with pytest.raises(ValidationError):
        Worker(
            id="conflict",
            provider="ollama",
            model="placeholder",
            endpoint="http://localhost",
            supports_tools=False,
            capabilities={"tool_calling": True},
        )
    named = ProviderConnection(
        id="inline:reserved", type="ollama", base_url="http://localhost"
    )
    assert (
        WorkersConfig(providers=[named], workers=[]).providers[0].id
        == "inline:reserved"
    )
    with pytest.raises(ValidationError):
        WorkersConfig(
            providers=[named], workers=[worker.model_copy(update={"id": "reserved"})]
        )
    run(policy_setup.app.close())


def test_probe_respects_atomic_remaining_token_cap(policy_setup):
    s = policy_setup
    config = s.config.model_copy(
        update={
            "providers": [
                p.model_copy(update={"budget": ResourceBudget(max_tokens=3)})
                if p.id == "local"
                else p
                for p in s.config.providers
            ]
        }
    )

    async def execute():
        await s.app.close()
        app = s.build(config)
        try:
            await app.worker_diagnostics.probe("local")
            assert s.providers["local"].calls[0][1].max_output_tokens == 3
            with pytest.raises(PolicyDenied, match="token_budget"):
                await app.worker_diagnostics.probe("local")
            assert len(s.providers["local"].calls) == 1
        finally:
            await app.close()

    run(execute())
