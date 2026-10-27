"""Fixed local headless operation; trusted host execution, not a sandbox."""

import base64
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agentforge.coding.process import validation_process
from agentforge.specialists.adapters import Output
from agentforge.specialists.filesystem import PrivateFiles
from agentforge.specialists.models import SpecialistError


class Blender:
    def __init__(self, tool, context):
        self.tool, self.context = tool, context

    async def run(self, parameters, inputs):
        config = self.tool.blender
        executable = config.executable
        info = executable.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or not 1 <= info.st_size <= 1 << 30
            or (os.name != "nt" and not os.access(executable, os.X_OK))
        ):
            raise SpecialistError("blender_executable_invalid")
        with executable.open("rb") as stream:
            if (
                hashlib.file_digest(stream, "sha256").hexdigest()
                != config.executable_sha256
            ):
                raise SpecialistError("blender_executable_changed")
        stage = self.context.staging
        script = Path(__file__).with_name("blender_script.py")
        with (
            stage.root(),
            tempfile.TemporaryDirectory(
                prefix="job-" + self.context.job.tool_job_id.hex + "-", dir=stage.path
            ) as directory,
        ):
            root = Path(directory)
            (root / "reference.png").write_bytes(inputs[0])
            request = {"operation": parameters.operation, "depth": parameters.depth}
            (root / "request.json").write_text(json.dumps(request), encoding="utf-8")
            digest = hashlib.sha256(
                script.read_bytes()
                + json.dumps(request, sort_keys=True).encode()
                + hashlib.sha256(inputs[0]).digest()
            ).hexdigest()
            self.context.record(phase="submission_unknown", intent_sha256=digest)
            result = await validation_process(
                [
                    str(executable),
                    "--background",
                    "--factory-startup",
                    "--disable-autoexec",
                    "--python-exit-code",
                    "1",
                    "--python",
                    str(script),
                ],
                cwd=root,
                timeout=self.tool.limits.runtime_seconds,
                cap=config.max_log_bytes,
                cancellation=SimpleNamespace(cancelled=False),
            )
            self.context.record(
                local_exit_code=result.exit_code,
                logs_truncated=result.stdout_truncated or result.stderr_truncated,
            )
            if result.timed_out:
                raise SpecialistError("specialist_timeout")
            if (
                result.exit_code != 0
                or result.stdout_truncated
                or result.stderr_truncated
            ):
                raise SpecialistError("blender_execution_failed")
            # Read only these two fixed regular files through the native no-follow
            # boundary, before parsing and embedding the export's sole buffer.
            files = PrivateFiles(root)

            def read(name):
                with (
                    files.root() as handle,
                    files.backend.regular_file(handle, name) as stream,
                ):
                    if os.name != "nt" and os.fstat(stream.fileno()).st_nlink != 1:
                        raise SpecialistError("blender_output_invalid")
                    data = stream.read(self.tool.limits.max_output_bytes + 1)
                    if len(data) > self.tool.limits.max_output_bytes:
                        raise SpecialistError("specialist_resource_limit")
                    return data

            document = json.loads(read("scene.gltf"))
            buffers = document.get("buffers", [])
            if len(buffers) != 1 or buffers[0].get("uri") != "scene.bin":
                raise SpecialistError("blender_output_invalid")
            binary = read("scene.bin")
            if buffers[0].get("byteLength") != len(binary):
                raise SpecialistError("blender_output_invalid")
            buffers[0]["uri"] = (
                "data:application/octet-stream;base64,"
                + base64.b64encode(binary).decode()
            )
            data = json.dumps(document, separators=(",", ":")).encode()
            return (Output("reference-blockout.gltf", "gltf", "model/gltf+json", data),)
