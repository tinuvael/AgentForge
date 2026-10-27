"""Offline workflow acceptance, also executed against the clean installed wheel.

Synthetic PNG -> synthetic glTF, and source exploration -> isolated coder with
named validation. A real loopback scripted Ollama Director calls the authenticated
Gateway, observes the workflow and reports real child/artifact identities.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from verify_gateway import call, client, free_port, listener

from agentforge.application.access import ClientPolicy, Policies, ProjectGrant
from agentforge.application.service import Application
from agentforge.coding.config import CodingConfig, ValidationCommand
from agentforge.core.worker import Worker
from agentforge.db.database import create_database_engine
from agentforge.db.migrate import upgrade_database
from agentforge.director.config import DirectorConfig
from agentforge.director.core import Director
from agentforge.gateway.auth import CredentialStore, fingerprint, save_policies
from agentforge.gateway.server import GatewayConfig, create_gateway
from agentforge.mcp.server import TOOL_CONTRACTS
from agentforge.providers.ollama import OllamaProvider
from agentforge.specialists.models import SpecialistConfig, SpecialistTool
from agentforge.workers.config import WorkersConfig
from agentforge.workflows.models import (
    Definition,
    InferenceStep,
    SpecialistStep,
    StepArtifact,
    TextReference,
)


async def acceptance(root):
    url = "sqlite:///" + (root / "workflows.db").as_posix()
    upgrade_database(url)
    repo = root / "project"
    repo.mkdir()
    (repo / "source.py").write_text("original\n", encoding="utf-8")
    git = Path(shutil.which("git")).resolve()

    def git_run(*args):
        return subprocess.run(
            [str(git), "-C", str(repo), *args], check=True, capture_output=True
        ).stdout

    git_run("init")
    git_run(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "add",
        "source.py",
    )
    git_run(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    primary = (
        git_run("rev-parse", "HEAD"),
        git_run("status", "--porcelain"),
        (repo / "source.py").read_bytes(),
    )
    private, worktrees = root / "artifacts", root / "worktrees"
    private.mkdir(mode=0o700)
    worktrees.mkdir(mode=0o700)
    specialists = SpecialistConfig(
        artifact_directory=private,
        tools=(
            SpecialistTool(
                tool_id="image",
                kind="comfyui_image",
                adapter="fake_comfyui",
                connection_id="fake-image",
                enabled=True,
                output_formats=("png",),
            ),
            SpecialistTool(
                tool_id="blender",
                kind="blender_scene",
                adapter="fake_blender",
                connection_id="fake-blender",
                enabled=True,
                input_formats=("png",),
                output_formats=("gltf",),
            ),
        ),
    )
    coding = CodingConfig(
        workspace_parent=worktrees,
        git_executable=git,
        validations={
            "check": ValidationCommand(
                argv=(
                    str(Path(sys.executable).resolve()),
                    "-c",
                    "from pathlib import Path; "
                    "assert Path('source.py').read_text() == 'edited\\n'",
                )
            )
        },
    )
    worker_requests = []

    def inference(request):
        body = json.loads(request.content)
        worker_requests.append(body)
        messages = body["messages"]
        tools = {t["function"]["name"] for t in body["tools"]}
        # Ollama tool messages may omit the tool name; count the fixed sequence.
        count = len([m for m in messages if m["role"] == "tool"])
        if "write_file" in tools:
            operations = [
                (
                    "write_file",
                    {
                        "path": "source.py",
                        "content": "edited\n",
                        "expected_sha256": __import__("hashlib")
                        .sha256(b"original\n")
                        .hexdigest(),
                    },
                ),
                ("run_validation", {"name": "check"}),
            ]
        else:
            operations = [("read_file", {"path": "source.py"})]
        if count < len(operations):
            name, arguments = operations[count]
            message = {
                "content": "",
                "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
            }
        else:
            message = {"content": "source.py:1 inspected; named validation passed."}
        return httpx.Response(
            200, json={"model": "scripted", "done": True, "message": message}
        )

    provider = OllamaProvider(transport=httpx.MockTransport(inference))
    workers = WorkersConfig(
        workers=[
            Worker(
                id="local",
                provider="ollama",
                model="scripted",
                endpoint="http://127.0.0.1:1",
                supports_tools=True,
                trust="local",
            )
        ]
    )

    def factory():
        return Application(
            create_database_engine(url),
            workers,
            providers={"ollama": provider},
            specialist_config=specialists,
            coding_config=coding,
            concurrency=1,
        )

    setup = factory()
    project = setup.projects.register_project("Workflow fixture", repo)
    setup.database.dispose()
    image_plan = Definition(
        project_id=project.id,
        steps=(
            SpecialistStep(
                step_id="image",
                kind="specialist",
                tool_id="image",
                intended_effect="Synthetic PNG",
            ),
            SpecialistStep(
                step_id="mesh",
                kind="specialist",
                tool_id="blender",
                intended_effect="Synthetic mesh reference",
                depends_on=("image",),
                artifact_inputs=(StepArtifact(step_id="image", format="png"),),
            ),
        ),
    )
    code_plan = Definition(
        project_id=project.id,
        steps=(
            InferenceStep(
                step_id="explore",
                kind="inference",
                agent_id="repo_explorer",
                worker_id="local",
                request="Read source.py",
                intended_effect="Source analysis",
            ),
            InferenceStep(
                step_id="code",
                kind="inference",
                agent_id="coder",
                worker_id="local",
                request="Edit source.py to edited and run check",
                intended_effect="Isolated write with named check",
                depends_on=("explore",),
                text_inputs=(TextReference(step_id="explore", max_chars=200),),
                required_validators=("check",),
            ),
        ),
    )
    token = "workflow-fixture-" + "a" * 48
    policy = ClientPolicy(
        client_id="director",
        token_sha256=fingerprint(token),
        tools=tuple(t.name for t in TOOL_CONTRACTS),
        projects=(
            ProjectGrant(
                project_id=project.id,
                workers=("local",),
                specialists=("image", "blender"),
            ),
        ),
        agents=("repo_explorer", "coder"),
        coding_submit=True,
    )
    auth = root / "auth.json"
    save_policies(auth, Policies(clients=(policy,)), expected=None)
    application = factory()
    gateway_port = free_port()
    gateway = create_gateway(
        lambda: application, CredentialStore(auth), GatewayConfig(port=gateway_port)
    )
    async with AsyncExitStack() as stack:
        base = await stack.enter_async_context(listener(gateway, port=gateway_port))
        async with client(base + "/mcp", token) as (sdk, _):
            arguments = dict(
                definition=image_plan.model_dump(mode="json"), idempotency_key="images"
            )
            image_workflow = await call(sdk, "submit_workflow", arguments)
            duplicate = await call(sdk, "submit_workflow", arguments)
            assert duplicate["workflow_id"] == image_workflow["workflow_id"]

            async def observe(identity, expected):
                async with asyncio.timeout(15):
                    while True:
                        result = await call(
                            sdk, "get_workflow", {"workflow_id": identity}
                        )
                        if result["state"] in expected:
                            return result
                        assert result["state"] not in {
                            "failed",
                            "blocked",
                            "recovery_required",
                        }, result
                        await asyncio.sleep(0.01)

            image_workflow = await observe(image_workflow["workflow_id"], {"completed"})
            image, mesh = [s["attempts"][-1] for s in image_workflow["steps"]]
            metadata = await call(
                sdk,
                "inspect_artifact",
                {"project_id": str(project.id), "artifact_id": mesh["artifact_ids"][0]},
            )
            assert metadata["input_artifact_ids"] == image["artifact_ids"]
            assert metadata["tool_job_id"] == mesh["tool_job_id"]
            code_workflow = await call(
                sdk,
                "submit_workflow",
                dict(
                    definition=code_plan.model_dump(mode="json"),
                    idempotency_key="coding",
                ),
            )
            review = await observe(code_workflow["workflow_id"], {"approval"})
            assert len(application.tasks.history()) == 1
            scope = application.workflows.review_scope(
                UUID(review["workflow_id"]), "code"
            )
            assert "repository_write" in scope.effects
            application.workflows.review(
                workflow_id=scope.workflow_id,
                step_id="code",
                scope_hash=scope.attempt.scope_hash,
            )
            code_workflow = await observe(review["workflow_id"], {"completed"})
            task_id = code_workflow["steps"][-1]["attempts"][-1]["task_id"]
            result = await call(sdk, "get_coding_workspace", {"task_id": task_id})
            assert result["changed_files"] == ["source.py"]
            assert (
                result["validation_runs"][0]["name"] == "check"
                and result["validation_runs"][0]["exit_code"] == 0
            )
            assert primary == (
                git_run("rev-parse", "HEAD"),
                git_run("status", "--porcelain"),
                (repo / "source.py").read_bytes(),
            )
        # Native inference HTTP plus authenticated MCP, not discovery-only.
        director_requests = []

        async def director_endpoint(request):
            body = await request.json()
            director_requests.append(body)
            results = [
                json.loads(m["content"])
                for m in body["messages"]
                if m["role"] == "tool"
            ]
            if results:
                observed = results[-1]
                assert observed["state"] == "completed"
                assert observed["steps"][-1]["attempts"][-1]["artifact_ids"]
                message = {
                    "content": "Observed completed workflow " + observed["workflow_id"]
                }
            else:
                message = {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "submit_workflow",
                                "arguments": {
                                    "definition": image_plan.model_dump(mode="json"),
                                    "idempotency_key": "local-director",
                                },
                            }
                        }
                    ],
                }
            return JSONResponse({"model": "director", "done": True, "message": message})

        endpoint = await stack.enter_async_context(
            listener(
                Starlette(
                    routes=[Route("/api/chat", director_endpoint, methods=["POST"])]
                )
            )
        )
        os.environ["AGENTFORGE_WORKFLOW_FIXTURE_TOKEN"] = token
        try:
            config = DirectorConfig(
                gateway_url=base + "/mcp",
                token_env="AGENTFORGE_WORKFLOW_FIXTURE_TOKEN",
                journal=root / "journal",
                model="director",
                inference={
                    "id": "director",
                    "type": "ollama",
                    "base_url": endpoint,
                    "trust": "local",
                },
            )
            async with Director(config) as director:
                item = director.create("fixture")
                item = await director.run(
                    item.id,
                    "fixture",
                    "Run the explicit synthetic workflow and observe the output.",
                )
                assert item.state == "completed", item.error
                assert (
                    len(item.workflows) == 1 and item.workflows[0].state == "completed"
                )
                assert len(director_requests) == 2
                assert len(application.specialists.repository.jobs()) == 4
        finally:
            os.environ.pop("AGENTFORGE_WORKFLOW_FIXTURE_TOKEN", None)
    # Completed children and artifact provenance remain inspectable on restart.
    restarted = factory()
    await restarted.start()
    try:
        assert (
            restarted.get_workflow(workflow_id=UUID(code_workflow["workflow_id"])).state
            == "completed"
        )
        assert (
            restarted.inspect_artifact(
                project_id=project.id, artifact_id=UUID(mesh["artifact_ids"][0])
            ).status
            == "ready"
        )
        assert len(restarted.tasks.history()) == 2
    finally:
        await restarted.close()
    print(
        "Workflow acceptance: artifact chain, isolated coder + named validation, "
        "native HTTP Local Director, concurrency one, provenance and restart: OK"
    )


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        asyncio.run(acceptance(Path(directory).resolve()))
