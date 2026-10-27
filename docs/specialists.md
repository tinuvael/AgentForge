# Specialist ToolJobs and private artifacts (#52)

AgentForge supports disabled-by-default **real ComfyUI HTTP** and **local headless
Blender 4.5 LTS** adapters alongside the unchanged deterministic fake adapters.
Specialists are separate from inference Workers. An external Codex/SDK client
explicitly selects the tool and passes authorized artifact IDs through existing MCP.
Real application, RTX 4080, GUI, image quality and manufacturing acceptance remain
**untested** until recorded in [#31](https://github.com/tinuvael/AgentForge/issues/31).
Blender creates known blockout geometry using image dimensions and a temporary image
reference; it does **not** reconstruct a 3D object from pixels.

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
Running jobs become `interrupted/recovery_required`; known ComfyUI receipts may
re-enter the shared queue for observation/retrieval only. An already committed cancel
wins. Running recovery occurs even if specialists are now disabled; receipt reconciliation
requires the original enabled tool fingerprint and current Project policy. Staged-file
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
glTF with finite positions, bounded views/indices and embedded buffers. PNG permits bounded opaque `tEXt` metadata from ComfyUI
SaveImage (never authorization/provenance). glTF also accepts the selected Blender
exporter's names, finite transforms, accessor offsets and uint16/uint32 indices;
triangle mode may be omitted per glTF defaults. Only one mesh/primitive/node and
POSITION attributes are supported. External resources, images, textures, materials,
animations, extensions and scripts are outside this profile. `blend`, `stl` and
`gaussian_splat` are reserved distinct format identities and currently **rejected**
by the validator. Adding extensions to filenames does not enable formats.
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
configuration and all example tools are disabled. Enable with the existing host:

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
| `list_specialist_tools` | Project UUID, bounded pagination; permitted enabled definitions with simulation/configuration evidence and diagnostic evidence. |
| `submit_tool_job` | Project/tool IDs, input artifact IDs, typed parameters (see below), request UUID and idempotency key; returns promptly with durable identity. |
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
tool definitions through the shared catalog; specialist mutations, job/artifact
content and binary retrieval remain outside its inference loop. This keeps
its existing conversation ownership, approvals and resume semantics intact until
a dedicated specialist client workflow is designed. No real application
availability is inferred from a tool being listed.

## Reproducible offline acceptance

From source with the development venv, or with the wheel installed and the smoke
script available outside the checkout:

```sh
python scripts/verify_specialists.py
python scripts/verify_specialist_adapters.py
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
includes both the unchanged fake demo and real-adapter code exercised through
mocked HTTP and a real bounded executable fixture. The unchanged nine-job
Ubuntu/Windows/macOS × Python
3.12/3.13/3.14 matrix also runs the security, process-loss and lifecycle regressions.

## Real adapter topology and setup

The **single Application host** owns SQLite, private artifact storage and TaskEngine.
ComfyUI runs on an explicitly configured reachable service; Blender runs on this
Application host. Neither a remote Gateway client nor an inference Worker's machine
implicitly becomes an execution host. No generic bridge/remote shell is provided.
Keep SQLite on local supported storage and use the existing protected Gateway for
remote clients. No service is probed by discovery or dashboard page loads.

The [configuration example](../config/specialists.example.toml) contains two real
and two fake definitions, all disabled. Create a separate private staging directory
outside repositories **and outside the artifact store**, `0700` on POSIX or a private
operator ACL on Windows. Select an absolute Blender 4.5 executable, set its SHA-256,
and enable only the required tools. For example:

```sh
mkdir -m 700 /absolute/private/blender-staging
shasum -a 256 /absolute/Blender/4.5/blender
```

Windows uses the actual installed drive-absolute executable, for example
`C:\Program Files\Blender Foundation\Blender 4.5\blender.exe`; get its hash with
`Get-FileHash -Algorithm SHA256`. On macOS choose the executable inside the app
bundle. Tool fingerprints bind configuration, approved workflow, executable hash
and packaged script hash. Updating any binding requires new job requests; queued
jobs cannot silently change execution identity.

ComfyUI configuration embeds a reviewed **API-format** workflow as `workflow_json`
(maximum 256 KiB / 256 nodes). Export API format from ComfyUI, review all node code,
model names, network and filesystem effects, then copy into private TOML. The
example uses built-in nodes and an operator-selected **already installed** checkpoint.
AgentForge does not install models, custom nodes, plugins or Blender. Set a fixed
batch of one and one configured `SaveImage` node. Extra outputs are not published.
The adapter replaces that node's filename prefix with a ToolJob-specific flat prefix.

Only these client parameters are supported:

| Adapter | Parameters | Authorized input / output |
| --- | --- | --- |
| Fake adapters | `seed`: uint32 (default 0) | Same synthetic PNG / triangle as Phase A |
| ComfyUI | `seed`: uint32; `text`: at most 2,000 characters | No inputs; exactly one PNG from the configured SaveImage node |
| Blender | `operation="reference_blockout"`; `depth`: 0.01–2 metres (default 0.25) | Exactly one ready authorized PNG; one self-contained glTF mesh |

Seed maps only to an approved `KSampler.seed`; text maps only to an approved
`CLIPTextEncode.text`. Omit mappings to keep those workflow values fixed (nondefault
client values then fail). No workflow/node classes, URLs, host paths, Python,
executable arguments or importer/exporter names are accepted from a model.
`list_specialist_tools.parameter_contracts` reports the allowed fields; private
workflow, executable, staging and auth configuration are excluded from responses.
Discovery says `configured_untested` for real adapters. Per-job evidence reports
actual observation and validated output, distinct from physical owner acceptance.

Use literal loopback HTTP (`127.0.0.1` or `[::1]`) for local ComfyUI; DNS names are
not allowed for cleartext operation. Other origins require HTTPS, valid server TLS,
and any operator-configured bearer credential via `api_key_env`. No URL credentials,
queries, fragments, base paths, redirects (even same-origin), environment proxies
or compressed HTTP responses are accepted. The service must support its origin's
classic `/prompt`, `/queue`, `/history/{prompt_id}` and `/view` routes. JSON responses
and PNG downloads are bounded while streaming, with per-request timeouts plus the
ToolJob deadline. Remote cloud trust requires the Project connection allowlist;
setting a local label is an operator trust decision, not network attestation.

ComfyUI/its nodes and Blender/Python are **trusted host execution, not an OS/network
sandbox**. Run only reviewed nodes and trusted Blender installations/scripts under
an appropriately restricted OS account/container when needed. The Blender process
uses factory startup, disables auto-execution, a conservative credential-free
environment, no stdin/shell and a fixed packaged script. It loads only the staged
PNG and builds a cuboid whose longest image-plane dimension is one metre and whose
depth is explicit. Its image-reference empty is temporary and is omitted from the
export; pixels are not a mesh/texture in the output. Only the selected known mesh
is exported, with normals/materials/textures/animations disabled. Blender writes
`scene.gltf` plus `scene.bin`; the adapter accepts only those fixed regular files,
embeds the sole checked buffer and validates before publication. No `.blend`, STL,
GLB, Gaussian splats, arbitrary imports or destructive scene operations are enabled.
The factory scene is disposable; no existing user scene is opened or overwritten.
`geometry_source=known_blockout`, input IDs/hash chain and `printability=not_assessed`
remain in descriptors. Valid format is not image reconstruction or manufacturing
assessment. Runtime/log/output/storage caps do not constitute an OS CPU/RAM/disk/GPU
sandbox; trusted applications must not write unbounded intermediate data.

## Durable recovery and cancellation

ToolJob JSON payloads add optional typed `execution` evidence; the existing
`0004_specialists` tables already support additive payload fields, so **no DDL
migration is necessary**. Old descriptors and Phase A request hashes remain readable.
Execution binding records host class, safe connection origin, configuration hash,
parameters and input IDs; no credential values or raw remote errors/logs are stored.

| Evidence | Meaning |
| --- | --- |
| `not_submitted` | No submission intent committed |
| `submission_unknown` + intent SHA-256 | Intent committed before submission; process loss may mean the service accepted it, even without a receipt |
| `accepted` + remote job UUID | ComfyUI receipt committed before polling; queue/history states are observations |
| `outputs_validated` + output hash/size | Retrieved/exported bytes passed validation; only `state=completed` makes descriptors visible |
| `remote_may_continue=true` | Remote completion is not established; local cancel/timeout never proves remote termination |

ComfyUI receives one POST only. Classic submission is **not exactly-once**: a
crash/network timeout after acceptance but before receipt persistence cannot be
resolved by repeating POST. Same-key AgentForge retries return the existing job.
On restart, interrupted jobs with known receipts and unchanged authorization/config
are queued for history/queue reads and retrieval, never a second submission.
Reconciliation respects the 256-job admission cap; excess interrupted receipts
remain retained for a later restart/operator recovery. A previously validated
output hash/size cannot change on reconciliation.
Missing history/queue entries are not proof of non-execution: observation ends at
the deadline with `interrupted/recovery_required`. No blind replay of ambiguous
ComfyUI or interrupted local Blender work occurs. Cancellation remains terminal.

For a missing receipt, stop the Gateway/other executor and back up SQLite + private
files. On the **authorized ComfyUI service**, inspect queue/history and private
operator logs for `client_id=<ToolJob UUID>` and prefix `agentforge_<UUID without
hyphens>`. Do not paste workflow bodies or credentials into issues. If a matching
receipt is found, use the same config and owning host:

```sh
python scripts/reconcile_specialist.py \
  --database-url sqlite:////absolute/private/agentforge.db \
  --workers /absolute/private/workers.toml \
  --specialists /absolute/private/specialists.toml \
  --job TOOLJOB-UUID --receipt COMFYUI-PROMPT-UUID
```

The operator-only method verifies **both** identities in upstream history/queue
before attaching the immutable receipt and re-entering the existing execution queue.
It is absent from MCP/Director/web. A wrong receipt, cancelled job or changed binding
is refused. This recovery process owns the same database lock and also resumes
eligible existing queued work. Restart the Gateway after it exits. If no matching
receipt can be established, retain the recovery-required job and investigate the
service manually. Absence from a pruned history is not safe replay authorization.
Only an operator's separately considered new request may start a new operation.

ComfyUI cancellation stops local observation/retrieval and publication. **It does
not stop the remote job**, delete its files/queue entry or release remote GPU work.
The adapter sends no `/interrupt` or cancellation request: older/global interrupt
behavior and version-dependent targeted routes do not provide this adapter's
cross-client isolation contract. Receipt/intent and possible continued execution
remain visible after cancellation and restart. Use ComfyUI's own trusted operator
controls after identifying the correct job and any impact on other clients.

Blender timeout/cancellation awaits existing process-group cleanup on POSIX or
kill-on-close Windows Job cleanup, including descendants, before removing staging.
After a hard Application/OS crash, do not assume POSIX descendants died: inspect
operator processes/cwds for `job-<ToolJob hex>-*`, stop only the identified process
tree, then remove its private leftover staging directory. Windows Job close normally
terminates descendants, but verify after a host failure. Interrupted Blender jobs
are never auto-launched again. Logs are drained with caps; oversize logs cause
failure, and raw host paths/log bodies never become public failure metadata.
Partial exports/downloads/publications never become ready artifacts. Startup cleans
staged artifact rows/files; unknown Blender staging remains for operator inspection.

## Native acceptance commands — opt-in, currently untested

First record installed AgentForge SHA, OS/filesystem/ACL, actual Blender 4.5 version,
ComfyUI commit, reviewed workflow/node/model hashes and GPU/driver versions in #31.
Start the existing authenticated Gateway with the reviewed `--specialists` config,
a disposable registered Project and a client granting the seven specialist
operations and explicit real tool IDs. Keep secrets in private environment only.

```powershell
nvidia-smi
& 'C:\Program Files\Blender Foundation\Blender 4.5\blender.exe' --version
Invoke-RestMethod 'http://127.0.0.1:8188/system_stats'
```

Then (with `AGENTFORGE_ACCEPTANCE_TOKEN` already set securely):

```sh
# Actual ComfyUI/RTX 4080 generation + actual local Blender exchange:
python scripts/accept_specialists_native.py \
  --gateway https://YOUR-PROTECTED-GATEWAY/mcp --project PROJECT-UUID \
  --image-tool comfy-image --blender-tool blender-blockout --seed 42
# Blender alone with an existing authorized PNG from the same client/Project:
python scripts/accept_specialists_native.py \
  --gateway https://YOUR-PROTECTED-GATEWAY/mcp --project PROJECT-UUID \
  --blender-tool blender-blockout --input-artifact ARTIFACT-UUID
```

The command submits typed MCP requests, checks exact duplicate identity, polls,
retrieves bounded chunks and verifies full SHA-256, printing sanitized job/descriptor
evidence. It neither installs applications nor verifies visual quality, GPU use,
GUI integration or printability. An SDK timeout may leave accepted work running;
retain the printed job ID and inspect/recover instead of rerunning the command.
Separately exercise queued/running/finalization cancellation, two scoped clients,
revocation, egress denials, shared capacity, service loss, receipt ambiguity and
forced restart. Inspect the glTF in the real target application and independently
assess any intended manufacturing use. All physical rows stay **untested** in #31
until actual evidence exists; hosted mocks do not complete them.

## Verified upstream contracts

Reviewed official [ComfyUI routes](https://docs.comfy.org/development/comfyui-server/comms_routes),
[server.py](https://github.com/comfyanonymous/ComfyUI/blob/master/server.py),
[queue history](https://github.com/comfyanonymous/ComfyUI/blob/master/execution.py)
and [SaveImage](https://github.com/comfyanonymous/ComfyUI/blob/master/nodes.py).
The adapter deliberately uses the classic server-generated prompt ID API rather
than assuming newer client-ID deduplication or per-job cancellation support.

Blender [4.5 command line](https://docs.blender.org/manual/en/4.5/advanced/command_line/arguments.html),
[exporter API](https://docs.blender.org/api/4.5/bpy.ops.export_scene.html) and
[4.5 exporter source](https://github.com/blender/blender/blob/blender-v4.5-release/scripts/addons_core/io_scene_gltf2/__init__.py)
define the supported invocation and separate glTF export. Embedded-export preference
is not assumed; AgentForge embeds the sole controlled `.bin` after export.
