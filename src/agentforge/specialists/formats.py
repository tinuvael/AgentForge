"""Conservative verified profiles. No external glTF resources or code payloads."""

import base64
import binascii
import json
import math
import struct
import zlib

from agentforge.specialists.models import SpecialistError

PNG = b"\x89PNG\r\n\x1a\n"


def validate(data, format):
    try:
        if not isinstance(data, bytes) or not 1 <= len(data) <= 16_777_216:
            raise ValueError
        if format == "png":
            _png(data)
            return "image/png", "none"
        if format == "gltf":
            _gltf(data)
            return "model/gltf+json", "mesh"
    except (
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        struct.error,
        zlib.error,
        RecursionError,
        OverflowError,
    ):
        pass
    raise SpecialistError("artifact_format_invalid")


def _png(data):
    if not data.startswith(PNG):
        raise ValueError
    offset, chunks, compressed = 8, [], bytearray()
    while offset < len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + size]
        crc = struct.unpack_from(">I", data, offset + 8 + size)[0]
        if crc != binascii.crc32(kind + body) or len(body) != size:
            raise ValueError
        chunks.append(kind)
        if len(chunks) > 4096:
            raise ValueError
        if len(chunks) == 1 and kind != b"IHDR":
            raise ValueError
        if len(chunks) > 1 and chunks[-2] == b"IEND":
            raise ValueError
        if kind == b"IHDR":
            width, height, depth, color, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            if (
                not 1 <= width <= 4096
                or not 1 <= height <= 4096
                or (depth, comp, filt, interlace) != (8, 0, 0, 0)
                or color not in {2, 6}
            ):
                raise ValueError
            stride = 1 + width * (3 if color == 2 else 4)
            expected = stride * height
            if expected > 16_777_216:
                raise ValueError
        elif kind == b"IDAT":
            compressed.extend(body)
        elif kind == b"tEXt":
            # ComfyUI SaveImage emits uncompressed prompt/workflow text. Treat it
            # only as opaque metadata; never execute or use it as provenance.
            keyword, separator, _ = body.partition(b"\0")
            if not separator or not 1 <= len(keyword) <= 79 or size > 262144:
                raise ValueError
        elif kind != b"IEND" or size:
            raise ValueError
        offset += size + 12
    if (
        offset != len(data)
        or chunks[0] != b"IHDR"
        or chunks[-1] != b"IEND"
        or chunks.count(b"IHDR") != 1
        or chunks.count(b"IEND") != 1
        or b"IDAT" not in chunks
    ):
        raise ValueError
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, expected + 1)
    if (
        len(raw) != expected
        or not decoder.eof
        or decoder.unused_data
        or any(raw[i] > 4 for i in range(0, expected, stride))
    ):
        raise ValueError


def _gltf(data):
    document = json.loads(data)
    if (
        set(document)
        != {
            "asset",
            "buffers",
            "bufferViews",
            "accessors",
            "meshes",
            "nodes",
            "scenes",
            "scene",
        }
        or not isinstance(document["asset"], dict)
        or document["asset"].get("version") != "2.0"
        or set(document["asset"]) - {"version", "generator"}
    ):
        raise ValueError
    if (
        len(document["buffers"]) != 1
        or len(document["meshes"]) != 1
        or len(document["accessors"]) != 2
        or len(document["bufferViews"]) != 2
        or set(document["meshes"][0]) - {"primitives", "name"}
    ):
        raise ValueError
    buffer = document["buffers"][0]
    if set(buffer) != {"uri", "byteLength"} or type(buffer["uri"]) is not str:
        raise ValueError
    prefix = "data:application/octet-stream;base64,"
    if not buffer["uri"].startswith(prefix):
        raise ValueError
    binary = base64.b64decode(buffer["uri"][len(prefix) :], validate=True)
    if (
        type(buffer["byteLength"]) is not int
        or len(binary) != buffer["byteLength"]
        or not binary
    ):
        raise ValueError
    primitives = document["meshes"][0]["primitives"]
    if len(primitives) != 1 or set(primitives[0]) - {"attributes", "indices", "mode"}:
        raise ValueError
    primitive = primitives[0]
    if primitive.get("mode", 4) != 4 or set(primitive["attributes"]) != {"POSITION"}:
        raise ValueError
    positions = _accessor(
        document, binary, primitive["attributes"]["POSITION"], 5126, "VEC3", 12
    )
    index_component = document["accessors"][primitive["indices"]]["componentType"]
    if index_component not in {5123, 5125}:
        raise ValueError
    index_width = 2 if index_component == 5123 else 4
    indices = _accessor(
        document, binary, primitive["indices"], index_component, "SCALAR", index_width
    )
    if (
        len(indices) % (3 * index_width)
        or any(
            i[0] >= len(positions) // 12
            for i in struct.iter_unpack("<H" if index_width == 2 else "<I", indices)
        )
        or any(not math.isfinite(v[0]) for v in struct.iter_unpack("<f", positions))
    ):
        raise ValueError
    position_accessor = document["accessors"][primitive["attributes"]["POSITION"]]
    minimum, maximum = [math.inf] * 3, [-math.inf] * 3
    for vertex in struct.iter_unpack("<3f", positions):
        for i in range(3):
            minimum[i] = min(minimum[i], vertex[i])
            maximum[i] = max(maximum[i], vertex[i])
    if (
        position_accessor.get("min") != minimum
        or position_accessor.get("max") != maximum
    ):
        raise ValueError
    if (
        type(document["scene"]) is not int
        or document["scene"] != 0
        or len(document["scenes"]) != 1
        or set(document["scenes"][0]) - {"nodes", "name"}
        or document["scenes"][0]["nodes"] != [0]
        or len(document["nodes"]) != 1
        or set(document["nodes"][0])
        - {"mesh", "name", "translation", "rotation", "scale"}
        or type(document["nodes"][0]["mesh"]) is not int
        or document["nodes"][0]["mesh"] != 0
    ):
        raise ValueError
    for key, count in (("translation", 3), ("rotation", 4), ("scale", 3)):
        value = document["nodes"][0].get(key)
        if value is not None and (
            not isinstance(value, list)
            or len(value) != count
            or any(type(v) not in {float, int} or not math.isfinite(v) for v in value)
        ):
            raise ValueError
    for item in (*document["nodes"], *document["scenes"], *document["meshes"]):
        if "name" in item and (
            not isinstance(item["name"], str) or len(item["name"]) > 100
        ):
            raise ValueError


def _accessor(document, binary, index, component, kind, width):
    if type(index) is not int or not 0 <= index < len(document["accessors"]):
        raise ValueError
    accessor = document["accessors"][index]
    if (
        set(accessor)
        - {"bufferView", "byteOffset", "componentType", "count", "type", "min", "max"}
        or accessor["componentType"] != component
        or accessor["type"] != kind
        or type(accessor["count"]) is not int
        or not 1 <= accessor["count"] <= 300000
    ):
        raise ValueError
    view_id = accessor["bufferView"]
    if type(view_id) is not int or not 0 <= view_id < len(document["bufferViews"]):
        raise ValueError
    view = document["bufferViews"][view_id]
    if (
        set(view) - {"buffer", "byteOffset", "byteLength", "target"}
        or view["buffer"] != 0
        or type(view["byteLength"]) is not int
    ):
        raise ValueError
    start = accessor.get("byteOffset", 0)
    if (
        type(start) is not int
        or start < 0
        or start + accessor["count"] * width > view["byteLength"]
    ):
        raise ValueError
    offset, length = view.get("byteOffset", 0), view["byteLength"]
    if (
        type(offset) is not int
        or offset < 0
        or offset % (2 if component == 5123 else 4)
        or start % (2 if component == 5123 else 4)
        or offset + length > len(binary)
    ):
        raise ValueError
    return binary[offset + start : offset + start + accessor["count"] * width]
