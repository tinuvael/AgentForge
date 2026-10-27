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
from types import SimpleNamespace
from uuid import UUID

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from verify_gateway import call, client, free_port, listener

from agentforge.application.access import ClientPolicy, Policies, ProjectGrant
from agentforge.application.service import Application
from agentforge.coding.config import CodingConfig, ValidationCommand
from agentforge.gateway.auth import CredentialStore, fingerprint, save_policies
from agentforge.gateway.server import GatewayConfig, create_gateway
from agentforge.mcp.server import TOOL_CONTRACTS
from agentforge.specialists.models import SpecialistConfig, SpecialistTool
from agentforge.web.combined import CompanionConfig
from agentforge.workflows.models import (
    Definition,
    InferenceStep,
    SpecialistStep,
    StepArtifact,
    TextReference,
)


async def acceptance(root):
    async with AsyncExitStack() as services:
        await scenario(root, services)


async def scenario(root, services):
    def operator(*arguments):
        result = subprocess.run(
            [sys.executable, "-m", "agentforge", *map(str, arguments)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    url = "sqlite:///" + (root / "workflows.db").as_posix()
    operator("db", "upgrade", "--database-url", url)
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
    entered, release = asyncio.Event(), asyncio.Event()

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

    async def worker_endpoint(request):
        body = await request.body()
        if any(
            m.get("content") == "cancel fixture" for m in json.loads(body)["messages"]
        ):
            entered.set()
            await release.wait()
        return JSONResponse(
            inference(
                httpx.Request("POST", "http://fixture/api/chat", content=body)
            ).json()
        )

    endpoint = await services.enter_async_context(
        listener(
            Starlette(routes=[Route("/api/chat", worker_endpoint, methods=["POST"])])
        )
    )
    workers_path, coding_path, specialists_path = (
        root / "workers.toml",
        root / "coding.toml",
        root / "specialists.toml",
    )
    workers_path.write_text(
        '[[workers]]\nid="local"\nprovider="ollama"\nmodel="scripted"\n'
        f'endpoint={json.dumps(endpoint)}\nsupports_tools=true\ntrust="local"\n'
    )
    coding_path.write_text(
        f"workspace_parent={json.dumps(str(worktrees))}\n"
        f"git_executable={json.dumps(str(git))}\n[validations.check]\n"
        f"argv={json.dumps(coding.validations['check'].argv)}\n"
    )
    specialists_path.write_text(
        f"artifact_directory={json.dumps(str(private))}\n"
        + "\n".join(
            "[[tools]]\n"
            + "\n".join(
                f"{key}={json.dumps(value)}"
                for key, value in tool.model_dump(mode="json").items()
                if key
                in {
                    "tool_id",
                    "kind",
                    "adapter",
                    "connection_id",
                    "enabled",
                    "input_formats",
                    "output_formats",
                }
            )
            for tool in specialists.tools
        )
    )
    operator("worker", "config-check", "--workers", workers_path)
    operator("worker", "list", "--workers", workers_path)
    operator("agent", "list", "--coding", coding_path)

    def factory():
        return Application.from_config(
            database_url=url,
            workers_path=str(workers_path),
            specialists_path=str(specialists_path),
            coding_path=str(coding_path),
            concurrency=1,
        )

    registered = operator(
        "project", "add", repo, "--name", "Workflow fixture", "--database-url", url
    )
    project = SimpleNamespace(id=UUID(registered["registration"]["project_id"]))
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
        agents=("general_agent", "repo_explorer", "coder"),
        coding_submit=True,
    )
    auth = root / "auth.json"
    save_policies(auth, Policies(clients=(policy,)), expected=None)
    application = factory()
    gateway_port = free_port()
    companion_port = free_port()
    gateway = create_gateway(
        lambda: application,
        CredentialStore(auth),
        GatewayConfig(port=gateway_port),
        companion=CompanionConfig(port=companion_port),
    )
    async with AsyncExitStack() as stack:
        base = await stack.enter_async_context(listener(gateway, port=gateway_port))
        async with client(base + "/mcp", token) as (sdk, _):
            assert (await call(sdk, "list_projects"))["projects"][0][
                "project_id"
            ] == str(project.id)
            assert (await call(sdk, "list_workers"))["workers"][0][
                "worker_id"
            ] == "local"
            assert {
                a["agent_id"] for a in (await call(sdk, "list_agents"))["agents"]
            } == {"general_agent", "repo_explorer", "coder"}

            async def task(request):
                return await call(
                    sdk,
                    "delegate_task",
                    dict(
                        project_id=str(project.id),
                        agent_id="general_agent",
                        worker_id="local",
                        task=request,
                    ),
                )

            async def terminal(identity):
                async with asyncio.timeout(10):
                    while True:
                        value = await call(sdk, "get_task", {"task_id": identity})
                        if value["state"] not in {"queued", "running"}:
                            return value
                        await asyncio.sleep(0.01)

            first = await task("Read source.py")
            assert (await terminal(first["task_id"]))["state"] == "completed"
            cancelled = await task("cancel fixture")
            async with asyncio.timeout(5):
                await entered.wait()
            assert (await call(sdk, "watch_task", {"task_id": cancelled["task_id"]}))[
                "state"
            ] == "running"
            await call(sdk, "cancel_task", {"task_id": cancelled["task_id"]})
            release.set()
            assert (await terminal(cancelled["task_id"]))["state"] == "cancelled"
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
            assert len(application.tasks.history()) == 3
            scope = application.workflows.review_scope(
                UUID(review["workflow_id"]), "code"
            )
            assert "repository_write" in scope.effects
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{companion_port}", trust_env=False
            ) as browser:
                path = f"/workflows/{scope.workflow_id}"
                assert (await browser.get(path)).status_code == 200
                review_path = path + f"/review/code/{scope.attempt.scope_hash}/approve"
                fields = {
                    "csrf": browser.cookies.get("agentforge_csrf"),
                    "confirm": "yes",
                }
                assert (await browser.post(review_path, data=fields)).status_code == 303
                assert (await browser.post(review_path, data=fields)).status_code == 409
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
            config = root / "director.toml"
            config.write_text(
                f"gateway_url={json.dumps(base + '/mcp')}\n"
                'token_env="AGENTFORGE_WORKFLOW_FIXTURE_TOKEN"\n'
                f'journal={json.dumps(str(root / "journal"))}\nmodel="director"\n'
                f'[inference]\nid="director"\ntype="ollama"\nbase_url={json.dumps(endpoint)}\ntrust="local"\n'
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "agentforge",
                "director",
                "run",
                "--config",
                str(config),
                "Run the explicit synthetic workflow and observe the output.",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(process.communicate(), 30)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            assert process.returncode == 0, (out, err)
            item = json.loads(out)
            assert item["state"] == "completed", item
            assert (
                len(item["workflows"]) == 1
                and item["workflows"][0]["state"] == "completed"
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
        assert len(restarted.tasks.history()) == 4
        assert restarted.get_task(task_id=UUID(first["task_id"])).state == "completed"
        assert (
            restarted.get_task(task_id=UUID(cancelled["task_id"])).state == "cancelled"
        )
    finally:
        await restarted.close()
    print(
        "Workflow acceptance: artifact chain, isolated coder + named validation, "
        "native HTTP Local Director, concurrency one, provenance and restart: OK"
    )


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        asyncio.run(acceptance(Path(directory).resolve()))
