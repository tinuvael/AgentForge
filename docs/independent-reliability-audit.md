# Independent reliability audit — 2026-10-28

Audited production baseline: **`01314210f8875610711805850d9c5d8e85708beb`**,
fetched `origin/main`. Branch `audit/independent-reliability-20261027` was created
directly from it. No main update, merge or automatic merge was performed.
Unrelated local `reports/` were preserved. Physical acceptance remains **OPEN**
in [#31](https://github.com/tinuvael/AgentForge/issues/31).

[PR #73](https://github.com/tinuvael/AgentForge/pull/73) was still **open/unmerged**
at `ffd27098026e6dba522ea095585025e7c06a1390`. Its report and #72 were read as
background, then production paths were independently traced. Its Windows Git
discovery, binary-install instructions, cache-free validators, Mac connection
trust, Director Clear/output and Git-test corrections are **not included or
silently required here**. Those known main-branch limitations remain until #73
is reviewed and merged. The three findings below are additional defects.

## Environment and method

Intel x86_64 macOS **26.6.2**, Darwin **25.6.0**, Python **3.13.2**, SQLite
**3.45.3**, Apple Git **2.50.1**. Repository and temporary state were on local
**APFS** (`/System/Volumes/Data`); a native temporary-file probe confirmed case
insensitivity. Python, Git, actual subprocesses, loopback HTTP and the in-app
browser were used. Codex CLI **0.164.0-alpha.11.1** and Xcode **26.6 / 17F113**
were available; no live Codex model session or Xcode project validator was run.
`gh`, Ollama and Blender were absent from PATH. No model/GPU/ComfyUI/Windows
installation, download or paid inference was needed.

Development tools were installed with `python -m pip install -e '.[dev]'`.
Source checks used MCP 1.30.0, httpx 0.28.1, FastAPI 0.162.0, Starlette 1.9.0,
Pydantic 2.14.1, SQLAlchemy 2.1.6, Alembic 1.20.1, pytest 9.1.1 and Ruff 0.17.2.
An independent archived-baseline venv used pytest 9.2.0, Starlette 1.10.0 and
Ruff 0.17.3. Fresh installed-distribution environments used Starlette 1.10.0.
Binary dependency resolution selected cryptography 48.0.1 without a version pin.
Exact environment/freeze files and logs were retained under
`/private/tmp/agentforge-independent-*`, outside version control.

The unchanged baseline archive, correctly installed into its own venv, ran
**1,741 passed / 29 skipped / 1 failed** in **760.61 s**. The failure is the
existing macOS Git-inspection test's cwd-dependent command shim, already fixed
in unmerged #73. An initial archive run incorrectly mixed its source imports with
the checkout's editable subprocess imports: **1,728 passed / 29 skipped / 14
failed**; it is not treated as product evidence. An intermediate final run was
interrupted after 316 passes when real-browser testing exposed the third defect.

## Confirmed findings and fixes

| Severity | Supported scenario and baseline consequence | Small correction and evidence |
| --- | --- | --- |
| **High** | A coder passes required named validation. Its workflow waits for successor approval. The workspace is edited or explicitly cleaned up, possibly across pause/restart/resume. `advance()` skips the already completed step, dispatches the successor and reports workflow completion using stale or missing code evidence. | Recheck completed required-validation steps while the workflow remains active. Missing/stale workspace identity fails acceptance with `validation_required`; the original Task/result remains retained and the successor gets no child ID. Three failing baseline cases cover edit, supported cleanup and restart; an unchanged-workspace control succeeds. Real Git worktrees and named validator subprocesses are used. |
| **Medium** | A compatible server returns nonempty partial content, or valid-looking calls, with `finish_reason=content_filter`. Tasks report success or execute the calls; SSE also publishes a successful terminal event for filtered/unsupported reasons. | Accept only `stop`, `length`, `tool_calls` at the adapter boundary. Other terminal signals fail before returning calls or terminal success. Existing output-limit behavior remains. Eight failing baseline cases cover durable SQLite Task results, no continuation/tool execution, JSON and SSE, and cleanup. HTTP responses use MockTransport; no external inference occurs. |
| **High / first use** | The real browser sends a workflow approval as a navigation POST under `Referrer-Policy: no-referrer`: `Origin: null`, `Sec-Fetch-Site: same-origin`, **403**. Approvals and workflow controls are unusable; the ToolJob cancellation form has the same wiring. ASGI clients omitted Origin and concealed the browser behavior. | Use packaged HTMX for those forms and return a full GET through `HX-Redirect`. Keep no-referrer, strict Origin/CSRF, confirmation, scope and replay checks. Four baseline regression failures cover workflow and ToolJob forms plus redirects. A real browser then sent the concrete loopback Origin and received **200**. |

The [protocol reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
defines `content_filter` as omitted content, so it cannot certify a complete
answer. Streaming text deltas remain provisional until a validated terminal event.

The exact final regression selection was run against the separately installed,
unchanged baseline: **15 failed / 1 passed**. It consists of
`test_required_validation_stays_fresh_while_successor_awaits_review`,
`test_incomplete_terminal_signal_is_not_success`, the two filtered cases in
`test_wire_finish_reason_controls_durable_task`,
`test_dashboard_scope_review_csrf_and_lifecycle`, and
`test_dashboard_tool_job_cancellation_preserves_origin`.
The same selection after the fixes: **16 passed**, **19.08 s**.

## Public interfaces and native process evidence

One disposable browser scenario used actual CLI configuration, two idempotent
migrations, a committed synthetic Project, **one separate Gateway process** with
its borrowed Companion, an authenticated official MCP client and a deterministic
native loopback Ollama-protocol HTTP server. No provider production code was
replaced. The only Gateway instrumentation recorded Origin, Fetch Metadata and
HTTP status; no credentials, request bodies or CSRF tokens were logged.

Baseline browser approval returned 403 without inference. With the fix, browser
approval ran the isolated coder and the real named Python validator. After an
out-of-band edit confined to that disposable worktree, approving the successor
produced `validation_required`, a blocked successor and no second Task. Browser
and authenticated MCP agreed. SIGINT shutdown and a new Gateway process retained
the same workflow and child IDs with **two total inference calls**, no replay and
unchanged primary source/HEAD.

Recorded fixture identities: workflow
`642b0c50-e426-4a25-8642-9ddb42cffce3`, coding Task
`ebaa8d27-4bbb-5d74-abb2-72ae591abde1`. Local evidence includes
`agentforge-independent-ui-baseline/browser-headers.jsonl`,
`agentforge-independent-ui-fixed/result.json` and
`agentforge-independent-browser-result.png` beneath `/private/tmp`.

## Scope challenged without additional confirmed defects

| Subsystem | Production paths traced and verification boundary |
| --- | --- |
| Projects, index, repository/Git | Registration identity → anchored descriptor access → scanner/transaction/cache → tools; native APFS alias/root replacement and bounded Git subprocess checks. Cached/stale structural records are distinct from live source. |
| Workers, agents, providers | Explicit connection/target binding, egress/resource admission, strict JSON and partial stream parsers, per-turn tool authorization and bounded outputs. Filtered termination is corrected above. |
| Tasks, Councils, observation | Durable claim/finish/cancel, atomic Council creation, shared slots, observer disconnects and retained results. Ordinary Task submission has no idempotency key; an ambiguous response must be reconciled, never blindly replayed. |
| Isolated coding | Provision/bind/finalize/cleanup, primary checkout invariants, latest named validation, pre/post content identities, output bounds and process-tree termination. Workflow acceptance gap is corrected above. |
| Gateway and policy | Authentication → current client grants → ownership/read/cancel policy; Host/Origin/body/session limits, live revocation and policy CAS. Accepted Tasks may finish after revocation by documented design. |
| Codex integration and Director | Disposable config install/dry-run/backups, discovery, reservations before I/O, lost-response recovery and CLI/web caller paths. #73's unfinished-workflow Clear/output defects remain separate. |
| Workflows | Precommitted deterministic dispatch identities, child linkage, dependency inputs, human-only scope approval, deadlines including waits, bounded safe retries and concurrency-one progress. |
| Specialists/artifacts | Durable ComfyUI intent/receipt and recovery reads without blind resubmission, local cancellation, artifact publication/hash/retention, fixed Blender subprocess invocation. HTTP/executable fixtures only; no real applications. |
| Database and lifecycle | Packaged migration chain, historical retention, commit-failure invalidation, cross-process owner/alias rejection and shutdown/restart. SQLite remains local. |
| Packaging, docs and UI | CLI/module entrypoints, sdist/wheel resources and clean imports, examples, Dashboard/Companion routes and forms. Actual browser actions supplemented ASGI tests and found the third defect. |

Existing process-death, transport ambiguity, cancellation and storage-fault tests
were reused. Their production intent/receipt/transaction paths were inspected;
passing names or prior CI were not treated as independent proof. No confirmed
duplicate external submission was found in the exercised idempotent paths.

## Final validation

Full source regression before the CI fixture correction below:
**1,757 passed / 29 skipped / 0 failed**, **771.63 s**.
All skips require native Windows; the deliberately invalid SQLite-URI test retains
its existing warning. Exact published head and its CI results are recorded in
[review PR #74](https://github.com/tinuvael/AgentForge/pull/74). No pending check is
counted as passed.
The final-head full regression is recorded there after completion.
Both independent clean installed-distribution acceptances and their `pip check`
completed successfully on this Mac. Ruff lint/format and working-tree whitespace
also passed. A focused helper run without native loopback permission produced
**191 passed / 1 skipped / 1 failed** solely at its loopback bind; the installed
and full regressions use the necessary permission and do not count that failure
as a product pass.
The required command set is `python -m pytest -ra`, `ruff check .`,
`ruff format --check .`, `git diff --check`, committed-range
`scripts/ci_check_whitespace.py --base 01314210f8875610711805850d9c5d8e85708beb`,
and `python -m build` (sdist → wheel).

Both built distributions are independently installed in fresh external venvs with
binary dependencies and no source PYTHONPATH. Copied acceptance helpers/configs
run outside the checkout. `verify_gateway.installed_gateway` now commits its
synthetic Project before registration, preserving the existing CLI → authenticated
Task → observation → restart acceptance. Full installed smoke additionally checks
isolated coding/named validation, workflows, Director, ownership and fixtures.
The final rebuilt wheel is byte-identical to the accepted wheel, SHA-256
`9833156f07c9bf2f103d3731be2279415137542a21dc15b3910111ee8280d7f3`.

### CI investigation and native APFS verification

At head `1685805`, all nine main regression jobs initially passed. The macOS/
Python 3.14 APFS job passed 120 checks, then failed normal image detach with
`Resource busy`. Its same-head retry passed both APFS variants and normal detach,
but its main suite found the existing `test_timeout_kills_descendants` marker.
Neither failure was silently classified as unrelated or counted as a pass.

That test allowed its child to write after 0.5 s, independently of when supervision
returned. Controlled delay at the real process launch on the unchanged baseline
reproduced its failure and recorded the marker **before the first group stop**.
The corrected fixture requires a real child to become ready, then releases its
action only after the validator has returned. Both ordinary and delayed launch
are exercised; the production **0.1 s timeout stays unchanged**. Both checks pass,
and deliberately killing only the parent makes the corrected test fail. This is
a stronger process-lifecycle check, not a timeout increase or ignored assertion.
Production process supervision was not changed. CI's original marker alone cannot
establish whether an action happened before or after termination.

The same native APFS helper was additionally run on this Intel Mac, using an
external source archive and separate reports: case-sensitive **120 passed /
3 skipped**, **206.93 s**; case-insensitive **121 passed / 2 skipped**, **208.40 s**.
Both images detached normally, without force. The two existing platform skips and
the deliberate SQLite-URI warning remain. These are native filesystem/process
tests with disposable repositories, not GPU or Windows application acceptance.

## Remaining boundaries

This audit does not establish exhaustive correctness or guaranteed first launch.
It does not close #31. The owner's Windows/NTFS and RTX 4080/driver, actual Ollama
model tools/quality/offload/VRAM, real ComfyUI/SDXL and Windows Blender/GUI, host
sleep/power loss, fresh Codex sessions and Mac/Tailscale/private HTTPS still need
physical acceptance. Xcode/project-specific validators, signing/device behavior,
real multi-worker inference and paid endpoints were not exercised. Hosted CI is
separate from these physical checks. #73's known fixes must also be reviewed
before relying on the documented fresh-install path on main.
