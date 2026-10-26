"""Deterministic real-adapter contracts; no installed Blender, GPU or live HTTP."""

import asyncio
import base64
import binascii
import hashlib
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError

from agentforge.application.errors import ServiceError
from agentforge.coding.process import validation_process
from agentforge.db.specialists import request_digest
from agentforge.mcp.server import create_server
from agentforge.specialists.adapters import FakeBlender, FakeComfyUI
from agentforge.specialists.comfyui import ComfyUI
from agentforge.specialists.formats import validate
from agentforge.specialists.models import (
    ComfyUIConfig,
    Parameters,
    SpecialistError,
    SpecialistTool,
    SubmitToolJob,
    ToolPage,
)
from tests.test_specialists import arguments, client, image, run, wait
from tests.test_specialists import setup as setup

WORKFLOW = {
    "1": {"class_type": "KSampler", "inputs": {"seed": 0}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "approved"}},
    "3": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "approved", "images": ["1", 0]},
    },
}


def comfy(setup, **updates):
    values = dict(
        tool_id="image",
        kind="comfyui_image",
        adapter="comfyui",
        connection_id="comfy-local",
        authorized_host="configured_http",
        simulated=False,
        availability="configured_untested",
        enabled=True,
        output_formats=("png",),
        comfyui=dict(
            base_url="http://127.0.0.1:8188",
            workflow_json=json.dumps(WORKFLOW),
            output_node="3",
            seed_field={"node": "1", "field": "seed"},
            text_field={"node": "2", "field": "text"},
            poll_seconds=0.01,
        ),
    )
    values.update(updates)
    tool = SpecialistTool.model_validate(values)
    return setup.config.model_copy(update={"tools": (tool, setup.config.tools[1])})


def png():
    (output,) = run(FakeComfyUI().run(Parameters(), ()))
    body = b"prompt\0" + b'{"untrusted":"metadata"}'
    chunk = (
        struct.pack(">I", len(body))
        + b"tEXt"
        + body
        + struct.pack(">I", binascii.crc32(b"tEXt" + body))
    )
    return output.data[:33] + chunk + output.data[33:]


class Server:
    def __init__(self, mode="success"):
        self.mode, self.calls, self.receipt = mode, [], uuid4()
        self.body = None
        self.data = png()
        self.polls = 0

    def handle(self, request):
        self.calls.append(request)
        if request.url.path == "/prompt":
            self.body = json.loads(request.content)
            if self.mode == "ambiguous":
                raise httpx.ReadTimeout("PRIVATE_TOKEN /private/path", request=request)
            return httpx.Response(
                200,
                json={"prompt_id": str(self.receipt), "number": 1, "node_errors": {}},
            )
        if request.url.path.startswith("/history/"):
            self.polls += 1
            if self.mode in {"running", "ambiguous"} or self.polls == 1:
                return httpx.Response(200, json={})
            prefix = self.body["prompt"]["3"]["inputs"]["filename_prefix"]
            ref = {
                "filename": prefix + "_00001_.png",
                "subfolder": "",
                "type": "output",
            }
            if self.mode == "hostile":
                ref["filename"] = "../../secret.png"
            status = {
                "status_str": "error" if self.mode == "failure" else "success",
                "completed": self.mode != "failure",
                "messages": ["PRIVATE /secret"],
            }
            return httpx.Response(
                200,
                json={
                    str(self.receipt): {
                        "prompt": [
                            1,
                            str(self.receipt),
                            self.body["prompt"],
                            {"client_id": self.body["client_id"]},
                            ["3"],
                        ],
                        "status": status,
                        "outputs": {"3": {"images": [ref]}},
                    }
                },
            )
        if request.url.path == "/queue":
            return httpx.Response(
                200,
                json={
                    "queue_running": [
                        [
                            1,
                            str(self.receipt),
                            self.body["prompt"],
                            {"client_id": self.body["client_id"]},
                        ]
                    ],
                    "queue_pending": [],
                },
            )
        assert request.url.path == "/view"
        if self.mode == "redirect":
            return httpx.Response(
                302, headers={"Location": "https://evil.example/secret"}
            )
        return httpx.Response(
            200,
            content=b"not PNG"
            if self.mode == "corrupt"
            else b"x" * 1048577
            if self.mode == "oversize"
            else self.data,
        )


def transport(monkeypatch, server):
    original = ComfyUI.__init__

    def init(self, tool, context, **kwargs):
        original(self, tool, context, transport=httpx.MockTransport(server.handle))

    monkeypatch.setattr(ComfyUI, "__init__", init)


def test_comfy_mcp_receipt_poll_publication_duplicate_and_private_config(
    setup, monkeypatch
):
    server = Server()
    transport(monkeypatch, server)

    async def execute():
        app = setup.make(comfy(setup))
        await app.start()
        try:
            async with create_connected_server_and_client_session(
                create_server(lambda: client(app, setup), borrowed=True)
            ) as session:
                discovery = await session.call_tool(
                    "list_specialist_tools", {"project_id": str(setup.project)}
                )
                text = json.dumps(discovery.structuredContent)
                assert {
                    tool.adapter
                    for tool in ToolPage.model_validate(
                        discovery.structuredContent
                    ).tools
                } == {"comfyui", "fake_blender"}
                assert (
                    "configured_untested" in text
                    and "workflow_json" not in text
                    and "127.0.0.1" not in text
                )
                fields = arguments(setup, parameters={"seed": 42, "text": "hello"})
                reply = await session.call_tool(
                    "submit_tool_job",
                    json.loads(SubmitToolJob.model_validate(fields).model_dump_json()),
                )
                job = await wait(
                    app,
                    app.get_tool_job(
                        tool_job_id=UUID(reply.structuredContent["tool_job_id"])
                    ),
                )
                assert job.state == "completed", job
                assert (
                    job.execution.phase == "outputs_validated"
                    and job.execution.remote_job_id == server.receipt
                )
                assert (
                    job.execution.remote_state == "completed"
                    and not job.execution.remote_may_continue
                )
                assert (
                    job.connection_origin == "http://127.0.0.1:8188/"
                    and not job.simulated
                )
                assert client(app, setup).submit_tool_job(**fields) == job
                artifact = app.inspect_artifact(
                    project_id=setup.project, artifact_id=job.output_artifact_ids[0]
                )
                assert (
                    artifact.sha256 == hashlib.sha256(server.data).hexdigest()
                    and not artifact.simulated
                )
                assert job.execution.validated_output_sha256 == artifact.sha256
                with pytest.raises(SpecialistError, match="execution_output_changed"):
                    app.specialist_service().repository.evidence(
                        job.tool_job_id, validated_output_sha256="0" * 64
                    )
                assert server.body["prompt"]["1"]["inputs"]["seed"] == 42
                assert "PRIVATE" not in job.model_dump_json()
                assert sum(r.method == "POST" for r in server.calls) == 1
        finally:
            await app.close()

    run(execute())


@pytest.mark.parametrize(
    "mode,category",
    [
        ("failure", "comfyui_execution_failed"),
        ("hostile", "comfyui_output_invalid"),
        ("redirect", "comfyui_http_failed"),
        ("corrupt", "artifact_format_invalid"),
        ("oversize", "comfyui_response_limit"),
    ],
)
def test_comfy_failures_do_not_publish_or_follow_references(
    setup, monkeypatch, mode, category
):
    server = Server(mode)
    transport(monkeypatch, server)

    async def execute():
        app = setup.make(comfy(setup))
        await app.start()
        try:
            job = await wait(app, app.submit_tool_job(**arguments(setup)))
            assert (job.state, job.failure_category) == ("failed", category)
            assert (
                not job.output_artifact_ids
                and job.execution.remote_job_id == server.receipt
            )
            assert not app.list_artifacts(project_id=setup.project).artifacts
            assert all(r.url.host == "127.0.0.1" for r in server.calls)
            if mode == "hostile":
                assert not any(r.url.path == "/view" for r in server.calls)
        finally:
            await app.close()

    run(execute())


@pytest.mark.parametrize("known", [False, True])
def test_restart_reconciles_known_receipt_without_resubmission(
    setup, monkeypatch, known
):
    server = Server("running" if known else "ambiguous")
    transport(monkeypatch, server)

    async def execute():
        config = comfy(setup)
        app = setup.make(config)
        await app.start()
        fields = arguments(setup)
        job = app.submit_tool_job(**fields)
        while not server.body or (
            known
            and not app.get_tool_job(
                tool_job_id=job.tool_job_id
            ).execution.remote_job_id
        ):
            await asyncio.sleep(0.01)
        await app.close()
        server.mode = "success"
        app = setup.make(config)
        await app.start()
        try:
            job = app.submit_tool_job(**fields)
            if known:
                job = await wait(app, job)
                assert job.state == "completed"
            else:
                assert (
                    job.state == "interrupted"
                    and job.failure_category == "recovery_required"
                )
                assert (
                    job.execution.phase == "submission_unknown"
                    and job.execution.remote_may_continue
                )
                # Operator can attach only an upstream receipt matching BOTH our
                # unique prefix and client_id. This is never a model operation.
                original_client = server.body["client_id"]
                server.body["client_id"] = str(uuid4())
                with pytest.raises(
                    SpecialistError, match="recovery_receipt_unverified"
                ):
                    await app.specialist_service().reconcile_remote(
                        tool_job_id=job.tool_job_id, remote_job_id=server.receipt
                    )
                assert (
                    app.get_tool_job(
                        tool_job_id=job.tool_job_id
                    ).execution.remote_job_id
                    is None
                )
                server.body["client_id"] = original_client
                await app.specialist_service().reconcile_remote(
                    tool_job_id=job.tool_job_id, remote_job_id=server.receipt
                )
                job = await wait(app, job)
                assert job.state == "completed"
            assert sum(r.method == "POST" for r in server.calls) == 1
        finally:
            await app.close()

    run(execute())


def test_comfy_cancel_preserves_receipt_and_never_interrupts_other_clients(
    setup, monkeypatch
):
    server = Server("running")
    transport(monkeypatch, server)

    async def execute():
        config = comfy(setup)
        app = setup.make(config)
        await app.start()
        fields = arguments(setup)
        job = app.submit_tool_job(**fields)
        while not app.get_tool_job(tool_job_id=job.tool_job_id).execution.remote_job_id:
            await asyncio.sleep(0.01)
        app.cancel_tool_job(tool_job_id=job.tool_job_id)
        job = await wait(app, job)
        assert job.state == "cancelled" and job.execution.remote_may_continue
        assert (
            job.execution.remote_job_id == server.receipt
            and not job.output_artifact_ids
        )
        await app.close()
        server.mode = "success"
        app = setup.make(config)
        await app.start()
        try:
            assert app.submit_tool_job(**fields).state == "cancelled"
            assert sum(r.method == "POST" for r in server.calls) == 1
        finally:
            await app.close()

    run(execute())


def test_configuration_policy_typed_arguments_and_phase_a_hashes(setup, monkeypatch):
    server = Server()
    transport(monkeypatch, server)
    for url in (
        "http://localhost:8188",
        "http://remote.test",
        "https://user:secret@remote.test",
        "https://remote.test/path",
        "https://remote.test/?secret=1",
    ):
        with pytest.raises(ValidationError):
            ComfyUIConfig(
                base_url=url, workflow_json=json.dumps(WORKFLOW), output_node="3"
            )
    config = comfy(setup, trust="cloud")

    async def execute():
        app = setup.make(config)
        await app.start()
        try:
            with pytest.raises((ServiceError, SpecialistError)):
                app.submit_tool_job(**arguments(setup))
            assert not server.calls
        finally:
            await app.close()

    run(execute())
    for parameters in (
        {"workflow": {}},
        {"seed": "1"},
        {"text": "x" * 2001},
        {"operation": "exec"},
        {"depth": float("nan")},
    ):
        with pytest.raises(ValidationError):
            SubmitToolJob.model_validate(arguments(setup, parameters=parameters))
    fields = SubmitToolJob.model_validate(arguments(setup))
    previous = fields.model_dump(mode="json")
    previous["parameters"] = {"seed": 0}
    assert (
        request_digest(fields)
        == hashlib.sha256(
            json.dumps(previous, separators=(",", ":")).encode()
        ).hexdigest()
    )


def blender_config(setup, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    executable = Path(sys.executable).resolve()
    tool = SpecialistTool(
        tool_id="blender",
        kind="blender_scene",
        adapter="blender",
        connection_id="local-blender",
        authorized_host="application_host",
        simulated=False,
        availability="configured_untested",
        enabled=True,
        input_formats=("png",),
        output_formats=("gltf",),
        blender=dict(
            executable=executable,
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            staging_directory=stage,
        ),
    )
    return setup.config.model_copy(update={"tools": (setup.config.tools[0], tool)})


def executable_fixture(monkeypatch, tmp_path, mode):
    # A representative Blender exporter profile: names, omitted triangle mode,
    # accessor offsets and uint32 indices. Native acceptance checks Blender itself.
    (output,) = run(FakeBlender().run(Parameters(), ()))
    document = json.loads(output.data)
    document["asset"]["generator"] = "Khronos glTF Blender I/O v4.5 profile fixture"
    for key in ("nodes", "scenes", "meshes"):
        document[key][0]["name"] = "Blockout"
    document["meshes"][0]["primitives"][0].pop("mode")
    binary = base64.b64decode(document["buffers"][0]["uri"].split(",")[1])[
        :36
    ] + struct.pack("<3I", 0, 1, 2)
    document["buffers"][0].update(
        uri="data:application/octet-stream;base64," + base64.b64encode(binary).decode(),
        byteLength=len(binary),
    )
    document["bufferViews"][1]["byteLength"] = 12
    document["accessors"][1]["componentType"] = 5125
    document["accessors"][0]["byteOffset"] = 0
    fixture = tmp_path / "export.json"
    fixture.write_text(json.dumps(document))
    marker = tmp_path / "descendant"
    calls = []

    async def process(argv, **kwargs):
        calls.append(argv)
        assert argv[1:7] == [
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
        ]
        assert Path(argv[7]).name == "blender_script.py" and len(argv) == 8
        return await validation_process(
            [
                str(Path(sys.executable).resolve()),
                str(Path(__file__).parent / "helpers/blender_fixture.py"),
                str(fixture),
                mode,
                str(marker),
            ],
            **kwargs,
        )

    monkeypatch.setattr("agentforge.specialists.blender.validation_process", process)
    return marker, calls


@pytest.mark.parametrize("mode", ["success", "partial", "logs", "timeout", "cancel"])
def test_blender_invocation_process_cleanup_exports_and_provenance(
    setup, monkeypatch, tmp_path, mode
):
    marker, calls = executable_fixture(monkeypatch, tmp_path, mode)
    config = blender_config(setup, tmp_path)
    if mode == "timeout":
        tool = config.tools[1]
        config = config.model_copy(
            update={
                "tools": (
                    config.tools[0],
                    tool.model_copy(
                        update={
                            "limits": tool.limits.model_copy(
                                update={"runtime_seconds": 0.3}
                            )
                        }
                    ),
                )
            }
        )

    async def execute():
        app = setup.make(config)
        await app.start()
        try:
            _, artifact = await image(app, setup)
            fields = arguments(
                setup,
                tool="blender",
                inputs=(artifact.artifact_id,),
                parameters={"operation": "reference_blockout", "depth": 0.5},
            )
            job = app.submit_tool_job(**fields)
            if mode == "cancel":
                while not Path(str(marker) + ".started").exists():
                    await asyncio.sleep(0.01)
                app.cancel_tool_job(tool_job_id=job.tool_job_id)
            job = await wait(app, job)
            if mode == "success":
                assert job.state == "completed", job
                output = app.inspect_artifact(
                    project_id=setup.project, artifact_id=job.output_artifact_ids[0]
                )
                assert (
                    output.geometry == "mesh"
                    and output.geometry_source == "known_blockout"
                    and not output.simulated
                )
                assert (
                    output.operation == "reference_blockout"
                    and output.printability == "not_assessed"
                )
                assert output.input_artifact_ids == (artifact.artifact_id,)
                assert (
                    job.execution.local_exit_code == 0
                    and job.execution.phase == "outputs_validated"
                )
                assert app.submit_tool_job(**fields) == job
            else:
                assert job.state == ("cancelled" if mode == "cancel" else "failed"), job
                assert not job.output_artifact_ids
            assert len(calls) == 1 and not list(
                config.tools[1].blender.staging_directory.iterdir()
            )
            if mode in {"cancel", "timeout"}:
                await asyncio.sleep(1.1)
                assert not marker.exists()
        finally:
            await app.close()

    run(execute())


def test_png_metadata_and_gltf_profile_reject_resource_escapes(tmp_path, monkeypatch):
    assert validate(png(), "png") == ("image/png", "none")
    executable_fixture(monkeypatch, tmp_path, "success")
    document = json.loads((tmp_path / "export.json").read_bytes())
    assert validate(json.dumps(document).encode(), "gltf") == (
        "model/gltf+json",
        "mesh",
    )
    for value in ("https://evil.test/data.bin", "../secret", "file:///secret"):
        document["buffers"][0]["uri"] = value
        with pytest.raises(SpecialistError):
            validate(json.dumps(document).encode(), "gltf")


@pytest.mark.parametrize(
    "failure", ["receipt_commit", "timeout", "bad_receipt", "response_limit"]
)
def test_submission_windows_remain_recovery_required(setup, monkeypatch, failure):
    server = Server("running" if failure == "timeout" else "success")
    original_handle = server.handle

    def handle(request):
        if request.url.path == "/prompt" and failure in {
            "bad_receipt",
            "response_limit",
        }:
            server.calls.append(request)
            server.body = json.loads(request.content)
            return httpx.Response(
                200,
                json={"prompt_id": "../../secrets"}
                if failure == "bad_receipt"
                else {"huge": "x" * 600000},
            )
        return original_handle(request)

    server.handle = handle
    transport(monkeypatch, server)

    async def execute():
        config = comfy(setup)
        if failure == "timeout":
            tool = config.tools[0]
            config = config.model_copy(
                update={
                    "tools": (
                        tool.model_copy(
                            update={
                                "limits": tool.limits.model_copy(
                                    update={"runtime_seconds": 0.15}
                                )
                            }
                        ),
                        config.tools[1],
                    )
                }
            )
        app = setup.make(config)
        repository = app.specialist_service().repository
        evidence = repository.evidence

        def record(identity, **fields):
            if failure == "receipt_commit" and "remote_job_id" in fields:
                raise OSError("database receipt commit failed /PRIVATE")
            return evidence(identity, **fields)

        monkeypatch.setattr(repository, "evidence", record)
        await app.start()
        try:
            fields = arguments(setup)
            job = await wait(app, app.submit_tool_job(**fields))
            assert (
                job.state == "interrupted"
                and job.failure_category == "recovery_required"
            )
            assert job.execution.remote_may_continue and not job.output_artifact_ids
            if failure != "timeout":
                assert (
                    job.execution.phase == "submission_unknown"
                    and job.execution.remote_job_id is None
                )
            assert app.submit_tool_job(**fields) == job
            assert sum(r.method == "POST" for r in server.calls) == 1
            assert "PRIVATE" not in job.model_dump_json()
        finally:
            await app.close()

    run(execute())


def test_blender_script_builds_bounded_geometry_with_fixed_export(
    tmp_path, monkeypatch
):
    from agentforge.specialists.blender_script import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "request.json").write_text(
        json.dumps({"operation": "reference_blockout", "depth": 0.5})
    )
    (tmp_path / "reference.png").write_bytes(png())
    exports, loaded = [], []
    cube = SimpleNamespace(select_set=lambda selected: None)
    context = SimpleNamespace(
        object=cube,
        collection=SimpleNamespace(objects=SimpleNamespace(link=lambda obj: None)),
        view_layer=SimpleNamespace(objects=SimpleNamespace(active=None)),
    )

    def load(path, **kwargs):
        loaded.append((path, kwargs))
        return SimpleNamespace(size=(400, 200))

    def export(**kwargs):
        exports.append(kwargs)
        return {"FINISHED"}

    bpy = SimpleNamespace(
        app=SimpleNamespace(version=(4, 5, 3)),
        context=context,
        data=SimpleNamespace(
            images=SimpleNamespace(load=load),
            objects=SimpleNamespace(new=lambda name, data: SimpleNamespace()),
        ),
        ops=SimpleNamespace(
            object=SimpleNamespace(
                select_all=lambda **kw: None,
                delete=lambda **kw: None,
                transform_apply=lambda **kw: None,
            ),
            mesh=SimpleNamespace(primitive_cube_add=lambda **kw: None),
            export_scene=SimpleNamespace(gltf=export),
        ),
    )
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    main()
    assert cube.scale == (1.0, 0.5, 0.5)
    assert loaded == [(str(tmp_path / "reference.png"), {"check_existing": False})]
    assert exports[0]["filepath"] == str(tmp_path / "scene.gltf")
    assert (
        exports[0]["export_format"] == "GLTF_SEPARATE" and exports[0]["use_selection"]
    )
    assert (
        exports[0]["export_materials"] == "NONE" and not exports[0]["export_animations"]
    )
    (tmp_path / "request.json").write_text('{"operation":"python","depth":0.5}')
    with pytest.raises(ValueError):
        main()
    assert len(exports) == 1


def test_blender_hash_and_staging_project_boundary(setup, tmp_path):
    config = blender_config(setup, tmp_path)
    tool = config.tools[1]
    changed = tool.model_copy(
        update={
            "blender": tool.blender.model_copy(update={"executable_sha256": "0" * 64})
        }
    )
    config = config.model_copy(update={"tools": (config.tools[0], changed)})

    async def execute():
        app = setup.make(config)
        await app.start()
        try:
            _, artifact = await image(app, setup)
            job = await wait(
                app,
                app.submit_tool_job(
                    **arguments(
                        setup,
                        tool="blender",
                        inputs=(artifact.artifact_id,),
                        parameters={"operation": "reference_blockout"},
                    )
                ),
            )
            assert (
                job.state == "failed"
                and job.failure_category == "blender_executable_changed"
            )
            assert (
                job.execution.phase == "not_submitted" and not job.output_artifact_ids
            )
        finally:
            await app.close()
        unsafe = tool.blender.staging_directory
        app = setup.make(config)
        app.projects.register_project("Staging cannot be a Project", unsafe)
        with pytest.raises((SpecialistError, ServiceError)):
            await app.start()
        assert not list(unsafe.iterdir())

    run(execute())


def test_auth_privacy_and_missing_credential_never_submits(setup, monkeypatch):
    server = Server()
    transport(monkeypatch, server)
    config = comfy(setup)
    private = config.tools[0].comfyui.model_dump(mode="json")
    private.update(base_url="https://comfy.example/", api_key_env="COMFYUI_TEST_TOKEN")
    tool = config.tools[0].model_copy(
        update={"comfyui": ComfyUIConfig.model_validate(private)}
    )
    config = config.model_copy(update={"tools": (tool, config.tools[1])})

    async def execute():
        app = setup.make(config)
        await app.start()
        try:
            monkeypatch.delenv("COMFYUI_TEST_TOKEN", raising=False)
            job = await wait(app, app.submit_tool_job(**arguments(setup)))
            assert job.state == "failed" and job.execution.phase == "not_submitted"
            assert not server.calls
            monkeypatch.setenv("COMFYUI_TEST_TOKEN", "PRIVATE_TOKEN_VALUE")
            job = await wait(app, app.submit_tool_job(**arguments(setup)))
            assert job.state == "completed"
            assert all(
                r.headers["authorization"] == "Bearer PRIVATE_TOKEN_VALUE"
                for r in server.calls
            )
            public = (
                app.list_specialist_tools(project_id=setup.project).model_dump_json()
                + job.model_dump_json()
            )
            assert (
                "PRIVATE_TOKEN_VALUE" not in public
                and "COMFYUI_TEST_TOKEN" not in public
            )
            assert "workflow_json" not in json.dumps(
                type(
                    app.list_specialist_tools(project_id=setup.project)
                ).model_json_schema(mode="serialization")
            )
        finally:
            await app.close()

    run(execute())


def test_real_cross_client_inputs_quota_and_atomic_finalize(
    setup, monkeypatch, tmp_path
):
    server = Server()
    transport(monkeypatch, server)
    config = comfy(setup)
    blender = blender_config(setup, tmp_path).tools[1]
    config = config.model_copy(
        update={"tools": (config.tools[0], blender), "max_artifacts": 1}
    )
    executable_fixture(monkeypatch, tmp_path, "success")

    async def execute():
        app = setup.make(config)
        await app.start()
        try:
            alpha, beta = client(app, setup), client(app, setup, "beta")
            first = await wait(app, alpha.submit_tool_job(**arguments(setup)))
            assert first.state == "completed"
            fields = arguments(
                setup,
                tool="blender",
                inputs=first.output_artifact_ids,
                parameters={"operation": "reference_blockout"},
            )
            with pytest.raises(ServiceError):
                beta.submit_tool_job(**fields)
            second = await wait(app, alpha.submit_tool_job(**fields))
            assert (
                second.state == "failed"
                and second.failure_category == "specialist_resource_limit"
            )
            assert (
                not second.output_artifact_ids
                and second.input_artifact_ids == first.output_artifact_ids
            )
            assert (
                second.execution.phase == "outputs_validated"
                and second.execution.validated_output_sha256
            )
            assert len(alpha.list_artifacts(project_id=setup.project).artifacts) == 1
            assert len(list(setup.directory.iterdir())) == 1
        finally:
            await app.close()

    run(execute())
