# Issue #53 design record

Base: merged PR #70, `1eb83fc3dfb24efa61aefdbb4194cb814c301b13`.
This record captures the decisions made before implementation.

- Immutable typed ordered DAG: 1–16 stable step IDs, at most eight earlier
  dependencies per step, inference Agent/Worker or specialist tool bindings.
  No expressions, templates, recursive planning or dynamic tool selection.
- One durable workflow snapshot contains bounded step/attempt history. Additive
  `0005_workflows` preserves existing Tasks, ToolJobs, artifacts and workspaces.
  An attempt's deterministic dispatch UUID and resolved inputs are committed
  **before** child creation. Tasks use this durable primary key; specialists use
  existing unique client/request/idempotency identities. A lost link is recovered
  by the recorded identity, including after process death.
- TaskEngine synchronously advances plans at queue boundaries. One child per
  workflow runs at a time; other workflows and ordinary work share its existing
  capacity. Waiting never occupies an execution slot. The existing executor's
  deadline wakeup handles idle plans; there is no coordinator task or second pool.
- Pending → approval (when required) → queued → running → completed/failed.
  Dependency failure blocks successors. Pause prevents dispatch while retaining
  accepted outcomes. Cancel first persists intent, then requests existing owned
  child cancellation. Recovery-required records never authorize a new attempt.
- Restart observes existing children; completed work is reused. Known ComfyUI
  receipts follow existing reconciliation. Unknown acceptance/local interrupted
  effects require operator investigation, never blind resubmission or rollback.
- Attempts are bounded (1–3 per step, 48 total maximum). Automatic new attempts
  cover only explicitly selected safe read-only inference or simulated adapter
  failure categories. New privileged attempts require fresh scope review.
- Trusted operator scope review uses the existing local HTTP security boundary;
  no model approval operation exists. Gateway re-resolves current client policy
  by identity, without credentials in the workflow. Local Director keeps its
  existing human approval mechanism for workflow resume/cancel controls and
  reaches workflow execution through the shared authenticated Gateway catalog.
- MVP includes artifact references, bounded final-answer text, existing isolated
  coder with required named validator evidence, operator lifecycle screens and
  deterministic application/MCP/HTTP acceptance. Existing Blender operation is
  a reference blockout; it does not reconstruct a mesh from an image. Physical
  acceptance remains #31.
