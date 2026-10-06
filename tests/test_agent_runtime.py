"""Deterministic in-process runtime tests; never require network, Ollama or GPU."""

import asyncio
import json
import shutil
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.agents import (
    REPO_EXPLORER,
    Agent,
    AgentRuntime,
    CancellationToken,
    RuntimeLimits,
    repository_toolset,
)
from agentforge.agents.tools import json_text
from agentforge.core.inference import GenerationResult, TokenUsage, ToolCall
from agentforge.core.provider_errors import BackendUnavailable, ProviderTimeout
from agentforge.core.worker import Worker
from agentforge.db.database import create_session_factory
from agentforge.db.index import IndexRepository
from agentforge.index.models import IndexStorageError
from agentforge.index.service import ProjectIndex
from agentforge.tools.errors import GitFailure
from agentforge.tools.service import RepositoryTools
from agentforge.workers.config import WorkersConfig


class ScriptedProvider:
    name = "fake"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.workers = []

    async def health(self, worker):
        pytest.fail("Runtime must not probe/rank/route Workers")

    def stream(self, worker, request):
        pytest.fail("Runtime uses generate(), never streaming")

    async def generate(self, worker, request):
        self.requests.append(request.model_copy(deep=True))
        self.workers.append(worker)
        assert self.turns, "Unexpected extra model turn"
        turn = self.turns.pop(0)
        if callable(turn):
            turn = turn()
        if isinstance(turn, Exception):
            raise turn
        return turn


class Clock:
    value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def answer(content="Evidence: source.py:1-4.", usage=None):
    return GenerationResult(content=content, model="fake-model", token_usage=usage)


def turn(*calls, content=""):
    return GenerationResult(content=content, model="fake-model", tool_calls=list(calls))


def call(name="read_file", arguments=None, identity="call-1"):
    return ToolCall(
        id=identity, name=name, arguments=arguments or {"path": "source.py"}
    )


@pytest.fixture
def setup(registry, database, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "source.py").write_text(
        "def helper():\n    pass\ndef run():\n    helper()\n"
    )
    (root / ".env").write_text("DENIED FILE SECRET")
    (root / "binary").write_bytes(b"SECRET\0binary")
    project = registry.register_project("Synthetic", root)
    index = ProjectIndex(registry, IndexRepository(create_session_factory(database[0])))
    index.refresh_index(project.id)
    repository = RepositoryTools(registry)
    tools = repository_toolset(index, repository)
    worker = Worker(
        id="explicit",
        provider="fake",
        model="fake-model",
        endpoint="http://never.invalid",
        supports_tools=True,
    )
    other = worker.model_copy(update={"id": "other", "model": "other-model"})
    clock = Clock()

    def build(turns, agent=REPO_EXPLORER, worker_config=None, tools_config=None):
        provider = ScriptedProvider(turns)
        runtime = AgentRuntime(
            projects=registry,
            workers=WorkersConfig(workers=worker_config or [worker, other]),
            agents=[agent],
            providers={"fake": provider},
            tools=tools if tools_config is None else tools_config,
            clock=clock,
        )
        return runtime, provider

    def run(runtime, **options):
        arguments = dict(
            project_id=project.id,
            worker_id=worker.id,
            agent_id="repo_explorer",
            task="Inspect evidence.",
        )
        arguments.update(options)
        return asyncio.run(runtime.run(**arguments))

    return SimpleNamespace(
        root=root,
        project=project,
        index=index,
        repository=repository,
        registry=registry,
        tools=tools,
        worker=worker,
        other=other,
        clock=clock,
        build=build,
        run=run,
    )


def tool_messages(provider):
    return [
        message for message in provider.requests[-1].messages if message.role == "tool"
    ]


def test_agent_behavior_is_separate_and_worker_must_be_explicit(setup):
    assert "model" not in Agent.model_fields and "worker_id" not in Agent.model_fields
    assert "system_prompt" not in Worker.model_fields
    runtime, provider = setup.build([answer()])
    with pytest.raises(TypeError):
        asyncio.run(
            runtime.run(
                project_id=setup.project.id, agent_id="repo_explorer", task="explore"
            )
        )
    result = setup.run(runtime, worker_id="other")
    assert provider.workers == [setup.other]
    assert result.worker_id == "other"
    assert result.agent_id == "repo_explorer"
    assert result.project_id == setup.project.id
    assert result.state == "completed" and result.reason == "completed"
    assert result.steps == 1 and result.tool_call_count == 0
    assert result.final_answer == "Evidence: source.py:1-4."


@pytest.mark.parametrize(
    "options",
    [
        {"worker_id": "missing"},
        {"agent_id": "missing"},
        {"project_id": uuid4()},
        {"project_id": "invalid UUID"},
        {"task": " "},
    ],
)
def test_invalid_binding_does_not_route_or_call_model(setup, options):
    runtime, provider = setup.build([answer()])
    assert setup.run(runtime, **options).reason == "invalid_configuration"
    assert not provider.requests


def test_generic_agent_without_repository_behavior(setup):
    agent = Agent(
        id="general", name="General", description="Text only", system_prompt="Be brief."
    )
    runtime, provider = setup.build([answer("hello")], agent=agent)
    result = setup.run(runtime, agent_id="general")
    assert result.final_answer == "hello"
    assert provider.requests[0].tools == []
    assert provider.requests[0].messages[0].content == "Be brief."


def test_one_tool_then_answer_preserves_id_and_project_binding(setup):
    runtime, provider = setup.build([turn(call(identity="arbitrary-id")), answer()])
    result = setup.run(runtime)
    assert result.reason == "completed" and result.tool_call_count == 1
    messages = provider.requests[1].messages
    assert messages[-2].tool_calls[0].id == "arbitrary-id"
    assert (
        messages[-1].tool_call_id == "arbitrary-id"
        and messages[-1].tool_name == "read_file"
    )
    body = json.loads(messages[-1].content)
    assert body["ok"] and "def helper" in body["result"]["content"]
    assert body["result"]["start_line"] == 1 and body["result"]["end_line"] == 4
    assert result.tool_output_bytes == len(messages[-1].content.encode())
    assert [e.kind for e in result.trace] == [
        "model_request",
        "model_response",
        "tool_request",
        "tool_result",
        "model_request",
        "model_response",
        "termination",
    ]
    assert [e.step for e in result.trace] == [1, 1, 1, 1, 2, 2, 2]
    assert result.trace[2].arguments["path"] == "<redacted>"
    assert (
        "def helper" not in result.model_dump_json()
    )  # metadata trace, no source body


def test_sequential_and_batched_tools_have_deterministic_order(setup):
    runtime, provider = setup.build(
        [
            turn(call("get_project_map", {"focus": "run"}, "map")),
            turn(
                call("read_file", {"path": "source.py", "start_line": 3}, "read"),
                call("search_code", {"query": "helper"}, "search"),
            ),
            answer(),
        ]
    )
    result = setup.run(runtime)
    assert result.steps == 3 and result.tool_call_count == 3
    assert [message.tool_call_id for message in tool_messages(provider)] == [
        "map",
        "read",
        "search",
    ]
    assert "source.py" in tool_messages(provider)[0].content
    assert [e.tool_name for e in result.trace if e.kind == "tool_request"] == [
        "get_project_map",
        "read_file",
        "search_code",
    ]
    assert len(json.loads(tool_messages(provider)[2].content)["result"]["matches"]) == 2


@pytest.mark.parametrize(
    "name,args,fragment",
    [
        ("get_project_map", {"focus": "helper"}, "helper"),
        ("find_symbol", {"query": "helper"}, "source.py"),
        ("list_files", {"max_results": 5}, "source.py"),
        ("search_code", {"query": "helper", "glob": "*.py"}, "line_number"),
        ("read_file", {"path": "source.py", "start_line": 3, "end_line": 4}, "def run"),
    ],
)
def test_repository_and_structural_tools_reuse_services(setup, name, args, fragment):
    runtime, provider = setup.build([turn(call(name, args)), answer()])
    assert setup.run(runtime).reason == "completed"
    body = json.loads(tool_messages(provider)[0].content)
    assert body["ok"] and fragment in tool_messages(provider)[0].content
    assert "DENIED FILE SECRET" not in tool_messages(provider)[0].content


@pytest.mark.parametrize(
    "name", ["get_symbol", "get_dependencies", "get_dependents", "get_related_symbols"]
)
def test_symbol_and_relationship_tools(setup, name):
    query = "run" if name == "get_dependencies" else "helper"
    symbol = setup.index.find_symbol(setup.project.id, query)[0]
    runtime, provider = setup.build(
        [turn(call(name, {"symbol_id": symbol.id})), answer()]
    )
    assert setup.run(runtime).reason == "completed"
    assert json.loads(tool_messages(provider)[0].content)["ok"]
    assert symbol.id in tool_messages(provider)[0].content


def test_structural_queries_cannot_leak_sensitive_indexed_files(setup):
    (setup.root / ".env.py").write_text("def DENIED_SYMBOL_SECRET(): pass\n")
    setup.index.refresh_index(setup.project.id)
    secret = setup.index.find_symbol(setup.project.id, "DENIED_SYMBOL_SECRET")[0]
    runtime, provider = setup.build(
        [
            turn(
                call("get_project_map", {"max_tokens": 4000}, "map"),
                call("find_symbol", {"query": "DENIED_SYMBOL_SECRET"}, "find"),
                call("get_symbol", {"symbol_id": secret.id}, "get"),
            ),
            answer(),
        ]
    )
    assert setup.run(runtime).reason == "completed"
    assert all(
        "DENIED_SYMBOL_SECRET" not in message.content
        for message in tool_messages(provider)
    )
    assert (
        json.loads(tool_messages(provider)[-1].content)["error"]["code"]
        == "sensitive_path"
    )


def test_schemas_are_deterministic_typed_and_project_bound(setup):
    first = [tool.definition.model_dump() for tool in setup.tools.values()]
    second = [
        tool.definition.model_dump()
        for tool in repository_toolset(setup.index, setup.repository).values()
    ]
    assert first == second
    for tool in first:
        assert tool["parameters"]["additionalProperties"] is False
        assert "project_id" not in tool["parameters"]["properties"]
    runtime, provider = setup.build([answer()])
    setup.run(runtime)
    assert [t.name for t in provider.requests[0].tools] == sorted(
        REPO_EXPLORER.allowed_tools
    )


@pytest.mark.parametrize(
    "name", ["shell", "write_file", "delete_file", "another_agent", "unknown_tool"]
)
def test_unknown_and_write_tools_rejected_without_execution(setup, name):
    runtime, provider = setup.build([turn(call(name)), answer()])
    result = setup.run(runtime)
    assert result.reason == "tool_not_allowed" and result.tool_call_count == 0
    assert len(provider.requests) == 1


def test_agent_allowed_tools_authoritative_for_known_tools(setup):
    restricted = REPO_EXPLORER.model_copy(
        update={"allowed_tools": ("get_project_map",)}
    )
    runtime, provider = setup.build([turn(call())], agent=restricted)
    assert setup.run(runtime).reason == "tool_not_allowed"
    assert [tool.name for tool in provider.requests[0].tools] == ["get_project_map"]


@pytest.mark.parametrize(
    "args",
    [
        {"path": "source.py", "project_id": "another-project"},
        {"path": "source.py", "start_line": "1"},
        {"path": "source.py", "start_line": True},
        {"path": 42},
        {"start_line": 1},
        {"path": "source.py", "end_line": 0},
        {"path": "source.py", "start_line": 4, "end_line": 2},
        {"path": "source.py", "max_bytes": 1_000_000},
        {"path": "../outside"},
        {"path": "/absolute"},
        {"path": "x\u0000y"},
    ],
)
def test_malformed_arguments_do_not_reach_services(setup, monkeypatch, args):
    def denied(*args, **kwargs):
        pytest.fail("Invalid arguments reached filesystem service")

    monkeypatch.setattr(setup.repository, "read_file", denied)
    runtime, provider = setup.build([turn(call(arguments=args)), answer()])
    assert setup.run(runtime).reason == "completed"
    assert json.loads(tool_messages(provider)[0].content) == {
        "ok": False,
        "error": {"code": "invalid_arguments"},
    }


@pytest.mark.parametrize(
    "name,args,code",
    [
        ("read_file", {"path": "missing"}, "path_not_found"),
        ("read_file", {"path": ".env"}, "sensitive_path"),
        ("read_file", {"path": "binary"}, "unsupported_text"),
        ("git_status", {"path": "."}, "not_git_repository"),
        ("get_symbol", {"symbol_id": "missing"}, "symbol_not_found"),
    ],
)
def test_recoverable_errors_are_bounded_sanitized_and_model_can_recover(
    setup, name, args, code
):
    runtime, provider = setup.build(
        [turn(call(name, args)), answer("Evidence insufficient.")]
    )
    result = setup.run(runtime)
    assert result.reason == "completed"
    body = tool_messages(provider)[0].content
    assert json.loads(body) == {"ok": False, "error": {"code": code}}
    assert len(body.encode()) < 100
    assert "SECRET" not in result.model_dump_json() + body
    assert result.trace[3].success is False


def test_git_backend_diagnostics_never_reach_model_or_trace(setup, monkeypatch):
    def failed(*args, **kwargs):
        raise GitFailure("RAW STDERR SECRET")

    monkeypatch.setattr(setup.repository, "git_status", failed)
    runtime, provider = setup.build([turn(call("git_status", {"path": "."})), answer()])
    result = setup.run(runtime)
    assert result.reason == "completed"
    assert (
        "RAW STDERR SECRET"
        not in result.model_dump_json() + tool_messages(provider)[0].content
    )


@pytest.mark.parametrize("cached_only", [False, True])
def test_root_identity_violation_is_terminal_even_for_cached_index(
    setup, tmp_path, cached_only
):
    def replace_root():
        setup.root.rename(tmp_path / "old")
        setup.root.mkdir()
        (setup.root / "source.py").write_text("REPLACEMENT SECRET")
        return turn(
            call("get_project_map", {"focus": "helper"}) if cached_only else call()
        )

    runtime, provider = setup.build([replace_root, answer()])
    result = setup.run(runtime)
    assert result.reason == "security_error"
    assert (
        len(provider.requests) == 1
        and "REPLACEMENT SECRET" not in result.model_dump_json()
    )


def test_symlink_rejection_is_terminal(setup, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("OUTSIDE SECRET")
    (setup.root / "alias").symlink_to(outside)
    runtime, provider = setup.build([turn(call(arguments={"path": "alias"})), answer()])
    result = setup.run(runtime)
    assert result.reason == "security_error" and len(provider.requests) == 1
    assert "OUTSIDE SECRET" not in result.model_dump_json()


@pytest.mark.parametrize(
    "error,reason",
    [
        (BackendUnavailable("RAW BACKEND SECRET"), "provider_error"),
        (RuntimeError("RAW STDERR SECRET"), "provider_error"),
        (ProviderTimeout("RAW TIMEOUT SECRET"), "provider_timeout"),
    ],
)
def test_backend_failure_does_not_fallback_and_is_sanitized(setup, error, reason):
    runtime, provider = setup.build([error, answer()])
    result = setup.run(runtime, limits=RuntimeLimits(timeout_seconds=200.0))
    assert result.reason == reason
    assert provider.workers == [setup.worker]
    assert "SECRET" not in result.model_dump_json()


def test_storage_failure_is_terminal_and_sanitized(setup, monkeypatch):
    def failed(*args, **kwargs):
        raise IndexStorageError("RAW SQL SECRET")

    monkeypatch.setattr(setup.index, "find_symbol", failed)
    runtime, provider = setup.build(
        [turn(call("find_symbol", {"query": "helper"})), answer()]
    )
    result = setup.run(runtime)
    assert result.reason == "tool_error" and len(provider.requests) == 1
    assert "RAW SQL SECRET" not in result.model_dump_json()


def test_single_overall_deadline_is_not_reset_between_model_calls(setup):
    def advance():
        setup.clock.advance(3)
        return turn(call(identity=f"id-{setup.clock.value}"))

    runtime, provider = setup.build([advance, advance, answer()])
    result = setup.run(runtime, limits=RuntimeLimits(timeout_seconds=5.0))
    assert result.reason == "timeout"
    assert [request.timeout_seconds for request in provider.requests] == [5.0, 2.0]
    assert result.tool_call_count == 1


def test_synchronous_tool_deadline_checked_after_return(setup, monkeypatch):
    original = setup.repository.read_file

    def slow(*args, **kwargs):
        setup.clock.advance(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(setup.repository, "read_file", slow)
    runtime, provider = setup.build([turn(call()), answer()])
    assert (
        setup.run(runtime, limits=RuntimeLimits(timeout_seconds=5.0)).reason
        == "timeout"
    )
    assert len(provider.requests) == 1


def test_runtime_enforces_timeout_when_provider_ignores_override(setup):
    runtime, provider = setup.build([])

    async def blocked(worker, request):
        await asyncio.Future()

    provider.generate = blocked
    result = setup.run(runtime, limits=RuntimeLimits(timeout_seconds=0.001))
    assert result.reason == "timeout"


def test_runtime_enforces_worker_timeout_separately(setup):
    tiny = setup.worker.model_copy(update={"timeout_seconds": 0.001})
    runtime, provider = setup.build([], worker_config=[tiny])

    async def blocked(worker, request):
        await asyncio.Future()

    provider.generate = blocked
    assert setup.run(runtime).reason == "provider_timeout"


def test_repeated_tool_loop_terminates_at_max_steps(setup):
    runtime, provider = setup.build([turn(call(identity=f"id-{n}")) for n in range(4)])
    result = setup.run(runtime, limits=RuntimeLimits(max_steps=3))
    assert result.reason == "max_steps" and result.steps == 3
    assert result.tool_call_count == 3 and len(provider.requests) == 3


def test_batch_over_tool_call_limit_is_rejected_before_any_tool(setup):
    runtime, provider = setup.build(
        [turn(call(identity="one"), call(identity="two")), answer()]
    )
    result = setup.run(runtime, limits=RuntimeLimits(max_tool_calls=1))
    assert result.reason == "max_tool_calls" and result.tool_call_count == 0
    assert len(provider.requests) == 1


def test_cumulative_tool_output_is_bounded_and_downstream_budgets_shrink(
    setup, monkeypatch
):
    (setup.root / "source.py").write_text("x" * 1000)
    budgets = []
    original = setup.repository.read_file

    def tracked(*args, **kwargs):
        budgets.append(kwargs["max_bytes"])
        return original(*args, **kwargs)

    monkeypatch.setattr(setup.repository, "read_file", tracked)
    runtime, provider = setup.build([turn(call(identity=f"id-{n}")) for n in range(12)])
    result = setup.run(runtime, limits=RuntimeLimits(max_tool_output_bytes=2700))
    assert result.reason == "tool_output_limit"
    assert result.tool_output_bytes <= 2700
    assert budgets[0] > budgets[-1]
    assert result.tool_call_count < 12


def test_individual_serialized_result_limit_covers_json_metadata(setup):
    tool = setup.tools["read_file"]
    custom = {
        **setup.tools,
        "read_file": replace(tool, execute=lambda *args: "x" * 500),
    }
    runtime, provider = setup.build([turn(call()), answer()], tools_config=custom)
    result = setup.run(runtime, limits=RuntimeLimits(max_tool_result_bytes=256))
    assert result.reason == "tool_result_limit" and result.tool_output_bytes == 0
    assert len(provider.requests) == 1


@pytest.mark.parametrize("task", ["x" * 80_000, "界" * 27_000])
def test_initial_context_counts_utf8_task_and_system_and_schemas(setup, task):
    runtime, provider = setup.build([answer()])
    assert setup.run(runtime, task=task).reason == "context_limit"
    assert not provider.requests


def test_tool_definitions_are_in_context_budget(setup):
    runtime, provider = setup.build([answer()])
    assert (
        setup.run(runtime, limits=RuntimeLimits(max_context_tokens=500)).reason
        == "context_limit"
    )
    assert not provider.requests


def test_worker_context_window_caps_runtime_budget(setup):
    worker = setup.worker.model_copy(update={"context_window": 500})
    runtime, provider = setup.build([answer()], worker_config=[worker])
    assert setup.run(runtime).reason == "context_limit"
    assert not provider.requests


def test_tool_evidence_context_limit_stops_before_next_model_request(setup):
    agent = REPO_EXPLORER.model_copy(update={"allowed_tools": ("read_file",)})
    (setup.root / "source.py").write_text("x" * 4000)
    runtime, provider = setup.build([turn(call()), answer()], agent=agent)
    result = setup.run(runtime, limits=RuntimeLimits(max_context_tokens=1300))
    assert result.reason == "context_limit" and result.tool_call_count == 1
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        answer("x" * 80_000),
        turn(call(arguments={"path": "source.py", "unused": "x" * 80_000})),
    ],
)
def test_assistant_text_and_tool_arguments_count_toward_context(setup, response):
    runtime, provider = setup.build([response, answer()])
    result = setup.run(runtime)
    assert result.reason == "context_limit" and result.tool_call_count == 0
    assert len(provider.requests) == 1


def test_cancellation_before_first_model_call(setup):
    token = CancellationToken()
    token.cancel()
    runtime, provider = setup.build([answer()])
    result = setup.run(runtime, cancellation=token)
    assert result.reason == "cancelled" and result.state == "cancelled"
    assert not provider.requests


def test_cancellation_between_model_and_tool(setup):
    token = CancellationToken()

    def cancel():
        token.cancel()
        return turn(call())

    runtime, provider = setup.build([cancel, answer()])
    result = setup.run(runtime, cancellation=token)
    assert result.reason == "cancelled" and result.tool_call_count == 0
    assert len(provider.requests) == 1


def test_cancellation_between_tool_and_next_model(setup, monkeypatch):
    token = CancellationToken()
    original = setup.repository.read_file

    def cancel(*args, **kwargs):
        value = original(*args, **kwargs)
        token.cancel()
        return value

    monkeypatch.setattr(setup.repository, "read_file", cancel)
    runtime, provider = setup.build([turn(call()), answer()])
    result = setup.run(runtime, cancellation=token)
    assert result.reason == "cancelled" and result.tool_call_count == 1
    assert len(provider.requests) == 1


def test_asyncio_cancelled_error_is_never_swallowed(setup):
    runtime, provider = setup.build([])

    async def cancelled(worker, request):
        raise asyncio.CancelledError

    provider.generate = cancelled
    with pytest.raises(asyncio.CancelledError):
        setup.run(runtime)


def test_observed_usage_preserved_per_turn_and_missing_is_unknown(setup):
    known = TokenUsage(input_tokens=40, output_tokens=8)
    runtime, provider = setup.build(
        [turn(call()).model_copy(update={"token_usage": known}), answer()]
    )
    result = setup.run(runtime)
    assert result.usage == (known, None)
    runtime, provider = setup.build([answer()])
    assert setup.run(runtime).usage == (None,)


@pytest.mark.parametrize(
    "response", [answer(""), turn(call(identity="same"), call(identity="same"))]
)
def test_empty_response_and_duplicate_call_ids_are_terminal(setup, response):
    runtime, provider = setup.build([response, answer()])
    assert setup.run(runtime).reason == "invalid_response"
    assert len(provider.requests) == 1


def test_call_id_reuse_across_turns_is_terminal(setup):
    runtime, provider = setup.build([turn(call()), turn(call()), answer()])
    result = setup.run(runtime)
    assert result.reason == "invalid_response" and result.tool_call_count == 1
    assert len(provider.requests) == 2


def test_repo_explorer_prompt_requires_evidence_and_hypothesis_and_read_only(setup):
    prompt = REPO_EXPLORER.system_prompt
    for text in [
        "Inspect evidence",
        "hypothesis/inference",
        "insufficient",
        "Never invent",
        "not source truth",
        "line ranges",
        "cannot modify",
        "untrusted data",
    ]:
        assert text in prompt
    assert (
        "shell" not in REPO_EXPLORER.allowed_tools
        and "write_file" not in REPO_EXPLORER.allowed_tools
    )


@pytest.mark.parametrize(
    "values",
    [
        {"max_steps": 0},
        {"max_steps": 101},
        {"timeout_seconds": 601.0},
        {"timeout_seconds": float("nan")},
        {"max_tool_calls": 201},
        {"max_tool_result_bytes": 65537},
        {"max_tool_output_bytes": 524289},
        {"max_context_tokens": 200001},
        {"max_steps": True},
    ],
)
def test_hard_limits_and_strict_configuration(values):
    with pytest.raises(ValidationError):
        RuntimeLimits(**values)


def test_invalid_agent_tool_configuration_and_disabled_worker_tools(setup):
    bad = REPO_EXPLORER.model_copy(update={"allowed_tools": ("shell",)})
    runtime, provider = setup.build([answer()], agent=bad)
    assert (
        setup.run(runtime).reason == "invalid_configuration" and not provider.requests
    )
    disabled = setup.worker.model_copy(update={"supports_tools": False})
    runtime, provider = setup.build([answer()], worker_config=[disabled])
    assert (
        setup.run(runtime).reason == "invalid_configuration" and not provider.requests
    )


def test_git_tools_in_registered_project_are_read_only(setup):
    git = shutil.which("git")
    if git is None:
        pytest.skip("Requires local Git executable, no network")

    def command(*args):
        subprocess.run(
            [
                git,
                "-C",
                str(setup.root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    command("init", "--initial-branch=main")
    command("add", "source.py")
    command("commit", "-m", "Fixture")
    (setup.root / "source.py").write_text("def helper():\n    return 1\n")
    before = {
        p.relative_to(setup.root): p.read_bytes()
        for p in setup.root.rglob("*")
        if p.is_file()
    }
    runtime, provider = setup.build(
        [
            turn(
                call("git_status", {"path": "."}, "status"),
                call("git_grep", {"query": "helper"}, "grep"),
                call("git_diff", {"path": "source.py"}, "diff"),
            ),
            answer(),
        ]
    )
    assert setup.run(runtime).reason == "completed"
    results = [json.loads(message.content) for message in tool_messages(provider)]
    assert all(body["ok"] for body in results)
    changes = {change["path"]: change for change in results[0]["result"]["changes"]}
    assert changes["source.py"]["worktree_status"] == "M"
    assert results[1]["result"]["matches"][0]["line_number"] == 1
    assert "return 1" in results[2]["result"]["content"]
    after = {
        p.relative_to(setup.root): p.read_bytes()
        for p in setup.root.rglob("*")
        if p.is_file()
    }
    assert before == after


def test_trace_order_and_values_repeat_deterministically(setup):
    results = []
    for _ in range(2):
        runtime, provider = setup.build([turn(call()), answer()])
        results.append(setup.run(runtime).trace)
    assert results[0] == results[1]
    assert "DENIED FILE SECRET" not in json_text(
        [event.model_dump() for event in results[0]]
    )


def test_provider_timeout_is_distinct_from_unexpired_overall_deadline(setup):
    runtime, provider = setup.build([ProviderTimeout("SECRET")])
    result = setup.run(runtime)
    assert result.reason == "provider_timeout"
    assert setup.clock.value == 0.0


def test_unknown_tool_in_batch_prevents_earlier_valid_tool_execution(setup):
    runtime, provider = setup.build(
        [turn(call(), call("shell", identity="denied")), answer()]
    )
    result = setup.run(runtime)
    assert result.reason == "tool_not_allowed" and result.tool_call_count == 0
    assert not any(event.kind == "tool_request" for event in result.trace)


def test_cancellation_during_first_batched_tool_stops_second_tool(setup, monkeypatch):
    token = CancellationToken()
    original = setup.repository.read_file

    def cancel(*args, **kwargs):
        value = original(*args, **kwargs)
        token.cancel()
        return value

    monkeypatch.setattr(setup.repository, "read_file", cancel)
    runtime, provider = setup.build(
        [turn(call(identity="one"), call(identity="two")), answer()]
    )
    result = setup.run(runtime, cancellation=token)
    assert result.reason == "cancelled" and result.tool_call_count == 1
    assert len(provider.requests) == 1


def test_root_replacement_during_tool_is_terminal_without_publishing_result(
    setup, monkeypatch, tmp_path
):
    original = setup.repository.read_file

    def replace_root(*args, **kwargs):
        value = original(*args, **kwargs)
        setup.root.rename(tmp_path / "replaced")
        setup.root.mkdir()
        return value

    monkeypatch.setattr(setup.repository, "read_file", replace_root)
    runtime, provider = setup.build([turn(call()), answer()])
    result = setup.run(runtime)
    assert result.reason == "security_error" and result.tool_output_bytes == 0
    assert len(provider.requests) == 1


def test_relationships_hide_edges_into_sensitive_index_files(setup):
    (setup.root / ".env.py").write_text("def DENIED_EDGE_SECRET(): pass\n")
    setup.index.refresh_index(setup.project.id)
    source = setup.index.find_symbol(setup.project.id, "run")[0]
    secret = setup.index.find_symbol(setup.project.id, "DENIED_EDGE_SECRET")[0]
    from agentforge.index.models import Relationship

    # Test wrapper filtering independently of the deliberately limited resolver.
    setup.index.get_dependencies = lambda *args: [
        Relationship(source.id, "calls", "DENIED_EDGE_SECRET", secret.id)
    ]
    tools = repository_toolset(setup.index, setup.repository)
    runtime, provider = setup.build(
        [turn(call("get_dependencies", {"symbol_id": source.id})), answer()],
        tools_config=tools,
    )
    assert setup.run(runtime).reason == "completed"
    assert json.loads(tool_messages(provider)[0].content)["result"]["items"] == []
    assert "DENIED_EDGE_SECRET" not in tool_messages(provider)[0].content


def test_aggregate_budget_checks_serialized_result_independently(setup):
    tool = setup.tools["read_file"]
    custom = {
        **setup.tools,
        "read_file": replace(tool, execute=lambda *args: "x" * 400),
    }
    runtime, provider = setup.build([turn(call()), answer()], tools_config=custom)
    result = setup.run(runtime, limits=RuntimeLimits(max_tool_output_bytes=256))
    assert result.reason == "tool_output_limit" and result.tool_output_bytes == 0
    assert len(provider.requests) == 1


def test_tool_results_never_serialize_arbitrary_exception_objects(setup):
    tool = setup.tools["read_file"]
    custom = {
        **setup.tools,
        "read_file": replace(
            tool, execute=lambda *args: RuntimeError("RAW BACKEND SECRET")
        ),
    }
    runtime, provider = setup.build([turn(call()), answer()], tools_config=custom)
    result = setup.run(runtime)
    assert result.reason == "tool_error" and len(provider.requests) == 1
    assert "RAW BACKEND SECRET" not in result.model_dump_json()
