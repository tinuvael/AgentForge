"""Shipped Provider wire finish reasons through runtime to independent storage."""

import json
import sqlite3

import httpx
import pytest

from agentforge.application.service import Application
from agentforge.core.worker import Worker
from agentforge.db.database import create_database_engine
from agentforge.providers.ollama import OllamaProvider
from agentforge.providers.openai_compatible import OpenAICompatibleProvider
from agentforge.workers.config import ProviderConnection, WorkersConfig
from tests.test_mcp import run


@pytest.mark.parametrize("protocol", ["ollama", "openai_compatible"])
@pytest.mark.parametrize(
    "case", ["stop", "length", "tools", "truncated_tools", "absent"]
)
def test_wire_finish_reason_controls_durable_task(
    database, registry, tmp_path, protocol, case
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "evidence.txt").write_text("Evidence")
    project = registry.register_project("Evidence", root)
    requests = []
    normalized = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        tools = case in {"tools", "truncated_tools"} and len(requests) == 1
        reason = "length" if case in {"length", "truncated_tools"} else "stop"
        function = {"name": "read_file", "arguments": {"path": "evidence.txt"}}
        message = {
            "role": "assistant",
            "content": "Incomplete answer" if reason == "length" else "Answer",
        }
        if tools:
            message["content"] = ""
            message["tool_calls"] = [{"id": "issued-call", "function": function}]
        if protocol == "ollama":
            wire = {"model": "observed-model", "done": True, "message": message}
            if case != "absent":
                wire["done_reason"] = reason
        else:
            if tools:
                message["tool_calls"][0]["type"] = "function"
                function["arguments"] = json.dumps(function["arguments"])
            choice = {"index": 0, "message": message}
            if case != "absent":
                choice["finish_reason"] = (
                    "tool_calls" if case == "tools" and tools else reason
                )
            wire = {"model": "observed-model", "choices": [choice]}
        return httpx.Response(200, json=wire)

    connection = ProviderConnection(
        id="selected", type=protocol, base_url="http://backend.invalid/v1"
    )
    provider = (
        OllamaProvider(connection, transport=httpx.MockTransport(handler))
        if protocol == "ollama"
        else OpenAICompatibleProvider(
            connection, transport=httpx.MockTransport(handler)
        )
    )
    generate = provider.generate

    async def observe(worker, request):
        response = await generate(worker, request)
        normalized.append(response)
        return response

    provider.generate = observe
    worker = Worker(
        id="explicit",
        provider=protocol,
        provider_connection="selected",
        model="configured",
        supports_tools=True,
    )
    app = Application(
        create_database_engine(database[1]),
        WorkersConfig(providers=[connection], workers=[worker]),
        providers={"selected": provider},
    )

    async def execute():
        await app.start()
        try:
            submitted = app.delegate_task(
                project_id=project.id,
                agent_id="general_agent",
                worker_id=worker.id,
                task="Read evidence.txt if useful",
            )
            final = await app.tasks.wait_task(submitted.task_id)
            with sqlite3.connect(database[1].database) as reader:
                state, reason, answer = reader.execute(
                    "SELECT state, reason, execution_result FROM tasks WHERE task_id=?",
                    (submitted.task_id.hex,),
                ).fetchone()
            expected = (
                "output_limit"
                if case in {"length", "truncated_tools"}
                else "provider_error"
                if protocol == "openai_compatible" and case == "absent"
                else "completed"
            )
            assert final.reason == reason == expected
            assert (
                final.state
                == state
                == ("completed" if expected == "completed" else "failed")
            )
            assert json.loads(answer)["final_answer"] == (
                "Answer" if expected == "completed" else None
            )
            if normalized:
                assert normalized[0].finish_reason == (
                    None
                    if case == "absent"
                    else "tool_calls"
                    if protocol == "openai_compatible" and case == "tools"
                    else "length"
                    if case in {"length", "truncated_tools"}
                    else "stop"
                )
            if case == "tools":
                assert final.execution_result.tool_call_count == 1
                assert len(requests) == 2
                assert normalized[0].tool_calls[0].id == "issued-call"
                assert normalized[0].tool_calls[0].arguments == {"path": "evidence.txt"}
                assert requests[1]["messages"][-1]["role"] == "tool"
            else:
                assert len(requests) == 1  # No continuation or silent retry.
                assert final.execution_result.tool_call_count == 0
        finally:
            await app.close()
            if hasattr(provider, "aclose"):
                await provider.aclose()

    run(execute())
