"""Offline executable fixture for the fixed Blender invocation contract."""

import base64
import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    fixture, mode, marker = sys.argv[1:]
    root = Path.cwd()
    request = json.loads((root / "request.json").read_text())
    assert request["operation"] == "reference_blockout"
    assert (root / "reference.png").read_bytes().startswith(b"\x89PNG")
    if mode in {"timeout", "cancel"}:
        # The child must not outlive either timeout or asyncio cancellation.
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time,pathlib; time.sleep(1); pathlib.Path("
                + repr(marker)
                + ").write_text('escaped')",
            ]
        )
        Path(marker + ".started").write_text("ready")
        time.sleep(30)
    if mode == "logs":
        print("x" * 70000, flush=True)
        time.sleep(1)
    document = json.loads(Path(fixture).read_text())
    binary = base64.b64decode(document["buffers"][0]["uri"].split(",", 1)[1])
    document["buffers"][0]["uri"] = "scene.bin"
    (root / "scene.gltf").write_text(json.dumps(document))
    if mode != "partial":
        (root / "scene.bin").write_bytes(binary)


if __name__ == "__main__":
    main()
