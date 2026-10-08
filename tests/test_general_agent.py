"""General analysis uses the shipped definition and existing execution boundaries."""

import json

import pytest

from agentforge.agents import GENERAL_AGENT, REPO_EXPLORER
from agentforge.application.definitions import shipped_agents
from agentforge.application.service import Application
from agentforge.coding.tools import CODER
from agentforge.db.database import create_database_engine
from agentforge.tasks.models import TaskValidationError
from agentforge.workers.config import WorkersConfig
from tests.test_agent_runtime import answer, call, tool_messages, turn
from tests.test_agent_runtime import setup as runtime_setup_fixture
from tests.test_councils import finish
from tests.test_mcp import run
from tests.test_mcp import setup as application_setup_fixture

runtime_setup = runtime_setup_fixture
application_setup = application_setup_fixture


@pytest.mark.parametrize("coding_enabled", [False, True])
def test_shipped_composition(coding_enabled):
    expected = (GENERAL_AGENT, REPO_EXPLORER)
    if coding_enabled:
        expected += (CODER,)
    assert shipped_agents(coding_enabled=coding_enabled) == expected
    assert len({agent.id for agent in expected}) == len(expected)


def test_general_contract_and_specialist_distinction():
    agent = GENERAL_AGENT
    assert agent.id == "general_agent" and agent.name == "General Agent"
    assert "analysis, comparison and synthesis" in agent.description
    assert "supplied context" in agent.description
    assert agent.workspace_mode == "project_readonly"
    assert agent.allowed_tools == (
        "list_files",
        "read_file",
        "search_code",
        "git_status",
        "git_diff",
    )
    assert set(agent.allowed_tools) < set(REPO_EXPLORER.allowed_tools)
    assert set(REPO_EXPLORER.allowed_tools) - set(agent.allowed_tools) == {
        "get_project_map",
        "find_symbol",
        "get_symbol",
        "get_dependencies",
        "get_dependents",
        "get_related_symbols",
        "git_grep",
    }
    assert agent.limits.model_dump() == {
        "max_steps": 8,
        "timeout_seconds": 90.0,
        "max_tool_calls": 12,
        "max_tool_result_bytes": 12_000,
        "max_tool_output_bytes": 48_000,
        "max_context_tokens": 24_000,
    }
    assert agent.system_prompt != REPO_EXPLORER.system_prompt
    for instruction in (
        "primary objective",
        "optional supporting",
        "only when useful",
        "untrusted data",
        "observed facts from inference",
        "contradictions",
        "uncertainty",
        "missing information",
        "Do not invent",
        "cite",
        "truncation",
        "sufficient evidence",
        "cannot change",
        "execute code",
        "shell commands",
        "external network",
        "choose another Worker",
        "delegate",
        "Council",
        "expand your own permissions",
    ):
        assert instruction in agent.system_prompt
    assert "exact implementation details" in REPO_EXPLORER.system_prompt
    assert "cached structural" not in agent.system_prompt.lower()


@pytest.mark.parametrize(
    "forbidden",
    [
        "get_project_map",
        "find_symbol",
        "git_grep",
        "write_file",
        "apply_patch",
        "delete_file",
        "run_validation",
        "shell",
        "http_fetch",
        "delegate_task",
        "delegate_council",
        "worker_probe",
        "register_project",
        "db_upgrade",
    ],
)
def test_runtime_rejects_nonallowlisted_batch_before_any_io(runtime_setup, forbidden):
    runtime, provider = runtime_setup.build(
        [turn(call(), call(forbidden, identity="denied")), answer()],
        agent=GENERAL_AGENT,
    )
    result = runtime_setup.run(runtime, agent_id=GENERAL_AGENT.id)
    assert result.reason == "tool_not_allowed" and result.tool_call_count == 0
    assert not any(event.kind == "tool_request" for event in result.trace)
    assert len(provider.requests) == 1
    assert {tool.name for tool in provider.requests[0].tools} == set(
        GENERAL_AGENT.allowed_tools
    )


def test_document_comparison_reads_real_evidence_and_preserves_files(runtime_setup):
    root = runtime_setup.root
    (root / "README.md").write_text("Deployment uses one process per database.\n")
    (root / "operations.txt").write_text("Run two services on separate databases.\n")
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    final = (
        "README.md:1 requires one process per database; operations.txt:1 uses "
        "separate databases for two services. These instructions are consistent."
    )
    runtime, provider = runtime_setup.build(
        [
            turn(call("list_files", {"max_results": 20}, "list")),
            turn(call("search_code", {"query": "database", "glob": "*.md"}, "search")),
            turn(
                call(arguments={"path": "README.md"}, identity="readme"),
                call(arguments={"path": "operations.txt"}, identity="operations"),
            ),
            answer(final),
        ],
        agent=GENERAL_AGENT,
    )
    result = runtime_setup.run(
        runtime,
        agent_id=GENERAL_AGENT.id,
        task="Compare the deployment instructions and identify contradictions.",
    )
    assert result.state == "completed" and result.final_answer == final
    assert result.steps == 4 and result.tool_call_count == 4
    assert result.tool_output_bytes <= GENERAL_AGENT.limits.max_tool_output_bytes
    bodies = [json.loads(message.content) for message in tool_messages(provider)]
    assert all(body["ok"] for body in bodies)
    assert bodies[1]["result"]["matches"][0]["path"] == "README.md"
    assert bodies[2]["result"]["content"] == before["README.md"].decode()
    assert bodies[3]["result"]["content"] == before["operations.txt"].decode()
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before


def test_supplied_context_can_complete_without_tools(runtime_setup):
    runtime, provider = runtime_setup.build(
        [answer("Both notes specify a single database.")], agent=GENERAL_AGENT
    )
    result = runtime_setup.run(
        runtime,
        agent_id=GENERAL_AGENT.id,
        task="Compare these notes: A says one database; B says a single database.",
    )
    assert result.state == "completed" and result.tool_call_count == 0
    assert len(provider.requests) == 1


@pytest.mark.parametrize("boundary", ["steps", "calls", "timeout", "context", "output"])
def test_shipped_limits_enforced_without_overrides(runtime_setup, boundary):
    if boundary == "steps":
        turns = [turn(call(identity=str(i))) for i in range(8)] + [answer()]
        reason, requests = "max_steps", 8
    elif boundary == "calls":
        turns = [turn(*(call(identity=str(i)) for i in range(13))), answer()]
        reason, requests = "max_tool_calls", 1
    elif boundary == "timeout":

        def late():
            runtime_setup.clock.advance(90.0)
            return answer()

        turns = [late]
        reason, requests = "timeout", 1
    elif boundary == "context":
        turns = [answer("x" * 72_000)]
        reason, requests = "context_limit", 1
    else:
        (runtime_setup.root / "source.py").write_text("x" * 6000)
        turns = [turn(*(call(identity=str(i)) for i in range(12))), answer()]
        reason, requests = "tool_output_limit", 1
    runtime, provider = runtime_setup.build(turns, agent=GENERAL_AGENT)
    result = runtime_setup.run(runtime, agent_id=GENERAL_AGENT.id)
    assert result.reason == reason and result.state == "failed"
    assert len(provider.requests) == requests
    assert result.steps <= GENERAL_AGENT.limits.max_steps
    assert result.tool_call_count <= GENERAL_AGENT.limits.max_tool_calls
    assert result.tool_output_bytes <= GENERAL_AGENT.limits.max_tool_output_bytes


@pytest.mark.parametrize("path", ["../outside.txt", "/outside.txt", ".env"])
def test_existing_path_policy_blocks_unsafe_or_sensitive_reads(runtime_setup, path):
    runtime, provider = runtime_setup.build(
        [turn(call(arguments={"path": path})), answer("Evidence unavailable.")],
        agent=GENERAL_AGENT,
    )
    result = runtime_setup.run(runtime, agent_id=GENERAL_AGENT.id)
    assert result.state == "completed"
    body = json.loads(tool_messages(provider)[0].content)
    assert not body["ok"] and "result" not in body
    assert "DENIED FILE SECRET" not in json.dumps(body)


def test_application_discovery_and_independent_readonly_council(application_setup):
    async def execute():
        app = application_setup.app
        await app.start()
        try:
            assert [agent.agent_id for agent in app.list_agents().agents] == [
                "general_agent",
                "repo_explorer",
            ]
            assert app.status().agent_count == 2
            council = app.delegate_council(
                project_id=application_setup.project.id,
                agent_id=GENERAL_AGENT.id,
                worker_ids=("ai395", "home-i5"),
                task="Compare supplied notes: A says one service; B says two.",
            )
            result = await finish(app, council)
            assert result.terminal and result.participant_counts["completed"] == 2
            assert result.agent_id == GENERAL_AGENT.id
            assert all(
                app.tasks.get_task(p.task_id).agent_id == GENERAL_AGENT.id
                for p in result.participants
            )
            requests = application_setup.provider.requests
            assert len(requests) == 2
            assert requests[0].messages == requests[1].messages
            assert all(len(request.messages) == 2 for request in requests)
            assert requests[0].messages[0].content == GENERAL_AGENT.system_prompt
        finally:
            await app.close()

    run(execute())


def test_task_binding_rejects_worker_without_tools_no_fallback(application_setup):
    workers = WorkersConfig(
        workers=[
            worker.model_copy(update={"supports_tools": False})
            for worker in application_setup.workers.workers
        ]
    )
    app = Application(
        create_database_engine(application_setup.database[1]),
        workers,
        providers={"fake": application_setup.provider},
    )

    async def execute():
        await app.start()
        try:
            with pytest.raises(TaskValidationError):
                app.delegate_task(
                    project_id=application_setup.project.id,
                    agent_id=GENERAL_AGENT.id,
                    worker_id="local-4080",
                    task="Summarize only this supplied text.",
                )
            assert not app.tasks.history() and not application_setup.provider.requests
        finally:
            await app.close()

    run(execute())
