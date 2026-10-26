"""Offline installed-wheel acceptance for real adapter code with service fixtures."""

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy.engine import URL

from agentforge.application.service import Application
from agentforge.coding.process import validation_process
from agentforge.db.database import create_database_engine
from agentforge.db.migrate import upgrade_database
from agentforge.specialists.adapters import FakeBlender, FakeComfyUI
from agentforge.specialists.comfyui import ComfyUI
from agentforge.specialists.models import (
    TERMINAL,
    Parameters,
    SpecialistConfig,
    SpecialistTool,
)
from agentforge.workers.config import WorkersConfig


async def acceptance(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("artifacts", "staging", "project"):
        (root / name).mkdir(mode=0o700)
    url = URL.create("sqlite", database=str(root / "adapters.db"))
    upgrade_database(url)
    executable = Path(sys.executable).resolve()
    workflow = {
        "1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "approved"}}
    }
    config = SpecialistConfig(
        artifact_directory=root / "artifacts",
        tools=(
            SpecialistTool(
                tool_id="image",
                kind="comfyui_image",
                adapter="comfyui",
                connection_id="mock-http",
                authorized_host="configured_http",
                enabled=True,
                simulated=False,
                availability="configured_untested",
                output_formats=("png",),
                comfyui=dict(
                    base_url="http://127.0.0.1:8188",
                    workflow_json=json.dumps(workflow),
                    output_node="1",
                ),
            ),
            SpecialistTool(
                tool_id="mesh",
                kind="blender_scene",
                adapter="blender",
                connection_id="fixture-executable",
                authorized_host="application_host",
                enabled=True,
                simulated=False,
                availability="configured_untested",
                input_formats=("png",),
                output_formats=("gltf",),
                blender=dict(
                    executable=executable,
                    executable_sha256=hashlib.sha256(
                        executable.read_bytes()
                    ).hexdigest(),
                    staging_directory=root / "staging",
                ),
            ),
        ),
    )
    (image,) = await FakeComfyUI().run(Parameters(), ())
    (mesh,) = await FakeBlender().run(Parameters(), ())
    document = json.loads(mesh.data)
    binary = base64.b64decode(document["buffers"][0]["uri"].split(",", 1)[1])
    document["buffers"][0]["uri"] = "scene.bin"
    fixture = root / "fixture.py"
    fixture.write_text(
        "from pathlib import Path\nPath('scene.gltf').write_bytes("
        + repr(json.dumps(document).encode())
        + ")\nPath('scene.bin').write_bytes("
        + repr(binary)
        + ")\n",
        encoding="utf-8",
    )
    receipt, submitted = uuid4(), []

    def handle(request):
        if request.url.path == "/prompt":
            submitted.append(json.loads(request.content))
            return httpx.Response(200, json={"prompt_id": str(receipt)})
        if request.url.path.startswith("/history/"):
            prefix = submitted[0]["prompt"]["1"]["inputs"]["filename_prefix"]
            return httpx.Response(
                200,
                json={
                    str(receipt): {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "1": {
                                "images": [
                                    {
                                        "filename": prefix + "_00001_.png",
                                        "type": "output",
                                        "subfolder": "",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        assert request.url.path == "/view"
        return httpx.Response(200, content=image.data)

    constructor = ComfyUI.__init__

    def initialize(self, tool, context):
        constructor(self, tool, context, transport=httpx.MockTransport(handle))

    async def process(argv, **options):
        assert Path(argv[-1]).name == "blender_script.py"
        assert argv[1:4] == ["--background", "--factory-startup", "--disable-autoexec"]
        return await validation_process([str(executable), str(fixture)], **options)

    def application():
        return Application(
            create_database_engine(url),
            WorkersConfig(workers=[]),
            providers={},
            specialist_config=config,
        )

    evidence = []
    with (
        patch.object(ComfyUI, "__init__", initialize),
        patch("agentforge.specialists.blender.validation_process", process),
    ):
        app = application()
        project = app.projects.register_project("Adapter fixtures", root / "project")
        await app.start()
        try:
            inputs = ()
            for tool in ("image", "mesh"):
                fields = dict(
                    project_id=project.id,
                    tool_id=tool,
                    input_artifact_ids=inputs,
                    parameters={"operation": "reference_blockout"}
                    if tool == "mesh"
                    else {},
                    request_id=uuid4(),
                    idempotency_key=uuid4().hex,
                )
                job = app.submit_tool_job(**fields)
                while job.state not in TERMINAL:
                    await asyncio.sleep(0.01)
                    job = app.get_tool_job(tool_job_id=job.tool_job_id)
                assert job.state == "completed", job
                assert job.execution.phase == "outputs_validated" and not job.simulated
                assert app.submit_tool_job(**fields) == job
                inputs = job.output_artifact_ids
                artifact = app.inspect_artifact(
                    project_id=project.id, artifact_id=inputs[0]
                )
                assert not artifact.simulated and artifact.status == "ready"
                evidence.append(artifact)
            assert len(submitted) == 1 and not list((root / "staging").iterdir())
        finally:
            await app.close()
    app = application()
    await app.start()
    try:
        for artifact in evidence:
            assert (
                app.inspect_artifact(
                    project_id=project.id, artifact_id=artifact.artifact_id
                )
                == artifact
            )
    finally:
        await app.close()
    print(
        json.dumps(
            {
                "scenario": "real adapter contracts with HTTP/executable fixtures",
                "restart_verified": True,
                "physical_applications_tested": False,
            }
        )
    )


def main():
    with TemporaryDirectory(prefix="agentforge-adapter-smoke-") as directory:
        asyncio.run(acceptance(Path(directory).resolve()))


if __name__ == "__main__":
    main()
