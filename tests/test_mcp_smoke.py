"""Manual utility checks with actual central tools/Tasks and scripted inference."""

import importlib.util
from pathlib import Path
from uuid import UUID

import pytest

from agentforge.core.inference import GenerationResult, ToolCall
from tests.test_mcp import setup as mcp_fixture

setup = mcp_fixture


@pytest.mark.parametrize("source_call", [False, True])
def test_smoke_source_acceptance_and_durable_trace(
    setup, monkeypatch, capsys, source_call
):
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_mcp.py"
    spec = importlib.util.spec_from_file_location("manual_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if source_call:
        setup.provider.turns.append(
            GenerationResult(
                content="",
                model="fake",
                tool_calls=[
                    ToolCall(
                        id="read", name="read_file", arguments={"path": "source.py"}
                    )
                ],
            )
        )
    setup.provider.turns.append(
        GenerationResult(content="Source evidence.", model="fake")
    )

    async def scripted_mcp(arguments):
        app = setup.app
        await app.start()
        try:
            submitted = app.delegate_task(
                project_id=UUID(arguments.project_id),
                agent_id="repo_explorer",
                worker_id=arguments.worker_id,
                task=arguments.task,
            )
            await app.tasks.wait_task(submitted.task_id)
            return app.get_task(task_id=submitted.task_id).model_dump(mode="json")
        finally:
            await app.close()

    monkeypatch.setattr(module, "smoke", scripted_mcp)
    monkeypatch.setattr(
        "sys.argv",
        [
            "smoke",
            "--database-url",
            str(setup.database[1]),
            "--workers",
            "unused.toml",
            "--project-id",
            str(setup.binding["project_id"]),
            "--worker-id",
            "home-i5",
            "--task",
            "Read source evidence",
            "--verify-source",
            "source.py",
        ],
    )
    assert module.main() == (0 if source_call else 1)
    output = capsys.readouterr()
    assert '"escape_rejected": true' in output.out
    assert '"model_tool_calls"' in output.out
    if source_call:
        assert '"name": "read_file"' in output.out
    else:
        assert "No successful model source" in output.err
