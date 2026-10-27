"""Required validation certifies the final isolated workspace, not history."""

import asyncio
import sys
from pathlib import Path

import pytest

from agentforge.coding.config import ValidationCommand
from agentforge.workflows.models import Definition, InferenceStep
from tests.test_coding_tasks import Provider, application
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_coding_workspaces import primary_state, sha
from tests.test_workflows import until

coding = coding_fixture


@pytest.mark.parametrize(
    "codes,edit_after,expected",
    [
        ([0, 1], None, "failed"),
        ([1, 0], 0, "completed"),
        ([0], 0, "failed"),
        ([0], None, "completed"),
        ([0], "after_task", "failed"),
    ],
    ids=[
        "success-failure",
        "failure-edit-success",
        "success-edit",
        "success",
        "changed-after-task",
    ],
)
def test_required_validator_final_invocation_and_workspace(
    coding, database, tmp_path, codes, edit_after, expected
):
    counter = tmp_path / "validator-invocations"
    script = (
        "from pathlib import Path; import sys; "
        f"p=Path({str(counter)!r}); n=int(p.read_text()) if p.exists() else 0; "
        f"p.write_text(str(n+1)); sys.exit({codes!r}[n])"
    )
    coding.config = coding.config.model_copy(
        update={
            "validations": {
                "check": ValidationCommand(
                    argv=(str(Path(sys.executable).resolve()), "-c", script)
                )
            }
        }
    )
    operations = []
    for index in range(len(codes)):
        operations.append(("run_validation", {"name": "check"}))
        if edit_after == index:
            operations.append(
                (
                    "write_file",
                    {
                        "path": "source.py",
                        "content": "modified\n",
                        "expected_sha256": sha("original\n"),
                    },
                )
            )
    provider = Provider(operations)
    before = primary_state(coding.root)

    async def scenario():
        app, _ = application(coding, database, provider, concurrency=1)
        provider.app = None
        if edit_after == "after_task":
            attach = app.tasks._repository.attach_coding_result

            def change_after_snapshot(task_id, result):
                attach(task_id, result)
                _, root = app.coding._paths(app.coding._record(task_id))
                (root / "source.py").write_text("changed after terminal snapshot\n")

            app.tasks._repository.attach_coding_result = change_after_snapshot
        await app.start()
        try:
            value = app.submit_workflow(
                definition=Definition(
                    project_id=coding.project.id,
                    steps=(
                        InferenceStep(
                            step_id="code",
                            kind="inference",
                            agent_id="coder",
                            worker_id="one",
                            request="Edit and run the named check",
                            intended_effect="Isolated edit",
                            required_validators=("check",),
                        ),
                        InferenceStep(
                            step_id="successor",
                            kind="inference",
                            agent_id="general_agent",
                            worker_id="one",
                            depends_on=("code",),
                            request="Report accepted result",
                            intended_effect="Report",
                        ),
                    ),
                ),
                idempotency_key="validation-review",
            )
            review = await until(app, value.workflow_id)
            assert review.state == "approval"
            app.workflows.review(
                workflow_id=value.workflow_id,
                step_id="code",
                scope_hash=review.steps[0].attempts[0].scope_hash,
            )
            final = await until(
                app, value.workflow_id, ("completed", "blocked", "failed")
            )
            child = app.get_task(task_id=final.steps[0].attempts[0].task_id)
            assert child.state == "completed"
            assert [v.exit_code for v in child.coding_result.validation_runs] == codes
            assert child.coding_result.write_calls == (edit_after == 0)
            assert final.state == expected
            if expected == "failed":
                assert final.steps[0].state == "failed"
                assert final.steps[0].failure_category == "validation_required"
                assert not final.steps[1].attempts
                assert len(app.tasks.history()) == 1
            else:
                assert final.steps[0].state == final.steps[1].state == "completed"
        finally:
            await app.close()

    asyncio.run(scenario())
    assert primary_state(coding.root) == before
