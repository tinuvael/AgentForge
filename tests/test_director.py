"""Shared Director core through actual SDK sessions and scoped Application calls."""

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from types import SimpleNamespace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agentforge.application.access import ClientApplication, ClientPolicy, ProjectGrant
from agentforge.core.inference import GenerationResult, ToolCall
from agentforge.core.provider_errors import BackendUnavailable, ProviderRejected
from agentforge.director.client import DirectorError
from agentforge.director.config import DirectorConfig
from agentforge.director.core import Director
from agentforge.director.journal import Journal
from agentforge.gateway.server import GatewayConfig
from agentforge.mcp.server import TOOL_CONTRACTS, create_server
from tests.test_mcp import PRIVATE, ScriptedProvider
from tests.test_mcp import setup as setup_fixture

setup = setup_fixture


def config(root, **limits):
    return DirectorConfig(
        gateway_url="http://127.0.0.1:8766/mcp",
        token_env="TEST_DIRECTOR_TOKEN",
        journal=root,
        model="scripted-director",
        inference={
            "id": "director",
            "type": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "trust": "local",
        },
        limits=limits,
    )


def tool(name, arguments):
    return GenerationResult(
        content="",
        model="scripted-director",
        tool_calls=[ToolCall(id="call", name=name, arguments=arguments)],
    )


@asynccontextmanager
async def fixture(setup, tmp_path, *, limits=None, turns=None, agents=None):
    principal = ClientPolicy(
        client_id="local-director",
        token_sha256="a" * 64,
        tools=tuple(t.name for t in TOOL_CONTRACTS),
        projects=(
            ProjectGrant(project_id=setup.project.id, workers=("home-i5", "cloud")),
        ),
        agents=("repo_explorer", "general_agent", "coder"),
        coding_submit=True,
    )
    if agents:
        setup.app = setup.build(agents=agents)
    access = ClientApplication(setup.app, lambda: principal, GatewayConfig())
    server = create_server(
        lambda: setup.app, borrowed=True, client_access=lambda _: access
    )

    @asynccontextmanager
    async def connector(_):
        async with create_connected_server_and_client_session(server) as client:
            yield client

    provider = ScriptedProvider()
    provider.turns = list(
        turns
        or [
            tool("delegate_task", setup.binding),
            GenerationResult(
                content="source.py:1 via returned Task evidence.",
                model="scripted-director",
                reasoning=PRIVATE,
            ),
        ]
    )
    setup.provider.turns = [
        tool("read_file", {"path": "source.py"}),
        GenerationResult(
            content="source.py:1 contains a function.", model="configured-model"
        ),
    ]
    await setup.app.start()
    try:
        async with Director(
            config(tmp_path / "journal", **(limits or {})),
            provider=provider,
            connector=connector,
        ) as director:
            yield SimpleNamespace(
                director=director, provider=provider, access=access, connector=connector
            )
    finally:
        await setup.app.close()


def test_delegate_verify_and_recover_without_duplicate(setup, tmp_path):
    async def scenario():
        async with fixture(setup, tmp_path) as f:
            item = f.director.create("cli")
            item = await f.director.run(item.id, "cli", "Inspect source.")
            assert item.state == "completed", item.error
            assert len(item.tasks) == 1
            assert item.client_id == "local-director"
            task = item.tasks[0]
            assert task.worker_id == "home-i5" and task.project_id == setup.project.id
            assert task.evidence == "source_tools_observed"
            assert setup.app.tasks.get_task(task.task_id).client_id == "local-director"
            stored = f.director.journal.path(item.id).read_text()
            assert PRIVATE not in stored and "token_sha256" not in stored
            f.provider.turns = [
                GenerationResult(
                    content="Recovered existing task.", model="scripted-director"
                )
            ]
            recovered = await f.director.run(item.id, "cli", resume=True)
            assert recovered.state == "completed" and len(recovered.tasks) == 1
            assert len(setup.app.tasks.history()) == 1
            assert setup.app.tasks.observer.subscriber_count == 0
            with pytest.raises(ValueError, match="Conversation unavailable"):
                f.director.load(item.id, "other-browser")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "turn,limits,expected",
    [
        (tool("shell", {"command": "unsafe"}), {}, "unknown_tool"),
        (tool("delegate_task", {"project_id": "bad"}), {}, "invalid_arguments"),
        (BackendUnavailable("secret endpoint"), {}, "backend_unavailable"),
        (ProviderRejected("secret", status_code=429), {}, "rate_limited"),
        (tool("list_workers", {}), {"turns": 1}, "turn_limit"),
        (tool("list_workers", {}), {"calls": 4}, "tool_limit"),
        (GenerationResult(content="x" * 9000, model="fake"), {}, "output_limit"),
    ],
)
def test_failures_are_bounded_and_safe(setup, tmp_path, turn, limits, expected):
    async def scenario():
        async with fixture(setup, tmp_path, limits=limits, turns=[turn]) as f:
            item = f.director.create("cli")
            item = await f.director.run(item.id, "cli", "Inspect.")
            assert item.state == "failed" and item.error == expected
            assert not item.tasks and not setup.app.tasks.history()
            assert "secret" not in f.director.journal.path(item.id).read_text()

    asyncio.run(scenario())


def test_coder_approval_is_precise_expiring_and_one_use(setup, tmp_path):
    from agentforge.application.definitions import shipped_agents

    async def scenario():
        binding = setup.binding | {"agent_id": "coder"}
        async with fixture(
            setup,
            tmp_path,
            turns=[tool("delegate_task", binding)],
            agents=shipped_agents(coding_enabled=True),
        ) as f:
            item = f.director.create("cli")
            item = await f.director.run(item.id, "cli", "Fix source.")
            assert item.state == "approval", item.error
            assert item.pending.arguments["worker_id"] == "home-i5"
            assert item.pending.project_id == str(setup.project.id)
            assert not setup.app.tasks.history()
            nonce = item.pending.nonce
            with pytest.raises(DirectorError, match="approval_invalid"):
                await f.director.run(item.id, "cli", nonce="wrong", approve=True)
            item.pending.expires = time() - 1
            f.director.journal.save(item)
            with pytest.raises(DirectorError, match="approval_invalid"):
                await f.director.run(item.id, "cli", nonce=nonce, approve=True)
            item.pending.expires = time() + 30
            f.director.journal.save(item)
            rejected = await f.director.run(item.id, "cli", nonce=nonce, approve=False)
            assert rejected.state == "cancelled" and rejected.pending is None
            with pytest.raises(DirectorError, match="approval_invalid"):
                await f.director.run(item.id, "cli", nonce=nonce, approve=True)
            assert not setup.app.tasks.history()

    asyncio.run(scenario())


def test_lost_submission_response_is_never_retried(setup, tmp_path):
    async def scenario():
        async with fixture(setup, tmp_path) as f:
            original = f.director.connector
            submissions = []

            @asynccontextmanager
            async def losing(config):
                async with original(config) as client:
                    call = client.call_tool

                    async def lost(name, arguments):
                        result = await call(name, arguments)
                        if name == "delegate_task":
                            submissions.append(result.structuredContent["task_id"])
                            raise ConnectionError("private network failure")
                        return result

                    client.call_tool = lost
                    yield client

            f.director.connector = losing
            item = f.director.create("cli")
            item = await f.director.run(item.id, "cli", "Inspect.")
            assert item.state == "ambiguous" and len(submissions) == 1
            with pytest.raises(DirectorError, match="submission_ambiguous"):
                await f.director.run(item.id, "cli", resume=True)
            assert len(setup.app.tasks.history()) == 1

    asyncio.run(scenario())


def test_journal_private_bounds_owner_and_restart(tmp_path):
    journal = Journal(tmp_path / "private", limit=1)
    journal.open()
    try:
        item = journal.create("browser", "gateway")
        with pytest.raises(ValueError, match="capacity"):
            journal.create("browser", "gateway")
        with pytest.raises(ValueError, match="owned"):
            Journal(journal.root).open()
        with pytest.raises(ValueError):
            journal.read("../../escape", "browser", "gateway")
        with pytest.raises(ValueError):
            journal.read(item.id, "browser", "another-gateway")
        if os.name != "nt":
            assert journal.path(item.id).stat().st_mode & 0o077 == 0
    finally:
        journal.close()
    journal.open()
    assert journal.read(item.id, "browser", "gateway").id == item.id
    journal.close()


def test_real_authenticated_directors_acceptance():
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/verify_director.py"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Two authenticated Directors; one executor" in result.stdout
    assert "Traceback" not in result.stderr


def test_policy_denial_and_explicit_selection(setup, tmp_path):
    from agentforge.core.policy import EgressPolicy

    setup.registry.set_egress_policy(setup.project.id, EgressPolicy())

    async def scenario():
        async with fixture(
            setup,
            tmp_path,
            turns=[tool("delegate_task", setup.binding | {"worker_id": "cloud"})],
        ) as f:
            item = f.director.create("cli")
            item = await f.director.run(item.id, "cli", "Inspect.")
            assert item.error == "policy_denied"
            assert not setup.app.tasks.history()
            item = f.director.create("browser")
            item.worker_id = "local-4080"
            f.director.journal.save(item)
            f.provider.turns = [tool("delegate_task", setup.binding)]
            item = await f.director.run(item.id, "browser", "Inspect.")
            assert item.error == "binding_denied"

    asyncio.run(scenario())


def test_cancellation_timeout_and_bounded_active_requests(setup, tmp_path):
    async def scenario():
        async with fixture(setup, tmp_path, limits={"active": 1, "seconds": 1}) as f:
            f.provider.release.clear()
            item = f.director.create("cli")
            running = asyncio.create_task(f.director.run(item.id, "cli", "Inspect."))
            await f.provider.entered.wait()
            other = f.director.create("other")
            with pytest.raises(DirectorError, match="director_busy"):
                await f.director.run(other.id, "other", "Inspect.")
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert f.director.load(item.id, "cli").state == "cancelled"
            assert not f.director.active and f.provider.cleaned == 1
            timed = await f.director.run(other.id, "other", "Inspect.")
            assert timed.error == "timeout" and not f.director.active

    asyncio.run(scenario())


def test_reconnect_read_and_reject_recursive_recovery(setup, tmp_path):
    async def scenario():
        async with fixture(setup, tmp_path) as f:
            original = f.director.connector
            attempts = []

            @asynccontextmanager
            async def flaky(config):
                if not attempts:
                    attempts.append(True)
                    raise ConnectionError("private")
                async with original(config) as client:
                    yield client

            f.director.connector = flaky
            item = f.director.create("cli")
            item = await f.director.run(item.id, "cli", "Inspect.")
            assert item.state == "completed" and attempts
            f.provider.turns = [tool("delegate_task", setup.binding)]
            item = await f.director.run(item.id, "cli", resume=True)
            assert item.error == "recovery_read_only"
            assert len(setup.app.tasks.history()) == 1

    asyncio.run(scenario())
