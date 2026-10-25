# Dashboard redesign review (#67)

The actual dashboard uses FastAPI, Jinja2, vendored HTMX and local CSS/JavaScript.
The [design system](../../design-system/agentforge/MASTER.md) records visual and
interaction rules. The [dashboard guide](../dashboard.md) defines exposure and
telemetry semantics; [Local Director](../local-director.md) defines orchestration.

## Audit and resulting screens

| Surface | Before | Implemented |
| --- | --- | --- |
| Shared navigation | Horizontal links without active state | Responsive persistent sidebar, six real routes, active parent detail route, skip link |
| Overview | Eight equal count cards and dense ungrouped data | Service/inventory strip, linked lifecycle counts, failure triage, recent Tasks, covered terminal telemetry |
| Local Director | Single long message/activity/approval column | Conversation navigation, message workspace, context/evidence rail, exact argument approvals with UTC expiry, separate stop/cancel, recovery links |
| Tasks | Unstyled filters and dense raw timestamps | Grouped filters, reset, compact dates with full UTC `datetime`/title, full IDs, lifecycle badges, keyboard-scrollable table |
| Task detail | Unstructured metadata | Shared hierarchy, scoped overflow, readable facts/results/diffs; all telemetry, validation and cleanup controls retained |
| Workers | Huge always-open probe tables | Identity/trust/model table, last health with time, expandable full probe evidence and declared capabilities, explicit unknown resource limits |
| Projects | Raw registration table | Shared hierarchy, wrapped roots/IDs, cached-state and host authorization explanation; no policy editor |
| Councils | Undifferentiated outcomes | Shared history, participant state counts, visible partial failures, independent Task links/results |

## Actual browser evidence

These are screenshots of the real application routes rendered in the Codex
in-app browser, using disposable SQLite records and in-memory scripted MCP and
inference. They are not concept images or evidence of real Worker availability.
No external credentials or inference services were used. Before images were
captured at the base commit before template edits. Random UUIDs/private temporary
paths and observation/approval timestamps vary between fixture runs; lifecycle
mix, names, requests and fixture dates are controlled.

| View | Before | After |
| --- | --- | --- |
| Overview, 1440px | [Baseline](screenshots/before-overview-1440.jpg) | [Redesign](screenshots/after-overview-1440.jpg) |
| Director active, 1440px | [Baseline](screenshots/before-director-1440.jpg) | [Redesign](screenshots/after-director-active-1440.jpg) |
| Director approval, 1440px | — | [Exact arguments and expiry](screenshots/after-director-approval-1440.jpg) |
| Tasks, 1440px | — | [History and filters](screenshots/after-tasks-1440.jpg) |
| Workers, 1440px | — | [Configuration and diagnostics](screenshots/after-workers-1440.jpg) |
| Overview, 390px | [Baseline](screenshots/before-mobile-390.jpg) | [Redesign](screenshots/after-overview-mobile-390.jpg) |
| Director, 390px | — | [Responsive conversation](screenshots/after-director-mobile-390.jpg) |

## Reproduce visual review

Install the dev extra in a Python 3.12+ venv, then run:

```sh
python scripts/preview_dashboard.py
```

Open `http://127.0.0.1:8877`. The console prints active and approval conversation
URLs. This development-only server creates its own temporary database, registered
sample roots and journal, uses a fixed **fixture browser identity**, and binds
only to loopback. Never reuse its identity middleware or data in a deployment.
Stop it with Ctrl+C; temporary state is removed. Expiring approvals remain subject
to the production five-minute limit; restart the fixture to get a fresh request.
Production routes, CSRF, Host/Origin checks, templates and browser scripts are used.
The in-memory MCP principal is a fixture, not proof of network authentication.

Review Overview, Director home/active/approval/completed, Tasks and Task details,
Workers with diagnostics expanded, Projects, and empty Councils. Repeat at
1440×1000, 1024×900, 768×1024, 390×844 and 375×812. Tab from the skip link,
open native disclosures with Space/Enter, operate filters, scroll data regions,
and check long IDs. Use a new conversation to test composition and restoration.
After capturing an approval, reject it and confirm no Task was submitted. Stop
the fixture Director and confirm the existing Task stays running. Inspect Task
cancellation with its separate confirmation. Re-run the fixture after mutations.

## Validation record

See [validation](validation.md) for recorded checks and known limits. Automated
coverage uses migrated SQLite and scripted Providers, including escaping,
fragments, exact approval fields, stale forms/CSRF, ownership, stop/cancel,
conversation restoration and full real HTTP integration. Existing tests remain.
No CI or simulated viewport establishes physical device acceptance. Windows
browser, authenticated MacBook SSH forwarding, physical iPad/small screens, real
Ollama activity, lengthy outputs and reconnect/approval checks remain in #31.
