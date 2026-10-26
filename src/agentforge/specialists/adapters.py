"""Deterministic in-process fixtures. These adapters never contact real tools."""

import asyncio
import base64
import binascii
import json
import struct
import zlib
from dataclasses import dataclass
from typing import Protocol

from agentforge.specialists.formats import PNG


@dataclass(frozen=True)
class Output:
    name: str
    format: str
    mime_type: str
    data: bytes


class Adapter(Protocol):
    async def run(
        self, parameters, inputs: tuple[bytes, ...]
    ) -> tuple[Output, ...]: ...


class FakeComfyUI:
    async def run(self, parameters, inputs):
        await asyncio.sleep(0)

        def chunk(kind, body):
            return (
                struct.pack(">I", len(body))
                + kind
                + body
                + struct.pack(">I", binascii.crc32(kind + body))
            )

        rgb = bytes((parameters.seed % 256, 64, 192))
        data = (
            PNG
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + rgb * 2) * 2))
            + chunk(b"IEND", b"")
        )
        return (Output("synthetic-image.png", "png", "image/png", data),)


class FakeBlender:
    async def run(self, parameters, inputs):
        await asyncio.sleep(0)
        # A known triangle, not image-to-3D inference. Inputs are provenance.
        binary = struct.pack("<9f3H", 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 2)
        document = {
            "asset": {"version": "2.0", "generator": "AgentForge synthetic fixture"},
            "buffers": [
                {
                    "uri": "data:application/octet-stream;base64,"
                    + base64.b64encode(binary).decode(),
                    "byteLength": len(binary),
                }
            ],
            "bufferViews": [
                {"buffer": 0, "byteOffset": 0, "byteLength": 36, "target": 34962},
                {"buffer": 0, "byteOffset": 36, "byteLength": 6, "target": 34963},
            ],
            "accessors": [
                {
                    "bufferView": 0,
                    "componentType": 5126,
                    "count": 3,
                    "type": "VEC3",
                    "min": [0, 0, 0],
                    "max": [1, 1, 0],
                },
                {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"},
            ],
            "meshes": [
                {
                    "primitives": [
                        {"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}
                    ]
                }
            ],
            "nodes": [{"mesh": 0}],
            "scenes": [{"nodes": [0]}],
            "scene": 0,
        }
        return (
            Output(
                "synthetic-triangle.gltf",
                "gltf",
                "model/gltf+json",
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
            ),
        )


ADAPTERS = {"fake_comfyui": FakeComfyUI, "fake_blender": FakeBlender}
