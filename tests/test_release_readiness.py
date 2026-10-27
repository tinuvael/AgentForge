"""Release regressions at the validator and browser public boundaries."""

import asyncio
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from agentforge.coding.config import ValidationCommand, load_coding
from agentforge.director.models import WorkflowBinding
from agentforge.workflows.models import Definition, InferenceStep
from tests.test_coding_validation import run_validation
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_coding_workspaces import write
from tests.test_director import fixture
from tests.test_director_web import browser, new, owner, submission
from tests.test_mcp import setup as setup_fixture
from tests.test_workflows import until

setup = setup_fixture
coding = coding_fixture


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
def test_terminal_workflow_can_be_cleared(setup, tmp_path, state):
    async def scenario():
        async with fixture(setup, tmp_path) as f:
            item = f.director.create("cli")
            item.workflows.append(
                WorkflowBinding(
                    workflow_id=UUID(int=1), project_id=setup.project.id, state=state
                )
            )
            f.director.journal.save(item)
            f.director.clear(item)
            assert not f.director.journal.path(item.id).exists()
            assert setup.app.status().available

    asyncio.run(scenario())


def test_documented_mac_worker_has_explicit_private_host_trust(tmp_path, registry):
    from agentforge.workers.config import load_workers
    from agentforge.workers.policy import WorkerPolicy

    document = Path(__file__).resolve().parents[1] / "docs/macos.md"
    path = tmp_path / "workers.toml"
    path.write_text(document.read_text().split("```toml")[1].split("```")[0])
    config = load_workers(str(path))
    # Registration's default local-only policy must accept this declared private
    # Windows endpoint. No live probe or data export is needed to verify the grant.
    root = tmp_path / "local project"
    root.mkdir()
    project = registry.register_project("Local APFS project", root)
    policy = WorkerPolicy(config, registry)
    assert policy.trust(config.workers[0]) == "local"
    policy.check(project.id, config.workers[0])


@pytest.mark.parametrize("state", ["approval", "paused", "recovery_required"])
def test_chat_clear_retains_unfinished_workflow_recovery(setup, tmp_path, state):
    async def scenario():
        async with fixture(setup, tmp_path) as f:
            setup.app.workflows.client_access = lambda _: f.access
            async with browser(setup, f) as (client, _):
                path = await new(client)
                workflow = setup.app.submit_workflow(
                    definition=Definition(
                        project_id=setup.project.id,
                        steps=(
                            InferenceStep(
                                step_id="review",
                                kind="inference",
                                agent_id="repo_explorer",
                                worker_id="home-i5",
                                request="Read source.py",
                                intended_effect="Read after human review",
                                approval_required=True,
                            ),
                        ),
                    ),
                    idempotency_key="retained",
                    client_id="local-director",
                )
                workflow = await until(setup.app, workflow.workflow_id)
                assert workflow.state == "approval" and not setup.app.tasks.history()
                if state == "paused":
                    workflow = setup.app.pause_workflow(
                        workflow_id=workflow.workflow_id
                    )
                elif state == "recovery_required":
                    # Durable post-crash state; clear must retain this identity too.
                    workflow = setup.app.workflows.repository.save(
                        workflow.model_copy(update={"state": state})
                    )
                item = f.director.load(UUID(path.split("/")[-1]), owner(client))
                item.workflows.append(
                    WorkflowBinding(
                        workflow_id=workflow.workflow_id,
                        project_id=setup.project.id,
                        state=state,
                    )
                )
                f.director.journal.save(item)
                response = await client.post(
                    path + "/clear",
                    data=submission(client, f.director, path, confirm="clear"),
                )
                assert response.status_code == 409
                retained = f.director.load(item.id, owner(client))
                assert retained.workflows[0].workflow_id == workflow.workflow_id
                assert (
                    setup.app.get_workflow(workflow_id=workflow.workflow_id).state
                    == state
                )
                assert not setup.app.tasks.history()

    asyncio.run(scenario())


def test_shipped_pytest_validator_certifies_first_run_without_changing_inputs(
    coding, tmp_path
):
    # A real copied venv executable preserves its package context and passes the
    # no-symlink executable policy. Reuse only the test runner's installed tools.
    venv = tmp_path / "validator venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--copies", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    purelib = venv / (
        "Lib/site-packages" if os.name == "nt" else f"lib/python{version}/site-packages"
    )
    (purelib / "audit-tools.pth").write_text(sysconfig.get_path("purelib") + "\n")
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    example = load_coding(
        str(Path(__file__).resolve().parents[1] / "config/coding.example.toml")
    ).validations["pytest"]
    coding.manager.config = coding.manager.config.model_copy(
        update={
            "validations": {
                "check": ValidationCommand(argv=(str(python), *example.argv[1:]))
            }
        }
    )
    workspace = coding.create()
    write(workspace, "test_contract.py", "def test_check():\n    assert 2 + 2 == 4\n")
    result = asyncio.run(run_validation(workspace))
    assert result["exit_code"] == 0, result
    assert coding.manager.get(coding.task.task_id).validators_succeeded(("check",))
    assert not (workspace.tool_root / ".pytest_cache").exists()
    assert not (workspace.tool_root / "__pycache__").exists()


def test_director_cli_reports_workflow_identity(capsys):
    from agentforge.director.cli import display
    from agentforge.director.models import Conversation

    workflow = WorkflowBinding(
        workflow_id=UUID(int=1), project_id=UUID(int=2), state="approval"
    )
    item = Conversation(owner="cli", gateway_identity="local", workflows=[workflow])
    display(item)
    output = json.loads(capsys.readouterr().out)
    assert output["workflows"] == [workflow.model_dump(mode="json")]


def test_director_cli_clear_preserves_workflow_identity(setup, tmp_path, monkeypatch):
    from agentforge.director import cli
    from agentforge.director.client import DirectorError

    async def scenario():
        async with fixture(setup, tmp_path) as f:
            item = f.director.create("cli")
            item.workflows.append(
                WorkflowBinding(
                    workflow_id=UUID(int=1),
                    project_id=setup.project.id,
                    state="approval",
                )
            )
            f.director.journal.save(item)

            class BorrowedDirector:
                async def __aenter__(self):
                    return f.director

                async def __aexit__(self, *_):
                    pass

            monkeypatch.setattr(cli, "load_config", lambda _: None)
            monkeypatch.setattr(cli, "Director", lambda _: BorrowedDirector())
            with pytest.raises(DirectorError, match="recover_or_reject_before_clear"):
                await cli.execute(
                    SimpleNamespace(
                        operation="clear", config="fixture", session=item.id
                    )
                )
            assert f.director.load(item.id, "cli").workflows == item.workflows

    asyncio.run(scenario())
