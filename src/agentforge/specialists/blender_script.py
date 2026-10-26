"""Trusted Blender 4.5 script, invoked only with a private cwd and fixed filenames.

Import-safe outside Blender so installed-package import validation can inspect it.
This builds a reference/blockout, not image-to-3D reconstruction.
"""

import json
from pathlib import Path


def main():
    import bpy

    if bpy.app.version[:2] != (4, 5):
        raise RuntimeError("This operation supports Blender 4.5 LTS")
    request = json.loads(Path("request.json").read_text())
    if (
        set(request) != {"operation", "depth"}
        or request["operation"] != "reference_blockout"
    ):
        raise ValueError("Unsupported operation")
    depth = request["depth"]
    if type(depth) not in {int, float} or not 0.01 <= depth <= 2:
        raise ValueError("Invalid depth")
    # Factory startup contains no user scene. Remove only disposable factory data.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    image = bpy.data.images.load(
        str(Path("reference.png").resolve()), check_existing=False
    )
    width, height = image.size
    if not 1 <= width <= 4096 or not 1 <= height <= 4096:
        raise ValueError("Invalid reference")
    reference = bpy.data.objects.new("Authorized image reference", None)
    reference.empty_display_type = "IMAGE"
    reference.data = image
    bpy.context.collection.objects.link(reference)
    bpy.ops.mesh.primitive_cube_add(size=1)
    cube = bpy.context.object
    cube.name = "Reference blockout (not reconstruction)"
    longest = max(width, height)
    cube.scale = (width / longest, depth, height / longest)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    # Only the known mesh is selected. Reference empties are intentionally omitted
    # from the glTF: no images/materials/remote resources are exported.
    bpy.ops.object.select_all(action="DESELECT")
    cube.select_set(True)
    bpy.context.view_layer.objects.active = cube
    result = bpy.ops.export_scene.gltf(
        filepath=str(Path("scene.gltf").resolve()),
        export_format="GLTF_SEPARATE",
        use_selection=True,
        export_texcoords=False,
        export_normals=False,
        export_materials="NONE",
        export_animations=False,
        export_cameras=False,
        export_lights=False,
        export_extras=False,
    )
    if result != {"FINISHED"}:
        raise RuntimeError("Export incomplete")


if __name__ == "__main__":
    main()
