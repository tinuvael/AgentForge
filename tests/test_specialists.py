"""Real storage/TaskEngine/Gateway contracts with deterministic offline adapters."""

import asyncio
import base64
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError

from agentforge.application.access import ClientApplication, ClientPolicy, ProjectGrant
from agentforge.application.errors import ServiceError
from agentforge.application.service import Application
from agentforge.core.policy import EgressPolicy
from agentforge.core.worker import Worker
from agentforge.db.database import create_database_engine
from agentforge.gateway.server import GatewayConfig
from agentforge.mcp.server import create_server
from agentforge.projects.errors import ProjectError
from agentforge.specialists.adapters import ADAPTERS, FakeComfyUI
from agentforge.specialists.filesystem import PrivateFiles
from agentforge.specialists.formats import PNG, validate
from agentforge.specialists.models import (
    TERMINAL,
    Artifact,
    Parameters,
    SpecialistConfig,
    SpecialistError,
    SpecialistTool,
    SubmitToolJob,
)
from agentforge.web.app import create_app
from agentforge.workers.config import WorkersConfig


def run(operation):
    async def bounded():
        async with asyncio.timeout(15):
            return await operation

    return asyncio.run(bounded())


def configuration(directory, **updates):
    return SpecialistConfig(
        artifact_directory=directory,
        tools=(
            SpecialistTool(
                tool_id="image",
                kind="comfyui_image",
                adapter="fake_comfyui",
                connection_id="fake-image",
                enabled=True,
                capabilities=("image",),
                output_formats=("png",),
            ),
            SpecialistTool(
                tool_id="blender",
                kind="blender_scene",
                adapter="fake_blender",
                connection_id="fake-blender",
                enabled=True,
                capabilities=("3d",),
                input_formats=("png",),
                output_formats=("gltf",),
            ),
        ),
        **updates,
    )


@pytest.fixture
def setup(database, registry, tmp_path):
    directory = tmp_path / "artifacts"
    directory.mkdir(mode=0o700)
    root = tmp_path / "project"
    root.mkdir()
    project = registry.register_project("Synthetic", root)
    other = tmp_path / "other"
    other.mkdir()
    second = registry.register_project("Other", other)
    config = configuration(directory)
    workers = WorkersConfig(
        workers=[
            Worker(
                id="text",
                provider="ollama",
                endpoint="http://localhost:1",
                model="fake",
                trust="local",
            )
        ]
    )

    def make(config=config, concurrency=2):
        return Application(
            create_database_engine(database[1]),
            workers,
            specialist_config=config,
            providers={},
            concurrency=concurrency,
        )

    return SimpleNamespace(
        make=make,
        config=config,
        directory=directory,
        project=project.id,
        second=second.id,
        url=database[1],
    )


def arguments(setup, *, tool="image", inputs=(), key=None, **fields):
    return dict(
        project_id=setup.project,
        tool_id=tool,
        input_artifact_ids=inputs,
        request_id=uuid4(),
        idempotency_key=key or uuid4().hex,
        **fields,
    )


async def wait(app, job):
    while True:
        app.tasks._changed.clear()
        current = app.get_tool_job(tool_job_id=job.tool_job_id)
        if current.state in TERMINAL:
            return current
        if not app.tasks.available:
            raise RuntimeError("executor unavailable")
        await app.tasks._changed.wait()


async def image(app, setup, **fields):
    job = await wait(app, app.submit_tool_job(**arguments(setup, **fields)))
    assert job.state == "completed", job.failure_category
    return job, app.inspect_artifact(
        project_id=setup.project, artifact_id=job.output_artifact_ids[0]
    )


def client(app, setup, identity="alpha", **updates):
    tools = (
        "list_specialist_tools",
        "submit_tool_job",
        "get_tool_job",
        "cancel_tool_job",
        "list_artifacts",
        "inspect_artifact",
        "retrieve_artifact",
        "describe_capabilities",
    )
    policy = ClientPolicy(
        client_id=identity,
        token_sha256=hashlib.sha256(identity.encode()).hexdigest(),
        tools=tools,
        projects=(
            ProjectGrant(project_id=setup.project, specialists=("image", "blender")),
        ),
        **updates,
    )
    return ClientApplication(app, lambda: policy, GatewayConfig())


def test_sequential_exchange_restart_hashes_provenance_and_bounded_retrieval(setup):
    async def execute():
        app = setup.make()
        await app.start()
        try:
            first, png = await image(app, setup)
            assert (first.kind, first.adapter, first.connection_id, first.trust) == (
                "comfyui_image",
                "fake_comfyui",
                "fake-image",
                "local",
            )
            second = await wait(
                app,
                app.submit_tool_job(
                    **arguments(setup, tool="blender", inputs=(png.artifact_id,))
                ),
            )
            assert second.state == "completed"
            mesh = app.inspect_artifact(
                project_id=setup.project, artifact_id=second.output_artifact_ids[0]
            )
            assert (
                mesh.geometry == "mesh"
                and mesh.printability == "not_assessed"
                and mesh.simulated
            )
            assert mesh.input_artifact_ids == (png.artifact_id,)
            assert mesh.tool_job_id == second.tool_job_id
            payload = bytearray()
            offset = 0
            while True:
                chunk = app.retrieve_artifact(
                    project_id=setup.project,
                    artifact_id=mesh.artifact_id,
                    offset=offset,
                    max_bytes=17,
                )
                payload.extend(base64.b64decode(chunk.data_base64))
                if chunk.next_offset is None:
                    break
                offset = chunk.next_offset
            assert hashlib.sha256(payload).hexdigest() == mesh.sha256
            assert json.loads(payload)["asset"]["version"] == "2.0"
            assert app.list_workers().workers[0].kind == "inference_worker"
            assert len(app.list_specialist_tools(project_id=setup.project).tools) == 2
        finally:
            await app.close()
        restarted = setup.make()
        await restarted.start()
        try:
            assert restarted.get_tool_job(tool_job_id=first.tool_job_id) == first
            assert (
                restarted.inspect_artifact(
                    project_id=setup.project, artifact_id=mesh.artifact_id
                )
                == mesh
            )
            assert (
                restarted.inspect_artifact(
                    project_id=setup.project, artifact_id=png.artifact_id
                )
                == png
            )
        finally:
            await restarted.close()

    run(execute())


def test_concurrent_duplicate_submissions_and_conflicts(setup):
    async def execute():
        app = setup.make()
        await app.start()
        try:
            fields = arguments(setup)

            async def submit():
                await asyncio.sleep(0)
                return app.submit_tool_job(**fields)

            jobs = await asyncio.gather(*(submit() for _ in range(12)))
            assert len({j.tool_job_id for j in jobs}) == 1
            assert (await wait(app, jobs[0])).state == "completed"
            assert len(app.list_artifacts(project_id=setup.project).artifacts) == 1
            with pytest.raises(SpecialistError, match="idempotency_conflict"):
                app.submit_tool_job(**{**fields, "parameters": {"seed": 1}})
            with pytest.raises(SpecialistError, match="idempotency_conflict"):
                app.submit_tool_job(**{**fields, "idempotency_key": "different"})
        finally:
            await app.close()

    run(execute())


def test_cross_project_cross_client_and_current_grants(setup):
    async def execute():
        app = setup.make()
        await app.start()
        try:
            alpha, beta = client(app, setup), client(app, setup, "beta")
            job = alpha.submit_tool_job(**arguments(setup))
            done = await wait(app, job)
            aid = done.output_artifact_ids[0]
            for operation in (beta.get_tool_job, beta.cancel_tool_job):
                with pytest.raises(ServiceError, match="access_denied"):
                    operation(tool_job_id=job.tool_job_id)
            for operation in (beta.inspect_artifact, beta.retrieve_artifact):
                with pytest.raises(ServiceError, match="access_denied"):
                    operation(project_id=setup.project, artifact_id=aid)
            assert not beta.list_artifacts(project_id=setup.project).artifacts
            with pytest.raises(ServiceError, match="access_denied"):
                beta.submit_tool_job(**arguments(setup, tool="blender", inputs=(aid,)))
            with pytest.raises(SpecialistError, match="artifact_not_found"):
                app.retrieve_artifact(project_id=setup.second, artifact_id=aid)
            shared = client(app, setup, "shared", read_shared=True)
            assert (
                shared.inspect_artifact(
                    project_id=setup.project, artifact_id=aid
                ).status
                == "ready"
            )
            with pytest.raises(ServiceError, match="access_denied"):
                shared.cancel_tool_job(tool_job_id=job.tool_job_id)
            denied = client(app, setup, "revoked")
            denied.principal = lambda: ClientPolicy(
                client_id="revoked", token_sha256="0" * 64
            )
            with pytest.raises(ServiceError, match="access_denied"):
                denied.retrieve_artifact(project_id=setup.project, artifact_id=aid)
            assert alpha.capabilities().specialist_operations
        finally:
            await app.close()

    run(execute())


def test_cloud_egress_denied_before_adapter_and_allowed_explicitly(setup):
    async def execute():
        config = setup.config.model_copy(
            update={
                "tools": tuple(
                    t.model_copy(update={"trust": "cloud"}) for t in setup.config.tools
                )
            }
        )
        app = setup.make(config)
        await app.start()
        try:
            assert not app.list_specialist_tools(project_id=setup.project).tools
            with pytest.raises(SpecialistError, match="specialist_policy_denied"):
                app.submit_tool_job(**arguments(setup))
            app.projects.set_egress_policy(
                setup.project,
                EgressPolicy(mode="cloud_allowed", allowed_connections=("fake-image",)),
            )
            await image(app, setup)
            with pytest.raises(SpecialistError, match="specialist_policy_denied"):
                app.submit_tool_job(**arguments(setup, tool="blender"))
        finally:
            await app.close()

    run(execute())


@pytest.mark.parametrize("stage", ["processing", "finalization", "queued"])
def test_cancellation_wins_and_no_outputs_are_published(setup, monkeypatch, stage):
    async def execute():
        entered, release = asyncio.Event(), asyncio.Event()

        class Blocking(FakeComfyUI):
            async def run(self, parameters, inputs):
                entered.set()
                await release.wait()
                return await super().run(parameters, inputs)

        monkeypatch.setitem(ADAPTERS, "fake_comfyui", Blocking)
        app = setup.make(concurrency=1)
        await app.start()
        try:
            job = app.submit_tool_job(**arguments(setup))
            if stage == "processing":
                await entered.wait()
                app.cancel_tool_job(tool_job_id=job.tool_job_id)
            elif stage == "queued":
                app.cancel_tool_job(tool_job_id=job.tool_job_id)
            else:
                original_write = app.specialists.files.write

                def cancel_after_write(aid, data):
                    original_write(aid, data)
                    app.cancel_tool_job(tool_job_id=job.tool_job_id)

                monkeypatch.setattr(app.specialists.files, "write", cancel_after_write)
                release.set()
            finished = await wait(app, job)
            assert finished.state == "cancelled" and finished.cancellation_requested_at
            assert not finished.output_artifact_ids
            assert not list(setup.directory.iterdir())
            assert app.tasks.available
        finally:
            await app.close()

    run(execute())


@pytest.mark.parametrize("fault", ["mime", "format", "oversize", "name", "timeout"])
def test_bad_adapter_output_and_timeout_fail_safely(setup, monkeypatch, fault):
    async def execute():
        class Bad(FakeComfyUI):
            async def run(self, parameters, inputs):
                if fault == "timeout":
                    await asyncio.Event().wait()
                (output,) = await super().run(parameters, inputs)
                changes = {
                    "mime": {"mime_type": "text/plain"},
                    "format": {"data": b"not-png"},
                    "oversize": {"data": b"x" * 2048},
                    "name": {"name": "../../outside.png"},
                }
                return (replace(output, **changes[fault]),)

        monkeypatch.setitem(ADAPTERS, "fake_comfyui", Bad)
        limits = setup.config.tools[0].limits.model_copy(
            update={"max_output_bytes": 1024, "runtime_seconds": 0.03}
        )
        cfg = setup.config.model_copy(
            update={
                "tools": (setup.config.tools[0].model_copy(update={"limits": limits}),)
            }
        )
        app = setup.make(cfg)
        await app.start()
        try:
            result = await wait(app, app.submit_tool_job(**arguments(setup)))
            assert result.state == "failed" and result.failure_category
            assert not result.output_artifact_ids and not list(
                setup.directory.iterdir()
            )
        finally:
            await app.close()

    run(execute())


def test_retention_cleanup_quota_and_expired_inputs(setup):
    async def execute():
        cfg = setup.config.model_copy(update={"max_artifacts": 1})
        app = setup.make(cfg)
        await app.start()
        try:
            _, artifact = await image(app, setup)
            blocked = await wait(app, app.submit_tool_job(**arguments(setup)))
            assert (
                blocked.state == "failed"
                and blocked.failure_category == "specialist_resource_limit"
            )
            with app.tasks.storage_access():
                assert (
                    app.specialists.cleanup(now=datetime.now(UTC) + timedelta(days=30))
                    == 1
                )
                assert (
                    app.specialists.cleanup(now=datetime.now(UTC) + timedelta(days=30))
                    == 0
                )
            with pytest.raises(SpecialistError, match="artifact_expired"):
                app.retrieve_artifact(
                    project_id=setup.project, artifact_id=artifact.artifact_id
                )
            with pytest.raises(SpecialistError, match="specialist_input_invalid"):
                app.submit_tool_job(
                    **arguments(setup, tool="blender", inputs=(artifact.artifact_id,))
                )
            await image(app, setup)
        finally:
            await app.close()

    run(execute())


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_missing_corrupt_artifact_never_retrieved_or_accepted(setup, fault):
    async def execute():
        app = setup.make()
        await app.start()
        try:
            _, artifact = await image(app, setup)
            path = setup.directory / PrivateFiles.name(artifact.artifact_id)
            if fault == "missing":
                path.unlink()
            else:
                path.chmod(0o600)
                path.write_bytes(b"wrong")
            assert (
                app.inspect_artifact(
                    project_id=setup.project, artifact_id=artifact.artifact_id
                ).status
                == fault
            )
            with pytest.raises(SpecialistError):
                app.retrieve_artifact(
                    project_id=setup.project, artifact_id=artifact.artifact_id
                )
            with pytest.raises(SpecialistError):
                app.submit_tool_job(
                    **arguments(setup, tool="blender", inputs=(artifact.artifact_id,))
                )
        finally:
            await app.close()

    run(execute())


@pytest.mark.posix
def test_symlink_hardlink_root_replacement_and_no_arbitrary_paths(setup):
    async def execute():
        app = setup.make()
        await app.start()
        try:
            _, artifact = await image(app, setup)
            path = setup.directory / PrivateFiles.name(artifact.artifact_id)
            outside = setup.directory.parent / "secret"
            outside.write_bytes(b"private")
            path.unlink()
            path.symlink_to(outside)
            with pytest.raises(SpecialistError):
                app.retrieve_artifact(
                    project_id=setup.project, artifact_id=artifact.artifact_id
                )
            path.unlink()
            os.link(outside, path)
            with pytest.raises(SpecialistError):
                app.retrieve_artifact(
                    project_id=setup.project, artifact_id=artifact.artifact_id
                )
            assert outside.read_bytes() == b"private"
            path.unlink()
            for aid in ("../../secret", "/etc/passwd", "https://example.invalid/x"):
                with pytest.raises(ValidationError):
                    app.retrieve_artifact(project_id=setup.project, artifact_id=aid)
            renamed = setup.directory.with_name("old")
            setup.directory.rename(renamed)
            setup.directory.mkdir(mode=0o700)
            with pytest.raises((SpecialistError, ProjectError)):
                app.specialists.authorize(setup.project)
        finally:
            await app.close()

    run(execute())


def test_store_inside_project_is_refused_and_default_disabled(setup):
    async def execute():
        app = setup.make(
            setup.config.model_copy(
                update={"artifact_directory": setup.directory.parent / "project"}
            )
        )
        with pytest.raises(SpecialistError, match="artifact_store_unsafe"):
            await app.start()
        plain = Application(
            create_database_engine(setup.url), WorkersConfig(workers=[]), providers={}
        )
        await plain.start()
        try:
            assert (
                plain.specialists is None
                and not plain.capabilities().specialist_operations
            )
            with pytest.raises(ServiceError, match="specialist_unavailable"):
                plain.list_specialist_tools(project_id=setup.project)
        finally:
            await plain.close()

    run(execute())


def test_interrupted_staged_write_recovery_does_not_replay(setup):
    async def execute():
        app = setup.make()
        # Seed the durable checkpoints a killed process leaves, without execution.
        with app.tasks.storage_access():
            service = app.specialists
            service.recover()
            tool = service.tools["image"]
            job = service.repository.submit(
                SubmitToolJob(**arguments(setup)), tool, None
            )
            service.repository.transition(job.tool_job_id, state="running")
            (output,) = await FakeComfyUI().run(Parameters(), ())
            artifact = Artifact(
                artifact_id=uuid4(),
                project_id=setup.project,
                tool_job_id=job.tool_job_id,
                tool_id="image",
                input_artifact_ids=(),
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                name=output.name,
                format="png",
                mime_type="image/png",
                size_bytes=len(output.data),
                sha256=hashlib.sha256(output.data).hexdigest(),
            )
            service.repository.reserve(artifact, setup.config)
            (
                setup.directory / PrivateFiles.name(artifact.artifact_id, pending=True)
            ).write_bytes(output.data[:20])
        await app.close()
        restarted = setup.make()
        await restarted.start()
        try:
            result = restarted.get_tool_job(tool_job_id=job.tool_job_id)
            assert (
                result.state == "interrupted"
                and result.failure_category == "recovery_required"
            )
            assert not list(setup.directory.iterdir())
            assert not result.output_artifact_ids
        finally:
            await restarted.close()

    run(execute())


def test_mcp_typed_exchange_and_web_metadata(setup):
    async def execute():
        app = setup.make()
        await app.start()
        server = create_server(lambda: app, borrowed=True)
        try:
            async with create_connected_server_and_client_session(server) as sdk:
                discovery = {t.name: t for t in (await sdk.list_tools()).tools}
                assert (
                    discovery["submit_tool_job"].inputSchema["additionalProperties"]
                    is False
                )
                assert discovery["retrieve_artifact"].outputSchema
                result = await sdk.call_tool(
                    "submit_tool_job",
                    SubmitToolJob(**arguments(setup)).model_dump(mode="json"),
                )
                assert not result.isError
                from agentforge.specialists.models import ToolJob

                job = await wait(app, ToolJob.model_validate(result.structuredContent))
                aid = str(job.output_artifact_ids[0])
                reply = await sdk.call_tool(
                    "retrieve_artifact",
                    {
                        "project_id": str(setup.project),
                        "artifact_id": aid,
                        "max_bytes": 8,
                    },
                )
                assert base64.b64decode(reply.structuredContent["data_base64"]) == PNG
                bad = await sdk.call_tool("submit_tool_job", {"path": "/private"})
                assert bad.isError and "/private" not in str(bad)
            import httpx

            web = create_app(shared_application=app)
            async with web.router.lifespan_context(web):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=web), base_url="http://localhost"
                ) as http:
                    page = await http.get("/tool-jobs")
                    assert page.status_code == 200 and "Simulated" in page.text
                    detail = await http.get("/tool-jobs/" + str(job.tool_job_id))
                    assert (
                        detail.status_code == 200
                        and "synthetic-image.png" in detail.text
                    )
                    assert (
                        str(setup.directory) not in detail.text
                        and "download=" not in detail.text
                    )
                    denied = await http.post(
                        "/tool-jobs/" + str(job.tool_job_id) + "/cancel",
                        data={"confirm": "yes"},
                    )
                    assert denied.status_code == 403
        finally:
            await app.close()

    run(execute())


@pytest.mark.parametrize("htmx", [False, True])
def test_dashboard_tool_job_cancellation_preserves_origin(setup, monkeypatch, htmx):
    from tests.test_web import client_for

    async def blocked(*_):
        await asyncio.Event().wait()

    monkeypatch.setattr(FakeComfyUI, "run", blocked)

    async def scenario():
        app = setup.make()
        async with client_for(app) as (browser, _):
            job = app.submit_tool_job(**arguments(setup))
            detail = f"/tool-jobs/{job.tool_job_id}"
            path = detail + "/cancel"
            page = await browser.get(detail)
            assert f'hx-post="{path}"' in page.text
            token = browser.cookies.get("agentforge_csrf")
            fields = {"confirm": "yes", "csrf": token}
            assert (
                await browser.post(path, data=fields, headers={"Origin": "null"})
            ).status_code == 403
            response = await browser.post(
                path,
                data=fields,
                headers={"HX-Request": "true", "Origin": "http://127.0.0.1"}
                if htmx
                else {},
            )
            assert response.status_code == (200 if htmx else 303)
            assert response.headers["HX-Redirect" if htmx else "location"] == detail
            assert (await wait(app, job)).state == "cancelled"

    run(scenario())


def test_formats_reject_external_resources_and_splats():
    for format in ("blend", "stl", "gaussian_splat", "png", "gltf"):
        with pytest.raises(SpecialistError, match="artifact_format_invalid"):
            validate(b"arbitrary bytes", format)


def test_specialist_config_cannot_become_shell_or_change_host(setup):
    tool = setup.config.tools[0].model_dump()
    for updates in (
        {"adapter": "shell"},
        {"authorized_host": "https://attacker.invalid"},
        {"kind": "inference_worker"},
        {"output_formats": ("gaussian_splat",)},
    ):
        with pytest.raises(ValidationError):
            SpecialistTool.model_validate({**tool, **updates})
    with pytest.raises(ValidationError):
        SubmitToolJob(**arguments(setup), parameters={"script": "print('unsafe')"})


@pytest.mark.parametrize("stage", ["pending", "published"])
def test_real_process_loss_recovers_without_replay(setup, tmp_path, stage):
    import subprocess
    import sys
    from pathlib import Path
    from uuid import UUID

    workers = tmp_path / "workers.toml"
    workers.write_text("workers = []\n")
    specialists = tmp_path / "specialists.toml"
    specialists.write_text(
        "artifact_directory = " + json.dumps(str(setup.directory)) + "\n"
        '[[tools]]\ntool_id = "image"\nkind = "comfyui_image"\n'
        'adapter = "fake_comfyui"\nconnection_id = "fake-image"\n'
        'enabled = true\noutput_formats = ["png"]\n'
    )
    process = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("helpers") / "specialist_process.py"),
            str(setup.url),
            str(workers),
            str(specialists),
            str(setup.project),
            stage,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 73, process.stderr
    checkpoint = json.loads(process.stdout)
    assert list(setup.directory.iterdir())

    async def execute():
        app = setup.make()
        await app.start()
        try:
            recovered = app.get_tool_job(tool_job_id=UUID(checkpoint["job"]))
            assert (
                recovered.state == "interrupted" and not recovered.output_artifact_ids
            )
            assert recovered.failure_category == "recovery_required"
            assert not list(setup.directory.iterdir())
        finally:
            await app.close()

    run(execute())


def test_finalize_failure_never_reports_success_and_keeps_safe_quota(
    setup, monkeypatch
):
    async def execute():
        app = setup.make()
        await app.start()
        original = app.specialists.files.write

        def disk_failure(identity, data):
            original(identity, data)
            raise OSError("PRIVATE_DISK_ERROR")

        monkeypatch.setattr(app.specialists.files, "write", disk_failure)
        try:
            result = await wait(app, app.submit_tool_job(**arguments(setup)))
            assert (
                result.state == "failed"
                and result.failure_category == "specialist_validation_failed"
            )
            assert "PRIVATE" not in result.model_dump_json()
            assert not result.output_artifact_ids and not list(
                setup.directory.iterdir()
            )
        finally:
            await app.close()

    run(execute())


def test_request_budget_and_binary_storage_budget(setup):
    async def execute():
        limit = setup.config.tools[0].limits.model_copy(update={"max_requests": 1})
        cfg = setup.config.model_copy(
            update={
                "tools": (setup.config.tools[0].model_copy(update={"limits": limit}),)
            }
        )
        app = setup.make(cfg)
        await app.start()
        try:
            fields = arguments(setup)
            job = app.submit_tool_job(**fields)
            assert app.submit_tool_job(**fields).tool_job_id == job.tool_job_id
            with pytest.raises(SpecialistError, match="specialist_resource_limit"):
                app.submit_tool_job(**arguments(setup))
            assert (await wait(app, job)).state == "completed"
        finally:
            await app.close()
        cfg = setup.config.model_copy(update={"max_storage_bytes": 1024})
        app = setup.make(cfg)
        await app.start()
        try:
            jobs = [app.submit_tool_job(**arguments(setup)) for _ in range(20)]
            completed = await asyncio.gather(*(wait(app, job) for job in jobs))
            assert any(
                j.failure_category == "specialist_resource_limit" for j in completed
            )
            assert sum(p.stat().st_size for p in setup.directory.iterdir()) <= 1024
        finally:
            await app.close()

    run(execute())


def test_shutdown_and_second_executor_preserve_ownership(setup, monkeypatch):
    from agentforge.db.ownership import ExecutorOwnershipError

    async def execute():
        entered = asyncio.Event()

        class Blocking(FakeComfyUI):
            async def run(self, parameters, inputs):
                entered.set()
                await asyncio.Event().wait()

        monkeypatch.setitem(ADAPTERS, "fake_comfyui", Blocking)
        app = setup.make(concurrency=1)
        await app.start()
        running = app.submit_tool_job(**arguments(setup))
        queued = app.submit_tool_job(**arguments(setup))
        await entered.wait()
        peer = setup.make()
        with pytest.raises(ExecutorOwnershipError):
            await peer.start()
        assert app.get_tool_job(tool_job_id=running.tool_job_id).state == "running"
        await app.close()
        restarted = setup.make()
        try:
            assert (
                restarted.get_tool_job(tool_job_id=running.tool_job_id).state
                == "interrupted"
            )
            assert (
                restarted.get_tool_job(tool_job_id=queued.tool_job_id).state == "queued"
            )
        finally:
            await restarted.close()

    run(execute())


def test_completed_cancellation_is_idempotent_and_root_pin_survives_restart(setup):
    async def execute():
        app = setup.make()
        await app.start()
        job, _ = await image(app, setup)
        assert app.cancel_tool_job(tool_job_id=job.tool_job_id) == job
        assert app.cancel_tool_job(tool_job_id=job.tool_job_id) == job
        await app.close()
        setup.directory.rename(setup.directory.with_name("old-store"))
        setup.directory.mkdir(mode=0o700)
        replacement = setup.make()
        with pytest.raises(SpecialistError, match="artifact_store_changed"):
            await replacement.start()

    run(execute())


def test_real_adapters_and_binary_director_actions_are_not_available():
    from agentforge.director.core import ALLOWED

    assert "list_specialist_tools" in ALLOWED
    assert not {"submit_tool_job", "cancel_tool_job", "retrieve_artifact"} & ALLOWED
    assert set(ADAPTERS) == {"fake_comfyui", "fake_blender"}


def test_denied_operation_does_not_even_read_job_storage(setup, monkeypatch):
    async def execute():
        app = setup.make()
        await app.start()
        try:
            denied = client(app, setup)
            denied.principal = lambda: ClientPolicy(
                client_id="denied", token_sha256="0" * 64
            )

            def forbidden(_):
                pytest.fail("Denied specialist operation must not read storage")

            monkeypatch.setattr(app.specialists.repository, "get", forbidden)
            with pytest.raises(ServiceError, match="access_denied"):
                denied.get_tool_job(tool_job_id=uuid4())
        finally:
            await app.close()

    run(execute())


def test_native_artifact_publication_is_exclusive_and_durable(setup):
    files = PrivateFiles(setup.directory)
    identity = uuid4()
    data = b"private artifact publication fixture"
    files.write(identity, data)
    assert files.read(identity, len(data)) == data
    with pytest.raises((OSError, SpecialistError, ProjectError)):
        files.write(identity, b"replacement forbidden")
    assert files.read(identity, len(data)) == data
    files.remove(identity)
    assert not list(setup.directory.iterdir())


@pytest.mark.windows
def test_native_artifact_publication_keeps_ancestry_pinned(setup):
    files = PrivateFiles(setup.directory)
    identity = uuid4()
    with files.root():
        with pytest.raises(PermissionError):
            setup.directory.rename(setup.directory.with_name("moved-artifacts"))
        files.write(identity, b"new private file")
        assert files.read(identity, 16) == b"new private file"
    files.remove(identity)
