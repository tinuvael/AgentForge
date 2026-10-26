# Issue #52 Phase B implementation record

Starting point: merged PR #69 (`3d38edb`), new branch from `origin/main`.

1. Retain the existing Application, SQLite payloads, TaskEngine slots, ownership,
   Gateway policy and atomic artifact publication. Extend typed configuration and
   persist bounded execution evidence in ToolJob payloads (no schema change needed).
2. Implement an opt-in ComfyUI HTTP adapter: trusted API workflow, approved scalar
   mappings, durable intent/receipt, queue/history observation and one bounded PNG.
   Never replay uncertain submissions. Reconcile known receipts after restart.
3. Implement local headless Blender 4.5 with a packaged script and fixed argv:
   authorized PNG reference to an aspect-ratio blockout mesh, exported as glTF.
   Private staging, existing bounded process-tree runner, no supplied Python/paths.
4. Narrowly extend PNG metadata and glTF exporter validation; add mocked HTTP and
   executable acceptance, recovery/security regressions and installed-wheel checks.
5. Document setup, cancellation, recovery and explicit native acceptance. Run full
   release checks and all nine hosted jobs. Physical application/GPU checks stay #31.

Topology: ComfyUI is an operator-selected service reachable from the Application;
Blender runs on the Application host, never implicitly on an inference Worker.
Gateway clients transfer authorized artifacts through existing MCP operations.
Trusted Blender/Python and ComfyUI nodes are host code, not OS/network sandboxes.
Local Director remains discovery-only for specialists; external SDK/Codex clients
can submit and retrieve through the existing authorized MCP contract.
