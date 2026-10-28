# Blender exporter evidence

`blender-4.5.9-reference-blockout.gltf` is the output of the shipped
`specialists/blender_script.py` on Blender 4.5.9 LTS, build `8bf95cbd38d1`,
with a deterministic 1×1 PNG and `reference_blockout`, depth 0.25.
Only `scene.bin` was embedded into its data URI, as the production adapter does.
The cube has eight positions and 36 indices; the exporter omits the default
triangle mode. This profile is independent of `FakeBlender`'s triangle.

Official Intel macOS archive SHA-256:
`00c8a433504291374bfa045c0c2d708a779f8abc8400b4718fdd11c117486fa4`.
[Archive and checksums](https://download.blender.org/release/Blender4.5/).

Reproduce without a GPU, models or a ComfyUI service:

```sh
python scripts/verify_specialist_adapters.py --blender /absolute/Blender/4.5/blender
```

This invokes the real adapter, packaged script, process runner, artifact validation,
publication and restart using synthetic PNG input and a fixture ComfyUI transport.
The default invocation replaces the Blender executable with a deterministic process
that emits this captured export; it does not establish application compatibility.
Windows GUI/GPU and the real ComfyUI → Blender chain remain in #31.
