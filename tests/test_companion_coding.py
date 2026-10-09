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


@pytest.mark.parametrize("retained", [None, [], ["retained.txt"]])
def test_cached_paths_prefer_retained_diff_even_when_empty(
    coding, monkeypatch, retained
):
    from tests.test_coding_workspaces import write

    workspace = coding.create()
    write(workspace, "edited.txt", "edited\n")
    observations = dict(coding.manager._record(coding.task.task_id)["observations"])
    assert observations["edited_files"] == ["edited.txt"]
    if retained is not None:
        observations.update(changed_files=retained, diff_stat="retained stat")
        coding.repository.update(coding.task.task_id, observations=observations)

    def no_diff(*args, **kwargs):
        pytest.fail("Cached summary must not compute a diff")

    monkeypatch.setattr(coding.manager, "diff", no_diff)
    report = coding.manager.get(coding.task.task_id, inspect_current=False)
    assert report.changed_files == tuple(
        retained if retained is not None else ["edited.txt"]
    )
    assert report.diff_stat == ("retained stat" if retained is not None else "")


def test_active_cached_edited_paths_respect_existing_limit(
    coding, database, monkeypatch
):
    coding.config = coding.config.model_copy(
        update={"limits": CodingLimits(max_changed_files=2)}
    )

    class PausedProvider(Provider):
        def __init__(self):
            super().__init__(
                [
                    (
                        "write_file",
                        {"path": name, "content": "edited\n", "expected_sha256": None},
                    )
                    for name in ("first.txt", "second.txt", "over-limit.txt")
                ]
            )
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, worker, request):
            if len(self.requests) == 3:
                self.entered.set()
                await self.release.wait()
            return await super().generate(worker, request)

    async def execute():
        provider = PausedProvider()
        app, _ = application(coding, database, provider)
        async with client_for(app) as (client, _):
            task = submit(app, coding)
            await asyncio.wait_for(provider.entered.wait(), timeout=5)
            assert app.tasks.get_task(task.task_id).state == "running"
            observations = app.coding._record(task.task_id)["observations"]
            assert "changed_files" not in observations
            assert observations["edited_files"] == ["first.txt", "second.txt"]
            assert "diff_stat" not in observations
            calls = []
            original = app.coding.diff

            def inspect(*args, **kwargs):
                calls.append(True)
                return original(*args, **kwargs)

            monkeypatch.setattr(app.coding, "diff", inspect)

            # Fail all workspace filesystem entrypoints if summaries try to read.
            def no_workspace_read(*args, **kwargs):
                pytest.fail("Cached summary must not read the worktree")

            with monkeypatch.context() as reads:
                reads.setattr(app.coding, "_worktree_root", no_workspace_read)
                reads.setattr(app.coding, "_registration", no_workspace_read)
                report = app.get_coding_summary(task_id=task.task_id)
                assert report.changed_files == ("first.txt", "second.txt")
                assert (
                    len(report.changed_files) == coding.config.limits.max_changed_files
                )
                assert report.diff_stat == ""
                for path in ("/companion", f"/companion/tasks/{task.task_id}"):
                    body = (await client.get(path)).text
                    assert "first.txt, second.txt" in body
                    assert "Observed file paths" in body
                    assert "recorded edit attempts" in body
                    assert "No retained file observation" not in body
                    assert "No diff stat observed" in body
            assert calls == []
            # Only the explicit route inspects/verifies the current workspace.
            response = await client.get(f"/companion/tasks/{task.task_id}/diff")
            assert response.status_code == 200 and calls == [True]
            assert (
                "first.txt" in response.text and "over-limit.txt" not in response.text
            )
            assert (
                app.coding.get(task.task_id, inspect_current=False).changed_files
                == report.changed_files
            )
            app.cancel_task(task_id=task.task_id)
            provider.release.set()
            await app.tasks.wait_task(task.task_id)

    asyncio.run(execute())
