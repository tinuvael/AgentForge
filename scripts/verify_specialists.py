"""Offline installed-package acceptance: fake image -> synthetic mesh -> restart.

No GPU, network, inference endpoint, ComfyUI or Blender installation is used.
"""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy.engine import URL

from agentforge.application.access import ClientApplication, ClientPolicy, ProjectGrant
from agentforge.application.service import Application
from agentforge.db.database import create_database_engine
from agentforge.db.migrate import upgrade_database
from agentforge.gateway.server import GatewayConfig
from agentforge.mcp.server import create_server
from agentforge.specialists.models import SpecialistConfig, SpecialistTool
from agentforge.workers.config import WorkersConfig


async def acceptance(root):
    url = URL.create("sqlite", database=str(root / "specialists.db"))
    upgrade_database(url)
    private = root / "artifacts"
    private.mkdir(mode=0o700)
    project_root = root / "project"
    project_root.mkdir()
    config = SpecialistConfig(
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

    def factory():
        return Application(
            create_database_engine(url),
            WorkersConfig(workers=[]),
            specialist_config=config,
            providers={},
        )

    app = factory()
    project = app.projects.register_project("Synthetic acceptance", project_root)
    tools = (
        "list_specialist_tools",
        "submit_tool_job",
        "get_tool_job",
        "cancel_tool_job",
        "list_artifacts",
        "inspect_artifact",
        "retrieve_artifact",
    )
    policy = ClientPolicy(
        client_id="demo",
        token_sha256="0" * 64,
        tools=tools,
        projects=(
            ProjectGrant(project_id=project.id, specialists=("image", "blender")),
        ),
    )
    access = ClientApplication(app, lambda: policy, GatewayConfig())
    await app.start()
    evidence = []
    try:
        server = create_server(
            lambda: app, borrowed=True, client_access=lambda _: access
        )
        async with create_connected_server_and_client_session(server) as sdk:
            previous = []
            for tool in ("image", "blender"):
                arguments = dict(
                    project_id=str(project.id),
                    tool_id=tool,
                    request_id=str(uuid4()),
                    idempotency_key=uuid4().hex,
                    input_artifact_ids=previous,
                )
                response = await sdk.call_tool("submit_tool_job", arguments)
                assert not response.isError, response
                job_id = response.structuredContent["tool_job_id"]
                while True:
                    app.tasks._changed.clear()
                    status = await sdk.call_tool(
                        "get_tool_job", {"tool_job_id": job_id}
                    )
                    assert not status.isError
                    job = status.structuredContent
                    if job["state"] in {
                        "completed",
                        "failed",
                        "cancelled",
                        "interrupted",
                    }:
                        break
                    await app.tasks._changed.wait()
                assert job["state"] == "completed", job
                duplicate = await sdk.call_tool("submit_tool_job", arguments)
                assert duplicate.structuredContent["tool_job_id"] == job_id
                previous = job["output_artifact_ids"]
                response = await sdk.call_tool(
                    "inspect_artifact",
                    {"project_id": str(project.id), "artifact_id": previous[0]},
                )
                assert not response.isError
                descriptor = response.structuredContent
                assert descriptor["simulated"] and descriptor["status"] == "ready"
                chunk = await sdk.call_tool(
                    "retrieve_artifact",
                    {"project_id": str(project.id), "artifact_id": previous[0]},
                )
                data = base64.b64decode(chunk.structuredContent["data_base64"])
                assert hashlib.sha256(data).hexdigest() == descriptor["sha256"]
                evidence.append(descriptor)
    finally:
        await app.close()
    restarted = factory()
    await restarted.start()
    try:
        for artifact in evidence:
            assert (
                restarted.inspect_artifact(
                    project_id=project.id, artifact_id=UUID(artifact["artifact_id"])
                ).model_dump(mode="json")
                == artifact
            )
    finally:
        await restarted.close()
    print(
        json.dumps(
            {
                "scenario": "synthetic image-to-mesh exchange",
                "restart_verified": True,
                "real_comfyui_blender_tested": False,
                "artifacts": evidence,
            },
            indent=2,
        )
    )


def main():
    with TemporaryDirectory(prefix="agentforge-specialists-") as directory:
        asyncio.run(acceptance(Path(directory).resolve()))


if __name__ == "__main__":
    main()
