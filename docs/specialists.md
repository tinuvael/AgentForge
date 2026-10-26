# Specialist ToolJobs and private artifacts (#52 Phase A)

Phase A implements durable specialist execution with **deterministic fake adapters**.
No ComfyUI, Blender, GPU, external inference, paid call or network request is made.
A text Worker never becomes a specialist tool. The output is a valid 2×2 RGB PNG
and a self-contained glTF 2.0 triangle mesh. The triangle is synthetic; the fake
Blender adapter records the image relationship without image-to-3D inference.
Neither output quality nor 3D printability is established. Phase B and physical
acceptance remain open in [#52](https://github.com/tinuvael/AgentForge/issues/52)
and [#31](https://github.com/tinuvael/AgentForge/issues/31).

## Composition and lifecycle

`Application` optionally composes `SpecialistService`, `SpecialistRepository` and
`PrivateFiles`. Migration `0004_specialists` adds ToolJobs, descriptors and the
private store identity to the **existing SQLite database**. It does not change
Worker/Provider or existing Task contracts. There is no second task database,
executor, process owner, workflow DAG or automatic tool selection.

TaskEngine's existing bounded slots alternate between ordinary Tasks and ToolJobs
when both queues have work. They share startup, wakeup, shutdown and kernel database
ownership. ToolJob changes wake the shared lifecycle condition; durable typed
status polling is authoritative. Task progress/watch contracts remain unchanged;
ToolJobs do not fabricate LLM tokens or Task telemetry. Request counts are lifetime
admission counts per stable tool ID, including failed/cancelled jobs. At most 256
specialist jobs may be queued/running. Output bytes, store bytes, artifact count
and wall time have independent explicit limits. Limits do not claim GPU/VRAM
accounting or a hard process sandbox.

ToolJob states are `queued`, `running`, `completed`, `failed`, `cancelled` and
`interrupted`. Failure categories contain fixed safe codes, never raw adapter
exceptions. Jobs retain Project/tool/client identity, validated parameters, input
and output IDs, UTC timestamps, cancellation, request UUID, idempotency key,
optional Task correlation and a SHA-256 tool-configuration fingerprint.
The job also snapshots kind, adapter, connection ID, trust and limits, preserving
historical execution evidence if an operator later changes a registry definition.

Both request UUID and idempotency key are unique within the submitting client
identity (trusted local operations have a separate scope). Retry the **exact same
arguments**, including both keys, to obtain the same job. Reusing either key for
different arguments fails. No retry replays a terminal or interrupted operation.
Input expiry does not invalidate an otherwise authorized exact duplicate's durable
identity. A new operation needs new keys. Correlated Tasks must belong to the same
Project and submitting client; correlations confer no additional rights.

Queued jobs surviving restart revalidate live Project identity, current egress and
the exact tool fingerprint before executing. Configuration drift fails safely.
Running jobs become `interrupted/recovery_required`; an already committed cancel
wins. Running recovery occurs even if specialists are now disabled. Staged-file
cleanup waits for the configured, identity-checked store to be available. Shutdown
interrupts active operations and retains queued jobs. Cancellation before claim
prevents execution; running adapter cancellation is cooperative. Finalization yields
a cancellation checkpoint before the terminal transaction. Committed cancellation
wins over late completion; committed completion remains terminal.

## Storage and integrity boundary

An operator creates an absolute private directory **outside every registered
Project and every Git repository**. It must not contain Project roots either.
POSIX ownership/mode checks require the current user and no group/other permissions
(`0700`); use a private operator ACL on Windows. Runtime files and the control-plane
account/OS are trusted. Other processes running as the operator can tamper with
storage; hashes detect content changes but are not a cryptographic signature.

The store pins the canonical root and native filesystem identity in SQLite across
restart. Replacement or selecting a different directory fails closed; moving a
store is an offline operator backup/restore operation, not a model argument.
Backup SQLite and its artifact directory together while the executor is stopped.

Files use application-generated UUID names in one flat private directory. Public
names are bounded portable metadata, never storage paths. Reads are by opaque ID
and Project, with no arbitrary path, shell, Python, workflow JSON, endpoint or URL
argument. POSIX uses descriptor-relative no-follow file operations; Windows uses
native local NTFS handles with pinned ancestry and reparse/hardlink rejection.
The shared native mutation primitive also preserves coding's existing API.

Finalization reserves bytes/counts in a staged SQLite row **before writing**. It
writes an exclusive pending file, flushes bytes, atomically renames without
replacement and flushes directory metadata (POSIX) or flushes the renamed native
handle (Windows). It rereads and verifies content before completion. One SQLite
transaction marks all declared outputs ready and completes the job. A failure or
cancel leaves no visible partial output. After process loss, staged files are
removed through the same safe boundary; interrupted jobs are never replayed.
Cleanup failure leaves reserved bytes and fails closed for operator investigation.

Finalized content has no mutation API. Every inspect/retrieve/input read checks
size, SHA-256 and format; missing/corrupt bytes are explicit and never served.
Descriptor lists show stored finalization facts; use `inspect_artifact` for current
integrity. Expired metadata remains as a tombstone, preserving provenance. Binary
payloads never enter ordinary SQLite records, Task answers or Director prompts.

The current verified profiles are noninterlaced 8-bit RGB/RGBA PNG (CRC, dimensions,
bounded decompression and scanline filters) and self-contained indexed triangle
glTF with finite positions, bounded views/indices and embedded buffers. External
resources/extensions/scripts are outside this profile. `blend`, `stl` and
`gaussian_splat` are reserved distinct format identities and currently **rejected**
by the validator. Phase B must add reviewed validators before emitting them.
`geometry=mesh` only means the checked representation contains mesh primitives;
`printability=not_assessed` is mandatory. Gaussian splats must never be relabeled
as meshes or printable geometry.

Retention defaults to seven days, configurable from one second to one year.
Startup cleans expired files. A trusted host operator can invoke
`application.specialist_service().cleanup()` on the owning event loop for explicit
maintenance; it takes the same database ownership guard and retains descriptors.
It is deliberately absent from MCP. Quota reservations are released only after
safe deletion; missing files are safely treated as already removed. Expired inputs
are rejected. Storage pressure never silently evicts unexpired data.

## Operator setup

Stop executors, back up private state, then explicitly upgrade:

```sh
agentforge db upgrade --database-url sqlite:////absolute/private/agentforge.db
```

Copy [specialists.example.toml](../config/specialists.example.toml) into private
operator configuration, set a pre-created absolute directory, and explicitly set
`enabled=true` for the two fake definitions to exercise them. There is no default
configuration and both example tools are disabled. Enable with the existing host:

```sh
agentforge mcp --database-url sqlite:////absolute/private/agentforge.db \
  --workers /absolute/private/workers.toml \
  --specialists /absolute/private/specialists.toml --companion
# Or add the same --specialists argument to `agentforge gateway serve` / `web`.
```

Use Windows drive-absolute paths and the existing local NTFS ownership policy on
Windows. There is no automatic installer or application launch.

Gateway clients need each operation in `ClientPolicy.tools`, plus explicit
`specialists` IDs within each Project grant. Existing grants default to no
specialists; Worker grants confer no specialist rights. Example Project grant:

```json
{"project_id":"REGISTERED-PROJECT-UUID","workers":["local-text"],
 "specialists":["fake-image","fake-blender"]}
```

Use the existing private credential provisioning/update process, preserve token
hashes and other grants, and keep tokens out of model prompts/browser code.
Gateway revalidates current credentials and independently checks operation, Project,
tool and owner on each request. Reading another client's results requires
`read_shared`; cancelling them additionally requires `cancel_shared`. Input artifact
ownership is checked before accepting a job, not merely before retrieval. Lists
apply owner/tool filters before pagination. Project local-only policy rejects
cloud-classified tools; cloud classification additionally needs the selected
`connection_id` in the Project's explicit cloud allowlist. Fake cloud classification
tests policy only and still performs no network call. Untrusted text cannot select
a host/connection or expand these grants. Revocation denies subsequent client access;
it does not retroactively undo already authorized submissions.

The trusted loopback operator dashboard adds **ToolJobs** history/detail pages,
refresh, existing CSRF-protected cancellation and artifact metadata. It labels
simulation throughout. Binary retrieval is via MCP; there are no inert download
controls or unauthenticated file routes. Existing Host/Origin/CSP/CSRF controls and
one-owner Companion lifecycle apply unchanged.

## MCP and Director contract

The shared typed catalog adds:

| Operation | Required identity and behavior |
| --- | --- |
| `list_specialist_tools` | Project UUID, bounded pagination; permitted enabled fake definitions and diagnostic evidence. |
| `submit_tool_job` | Project/tool IDs, input artifact IDs, `parameters={"seed":0}`, request UUID and idempotency key; returns promptly with durable identity. |
| `get_tool_job` | Job UUID; server looks up Project/tool/owner. |
| `cancel_tool_job` | Job UUID; durable cancellation, safe to repeat. |
| `list_artifacts` | Project UUID and bounded pagination; descriptors only. |
| `inspect_artifact` | Project/artifact UUIDs; verified descriptor or missing/corrupt/expired status. |
| `retrieve_artifact` | Project/artifact UUIDs, offset and `max_bytes<=49152`; base64 chunk, next offset, full size/hash. |

Retrieval is an **explicit client binary-transfer mechanism**, not an ordinary
model-context tool. Clients should stream chunks to their own authorized storage,
verify the full hash, and keep binary content out of inference prompts. Each
request independently reauthorizes and checks the entire bounded file; there are
no public URLs or capability tokens that survive revocation.

Codex can use the new Gateway/stdio catalog. Local Director discovers permitted
fake tool definitions through the shared catalog; specialist mutations, job/artifact
content and binary retrieval remain outside its Phase A inference loop. This keeps
its existing conversation ownership, approvals and resume semantics intact until
a dedicated specialist client workflow is designed. No real application
availability is inferred from a tool being listed.

## Reproducible offline acceptance

From source with the development venv, or with the wheel installed and the smoke
script available outside the checkout:

```sh
python scripts/verify_specialists.py
python -m pytest -ra tests/test_specialists.py
python -m pytest -ra
ruff check .
ruff format --check .
git diff --check
python -m build
python scripts/ci_verify_wheel.py
```

The demo explicitly submits image then Blender ToolJobs through SDK MCP with
Gateway-style scoped access, retrieves the files, repeats submission without
duplicate execution, restarts Application and verifies the same descriptors/hash.
The image is a deterministic colored square and the glTF is a known triangle. No
workflow graph or model quality judgment is involved. Installed-wheel verification
includes the demo. The unchanged nine-job Ubuntu/Windows/macOS × Python
3.12/3.13/3.14 matrix also runs the security, process-loss and lifecycle regressions.

## Phase B adapter integration and native acceptance (not implemented/tested)

Extend the typed specialist definition with a new **explicit** adapter and
operator-owned connection configuration; never reinterpret Worker endpoints.
The async `Adapter.run(parameters, inputs)` contract receives only validated
parameters and authorized verified input bytes and returns declared `Output`
objects (name, format, MIME, bounded bytes). The application owns job IDs, policies,
provenance, deadlines, quotas, finalization, cancellation and terminal persistence.
Adapters must bound reads **before buffering**, respect cancellation, report fixed
safe errors and avoid returning success for incomplete exports. Add named operations
and validators to the contract without replacing ToolJobs/private storage. A
non-idempotent remote submission must save its remote receipt before further I/O;
recovery must reconcile that receipt explicitly rather than blindly resubmitting.
Real adapters may require new safe persisted receipt fields and migrations.

ComfyUI must use an operator-approved workflow and exact authorized connection.
Bound request/poll/result sizes, disallow arbitrary node installation or filesystem
selection, authorize retrieval origins and validate every downloaded output. A
remote interrupt endpoint may affect other jobs: define ownership-specific cancel
semantics before enabling it. Blender must accept named approved scene operations
and scoped inputs/exports, with bounded process/host resource and descendant cleanup.
Never accept arbitrary Blender Python from a model. A native bridge or Blender
Python is trusted host execution, **not an OS/network sandbox**; deployment needs
separate OS isolation when code/plugins are not trusted.

The following native diagnostics are optional trusted-operator prerequisites,
**not AgentForge real-adapter commands** and not proof of generation:

```powershell
nvidia-smi
& 'C:\Program Files\Blender Foundation\Blender 4.5\blender.exe' --version
# Only after selecting and authorizing the local ComfyUI host/port:
Invoke-RestMethod 'http://127.0.0.1:8188/system_stats'
```

Use the actual installed Blender path/version. After Phase B lands, add/run its
explicit adapter acceptance entrypoint on the RTX 4080 host. Keep these checklist
items untested until evidence is recorded in #31:

- Actual approved ComfyUI workflow, image quality, GPU/VRAM/time, output formats and
  bounded retrieval; no install of unapproved custom nodes or implicit paid calls.
- Actual Blender import of the authorized image, named scene operation/export,
  scene/mesh versus Gaussian splat truthfulness, file integrity and host integration.
- Native queued/running/finalization cancellation, process crash, reconnect and
  ambiguous non-idempotent remote submission recovery without duplicate execution.
- Two real scoped Codex clients, egress denials, local NTFS/APFS private ACL/path
  boundaries, simultaneous ordinary Tasks and ToolJobs, limits and retained files.
- Independent mesh topology/manifold/scale checks and, only when separately tested,
  manufacturing suitability. A valid glTF/STL or a fake triangle proves none of it.

No Phase B executable/CLI is shipped here; do not mark these checks complete from
hosted CI or this synthetic demo.
