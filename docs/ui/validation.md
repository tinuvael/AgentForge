# Issue #67 validation

## Automated checks

Local host: native macOS/APFS, CPython 3.13.2. Development tools installed with
`python -m pip install -e '.[dev]'` in the existing venv.

- Full regression run: **1,614 passed, 28 Windows-only skips**
  in 703.62 seconds. One existing SQLAlchemy SQLite-query warning.
- Final focused UI/Director/Council/coding run: **44 passed**.
- Final new dashboard regression file: **11 passed**, including HTMX redirects,
  unchanged Origin/CSRF/stale revision rejection, exact approval fields/expiry,
  escaping, conversation restoration and confirmed workspace cleanup.
- Ruff lint, Ruff format check and `git diff --check`: passed.
- Source distribution and wheel build: passed. Clean installed-wheel verification
  passed with the exact tested dependencies (see the installation note below).
  The two final HTMX regression cases are included in the focused 11-test run;
  hosted CI runs the complete final test collection.
- The nine-job Actions matrix remains unchanged: Ubuntu 24.04, Windows 2025,
  macOS 15 × Python 3.12, 3.13, 3.14. Every job retains full pytest, lint,
  formatting, build and clean installed-wheel checks; macOS retains APFS acceptance.

The first unconstrained clean-wheel install selected cryptography 50.0.2 as a
source distribution on this Intel Mac and failed because OpenSSL development
files are unavailable. Local wheel verification is reproduced with binary wheels
and the exact tested environment, without changing repository dependency ranges:

```sh
python -m pip freeze --exclude-editable > reports/dashboard-dependencies.txt
PIP_CONSTRAINT="$PWD/reports/dashboard-dependencies.txt" PIP_ONLY_BINARY=:all: \
  python scripts/ci_verify_wheel.py
```

The fresh venv, migration roundtrips, packaged imports/assets, `pip check`, MCP,
Gateway, Companion and Director smoke checks still run outside the checkout.
Dependency/environment reports remain ignored, not committed as runtime policy.

## Actual browser review

Reviewed screenshots and DOM/accessibility state in the Codex in-app browser.
All records came from the offline fixture server documented in [README](README.md).

| Width | Overview | Tasks | Workers | Projects | Councils | Director active / approval |
| --- | --- | --- | --- | --- | --- | --- |
| 1440 | Fits | Contained table | Fits | Fits | Fits (empty) | Three-area workspace / exact arguments |
| 1024 | Fits | Contained table | Contained table | Contained table | Contained table | Evidence follows messages |
| 768 | Fits | Contained table | Contained table | Contained table | Contained table | Collapsible conversation navigation |
| 390 | Fits | Contained table | Contained table | Contained table | Contained table | Stacked workspace, six-route navigation |

For these 28 route/viewport combinations, document scroll width equalled viewport
width; each had one main element and all six real navigation links. Full UUIDs,
models and dates were retained. See the committed desktop/mobile screenshots,
plus [tablet](screenshots/after-director-tablet-768.jpg),
[laptop](screenshots/after-tasks-laptop-1024.jpg) and
[Task detail](screenshots/after-task-detail-1440.jpg).

Browser interaction checks:

- Skip link focused `main`; native conversation disclosure operated with Space;
  focused summary displayed a solid outline.
- HTMX filtering retained the selected state, produced the expected single
  result and retained exactly one `task-table` element.
- Created a conversation, selected a Worker, submitted a request containing
  `<script>` text and observed completion. The text remained text; there were
  zero script nodes in the conversation messages. The saved transcript remained available on reload.
- Rejected an exact-argument approval; it became cancelled, with no privileged
  submission. Stopped Director and verified its independent Task remained running.
- Separately confirmed cancellation of a queued Task. It became cancelled with
  one `task-detail` fragment and no remaining cancellation form.
- Inspected active/error/empty states, approval argument wrapping, focus, mobile
  navigation, long IDs, unknown telemetry, table containment and diagnostic disclosure.
- Measured token palette contrasts against raised surfaces: primary text 14.53:1,
  secondary text 7.97:1, links 10.71:1, semantic status text at least 8.63:1;
  primary button text 10.01:1. Control borders 4.43:1. Reduced-motion CSS removes
  transitions; no animation library or continuous decorative motion is present.

Real browser review exposed the inherited native-form POST problem under
`Referrer-Policy: no-referrer`: native navigation sent `Origin: null` and the
existing strict guard correctly rejected it. Director and workspace cleanup
forms now use HTMX with a full GET `HX-Redirect`, following the existing Companion
pattern. Browser creation/send/rejection/stop now succeed. `Origin: null`, foreign
Origin, invalid CSRF, stale revisions and replay checks remain rejected by tests.
No security header, cookie, Host/Origin rule, nonce or core approval rule changed.

## Limits and remaining acceptance

This is viewport and scripted-Provider evidence, not live hardware or a full
assistive-technology audit. No new browser runtime/dependency was added to CI.
Council mixed outcomes, coding details and security races retain existing ASGI
and platform regression coverage; the review server's Council history is empty.
Worker resources/quotas absent from current projections remain explicitly unknown.
No inference, egress policy, Gateway authentication, SQLite owner or Provider
contract was changed. Physical Windows browser, authenticated MacBook SSH tunnel,
iPad/small-screen use, real Ollama sessions, long outputs and reconnect/approval
acceptance remain in **Issue #31**. Hosted checks do not close that gate.
