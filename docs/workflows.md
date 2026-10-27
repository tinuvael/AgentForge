# Bounded durable workflows

Directors choose the plan, registered Project, Agent/Worker or specialist tool,
inputs and limits. AgentForge validates the immutable plan and executes ordinary
Tasks/ToolJobs through its existing Application, SQLite owner and TaskEngine.
There is no automatic Worker/tool substitution or planning loop.

## Contracts and examples

The shared MCP catalog adds `submit_workflow`, `get_workflow`, `pause_workflow`,
`resume_workflow`, and `cancel_workflow`. Existing single-Task/specialist calls
retain their behavior. Submission takes `definition` and a client-scoped
`idempotency_key` (1–64 simple characters). An identical key/definition returns
its durable workflow; changing the definition under a used key is rejected.
The submitting identity comes from the authenticated Gateway, never a model field.
Trusted stdio uses the existing local operator identity.

Example `submit_workflow` arguments with enabled fake tools:

```json
{
  "idempotency_key": "reference-001",
  "definition": {
    "project_id": "REGISTERED-PROJECT-UUID",
    "max_attempts": 2,
    "timeout_seconds": 300,
    "steps": [
      {
        "step_id": "concept", "kind": "specialist", "tool_id": "fake-image",
        "intended_effect": "Generate a synthetic PNG reference"
      },
      {
        "step_id": "blockout", "kind": "specialist", "tool_id": "fake-blender",
        "intended_effect": "Generate a synthetic mesh linked to the reference",
        "depends_on": ["concept"],
        "artifact_inputs": [{
          "source": "step", "step_id": "concept", "output": 0, "format": "png"
        }]
      }
    ]
  }
}
```

Use actual configured tool IDs. Real Blender requires
`parameters: {"operation": "reference_blockout", "depth": 0.25}` and trusted
scope review. Its known cuboid is **not image-to-3D reconstruction**.

Inference steps require `kind: "inference"`, explicit `agent_id`, `worker_id`,
`request`, and `intended_effect`. For exploration → coder, make the coder depend
on the explorer and supply
`text_inputs: [{"step_id": "explore", "max_chars": 2000}]` plus
`required_validators: ["check"]`. Configure `check` as a trusted named validator
using [coding setup](coding.md). The coder writes only its isolated workspace.
Required validator evidence must show a successful actual run before step success;
a model claim alone is insufficient. Validation belongs to that coder Task's
workspace, not a fresh unrelated successor Task. Review its retained Task/workspace
and diff using existing operations; no workflow step commits, publishes or cleans it.

Definitions allow 1–16 steps, at most eight distinct earlier dependencies per step,
64 KiB serialized definition, 1–3 attempts per step and at most 48 total attempts.
The Director supplies topological order. Unknown IDs, cycles, wrong output types,
incompatible formats, unsupported step kinds and excess limits fail before execution.
Every planned attempt must fit `max_attempts`. Workflow timeout is 1–86400 seconds
from acceptance, including pauses and review waits. It requests cancellation at
expiry; it cannot promise immediate termination of remote effects.

Inference `limits` defaults to 8 turns, 12 tools, 90 seconds and existing bounded
context/output limits. A step can restrict but cannot expand its Agent's limits.
`task_policy` applies existing egress and request/token limits. Specialist `limits`
restrict configured runtime/output/request limits. One child constitutes one
specialist execution request; operator lifetime quotas still apply. No price
prediction is made, and unknown costs remain unknown.

## Lifecycle, attempts and recovery

Workflow/step snapshots expose pending dependencies, approval, queued/running,
completed/failed, paused, cancelling/cancelled, blocked and recovery-required states.
Attempts retain dispatch/child IDs, exact resolved inputs, approved scope hash,
created/started/finished/approved UTC timestamps, safe failures and bounded results.
Unstarted work has no invented start timestamp. One child per workflow is dispatched
at a time; separate workflows and ordinary work share all configured slots.

- Pause persists immediately and dispatches no successor. Accepted children may
  finish and their outcomes are observed even while paused.
- Resume rechecks current client grants; dispatch rechecks Project/root, Agent,
  Worker/tool fingerprint, egress, resource policy and input readiness/integrity.
  Revoked/unavailable authorization blocks safely. Restoration never grants approval.
- Cancellation durably stops dispatch before requesting owned active child
  cancellation through existing Task/ToolJob APIs. If current policy denies child
  cancellation, cancelling intent remains; an operator must restore rights or
  inspect the child. ComfyUI remote work may continue after local cancellation.
- A failed/cancelled dependency cannot satisfy a prerequisite. Failure retains
  evidence and blocks dependent work. It never silently counts as success/skip.
- Restart reconciles recorded identities. The dispatch intent commits before child
  creation. A Task's deterministic primary key or ToolJob's durable unique request
  identity closes the creation/linkage window without an in-memory deduplication
  cache. Completed children are reused. Client disconnect releases transport only.
- Interrupted Tasks and uncertain non-idempotent specialist effects remain
  recovery-required; resume observes the same child and cannot create a new attempt.
  Known ComfyUI receipts retain the [existing reconciliation](specialists.md).
  Operator receipt repair may let the same child finish; resume then observes it.
  No workflow receipt fabrication, blind remote retry, rollback or destructive
  workspace cleanup is provided.

`retry: {"max_attempts": 2, "on": ["provider_error"]}` explicitly permits a new
attempt for that safe failure. Eligible categories are `provider_error`,
`provider_timeout` for read-only inference, and `specialist_adapter_failed` for
simulated specialists. Coder and real specialist effects never automatically rerun.
Each new attempt has a new recorded dispatch identity; continued observation of a
known child is not a retry. Pauses/approval/recovery waits consume no inference calls.

## Authorization and review

Gateway clients need workflow operation grants and per-step underlying
`delegate_task/get_task/cancel_task` or
`submit_tool_job/get_tool_job/cancel_tool_job/inspect_artifact` grants, exact
Project/Agent/Worker/specialist bindings, and `coding_submit` for coder. The Gateway
resolves current policy by submitting client identity before each dispatch,
including after restart. Expired/revoked clients fail closed. No bearer credential
is stored in workflow state, results or prompts. Cross-client workflow access is
denied even with shared Task-history grants.

Repository-writing coder steps, cloud paid/egress targets, all real specialist
GPU/host operations and explicit `approval_required` checkpoints wait for trusted
operator review. Set operator-owned Worker `workflow_review_required = true` for
GPU-heavy or otherwise privileged local inference. No GPU/cost status is inferred
from a Worker name. Cloud egress must already be authorized by Project and client
policy; approval cannot override it. Destructive existing-scene Blender operations
are unsupported; adding one requires a separate authorized contract and review.

The existing trusted loopback dashboard/Companion exposes `/workflows`, individual
steps/attempts, exact resolved inputs, recorded execution targets, effects and
limits, child Task/ToolJob links, retained workspaces and artifact metadata.
Review approval is bound to the workflow, client, Project, immutable step, target
fingerprint, input identities/text, limits and attempt number. The unused exact
scope remains reviewable durably; it is consumed once. Any changed target/input
fails dispatch and cannot reuse approval. A new execution attempt requires its own
review. Ordinary harmless read-only/local and simulated steps require no extra
prompt. Existing target/Project authorization remains necessary at every boundary.

Approval exists only on trusted Application/operator HTTP, protected by the
existing Host/Origin/CSRF and explicit confirmation controls. It is deliberately
absent from MCP/model tools. Model planning, repository instructions and generated
messages cannot approve operations. Local Director uses the authenticated shared
workflow tools, records workflow IDs before observation and reports actual states,
child IDs and artifact IDs. Resume/cancel controls reuse its existing expiring
one-use human action approval. For server step review it links to `/workflows/<id>`;
the operator reviews there and restores known execution results in Director chat.
Binary retrieval remains excluded from the Director inference loop.

## Inputs and provenance

A step artifact reference names an explicit dependency, output index and format.
An existing artifact reference uses `source: "artifact"`, `artifact_id`,
`producer_job_id` and `format`. Consumption verifies the recorded successful
producer, Project/client ownership, readiness, expiry, SHA-256 and format. The
lineage is workflow → step → attempt → Task/ToolJob → artifact; specialist output
descriptors additionally preserve input artifact IDs. A shared-read grant cannot
turn another client's artifact into authority for a new effect.

Text references transfer only explicitly bounded completed final-answer text
(1–4000 characters per reference, at most four references), labeled untrusted with
producer step and Task identity. Binary payloads, artifact file locations, private
worktree control, hidden reasoning and automatic tool-body capture are not added.
Output snapshots include at most 4000 final-answer characters per inference attempt.
As with ordinary intentional Task text, final answers can contain untrusted source
claims; Directors must review evidence.

## Reproducible offline acceptance

```sh
python scripts/verify_workflows.py
python -m pytest tests/test_workflows.py -ra
```

The helper uses a disposable Git repository, private SQLite/artifact/worktree
storage, deterministic HTTP inference/executables and the authenticated public MCP
contracts. It verifies synthetic PNG → glTF provenance, real isolated coder writes
and named validation, primary-checkout preservation, concurrency one, restart and
Local Director actual workflow calls/results through a scripted native inference
HTTP endpoint. It runs in the clean installed-wheel checks and ordinary CI.
Git is required for this workflow acceptance. No installed Blender, GPU or billable
endpoint is used. It establishes neither image/mesh quality, performance,
printability nor physical Windows/Mac/Ollama/RTX 4080/ComfyUI/Blender/Xcode acceptance.
Those remain explicitly untested in [#31](https://github.com/tinuvael/AgentForge/issues/31).
