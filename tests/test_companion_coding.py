"""Real coding contracts: summaries exclude captures; diff stays on demand."""

import asyncio

import pytest

from agentforge.coding.config import ValidationCommand
from agentforge.coding.models import CodingLimits
from tests.test_coding_tasks import Provider, application, submit
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_web import client_for

coding = coding_fixture


@pytest.mark.parametrize("exit_code", [0, 3])
def test_workspace_diff_validation_summary(coding, database, monkeypatch, exit_code):
    import sys
    from pathlib import Path

    limits = CodingLimits(max_diff_bytes=256)
    coding.config = coding.config.model_copy(
        update={
            "limits": limits,
            "validations": {
                "check": ValidationCommand(
                    argv=(
                        str(Path(sys.executable).resolve()),
                        "-c",
                        "print('PRIVATE_VALIDATION_CAPTURE'); "
                        f"raise SystemExit({exit_code})",
                    )
                )
            },
        }
    )
    provider = Provider(
        [
            (
                "write_file",
                {
                    "path": "new.txt",
                    "content": "<script>untrusted</script>\n" * 100,
                    "expected_sha256": None,
                },
            ),
            ("run_validation", {"name": "check"}),
        ]
    )

    async def execute():
        app, _ = application(coding, database, provider)
        async with client_for(app) as (client, _):
            task = submit(app, coding)
            await app.tasks.wait_task(task.task_id)
            report = app.get_coding_workspace(task_id=task.task_id)
            calls = []
            original = app.get_coding_diff
            inspection_calls = []
            original_inspection = app.coding.diff

            def inspect(*args, **kwargs):
                inspection_calls.append(True)
                return original_inspection(*args, **kwargs)

            monkeypatch.setattr(app.coding, "diff", inspect)

            def diff(**kwargs):
                calls.append(kwargs)
                return original(**kwargs)

            monkeypatch.setattr(app, "get_coding_diff", diff)
            path = f"/companion/tasks/{task.task_id}"
            body = (await client.get(path)).text
            for expected in (
                report.branch_name,
                report.base_commit,
                "new.txt",
                "Writes / bytes",
                "Validation duration",
                "Inspect bounded current diff",
            ):
                assert expected in body
            assert ("check · passed" if exit_code == 0 else "check · failed") in body
            assert calls == [] and inspection_calls == []
            assert "PRIVATE_VALIDATION_CAPTURE" not in body
            assert str(coding.parent) not in body
            assert "<script>untrusted" not in body
            body = (await client.get(path + "/diff")).text
            assert len(calls) == len(inspection_calls) == 1
            assert "&lt;script&gt;untrusted" in body
            assert "Diff truncated" in body
            assert str(coding.parent) not in body
            report = app.get_coding_workspace(task_id=task.task_id)
            app.cleanup_coding_workspace(
                task_id=task.task_id, workspace_id=report.workspace_id
            )
            body = (await client.get(path)).text
            assert "Inspection unavailable" in body
            assert "Inspect bounded current diff" not in body

    asyncio.run(execute())
