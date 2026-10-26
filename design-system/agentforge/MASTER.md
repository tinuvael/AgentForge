# AgentForge dashboard design system

The operator needs to see what is executing, on whose Worker, with what evidence,
and what needs a human decision. This is the shared visual contract for the
FastAPI / Jinja2 / HTMX dashboard. Runtime behavior remains in the existing core.

## Direction and references

Reviewed the installed `ui-ux-pro-max` workflow and `design-system` token guidance.
The verified search, `infrastructure monitoring dashboard --design-system
--density 8 --variance 2`, recommended an operations pattern and minimal Swiss
hierarchy. Adapted that guidance to the existing product: graphite/navy surfaces,
cyan actions, system fonts, compact native controls. No landing-page conversion
patterns, remote font imports, animation libraries or SPA framework are used.
There are no page overrides; all screens follow this file.

## Shared foundations

Tokens live in `src/agentforge/web/static/dashboard.css` in three layers:

| Layer | Examples | Purpose |
| --- | --- | --- |
| Primitive | `--graphite-950`, `--cyan-400`, `--space-2` | Palette and 4/8px rhythm |
| Semantic | `--bg`, `--panel`, `--raised`, `--muted`, `--danger` | Meaning and surface hierarchy |
| Component | `--button-bg`, `--button-fg`, `--sidebar-width` | Reusable control/layout rules |

Background #0c1017; panels #111720; raised surfaces #151d28. Text #e8eef6;
secondary text #a5b3c6; links #8cd9ed; primary action #66c9e3 with dark text.
Positive #8bdbb4, caution #f3cc87, failure #f4a3ad. Text labels always accompany
status color. Dividers are subtle; form control borders and focus rings are stronger.

System sans for prose; system monospace for identifiers/code. Page titles 30px,
section titles 17px, body 14px, conversation 13px, operational metadata 12–13px.
Small decorative labels remain separate. Use full, copyable identifiers with wrapping. Six-pixel panel/control radius; no glass,
large gradients, decorative metrics or entrance animations. Hover transitions
150ms, removed for reduced motion. Native select/checkbox/details controls.

## Patterns and responsive hierarchy

- One sidebar with the six actual destinations; Local Director is second.
  Current routes and their details share `aria-current=page`.
- A page heading gives purpose; panels group actual information, not decoration.
  One dominant action where a real action exists. Never invent controls.
- Overview separates service availability, configuration counts, lifecycle counts,
  recent submissions and terminal telemetry. A failed-history link supports triage.
- Director separates conversation navigation, readable messages and an evidence
  rail. Exact argument approvals interrupt the conversation at the decision point.
  Request pins are labelled separately from the immutable Project/Worker/Agent
  recorded on each delegated Task; incomplete evidence
  stays labelled. Large messages use native disclosures with the full escaped text.
- History tables keep complete engineering fields in labelled, keyboard-focusable
  overflow regions. Worker probe detail uses expandable rows; declarations never
  become measurements. Resource/quota data absent from this view stays unknown.
- At 1279px the Director evidence rail follows the conversation. At 900px its
  conversation navigation moves above it and initially collapses. At 640px the
  primary sidebar becomes a six-link, two-row navigation, selections stack, forms
  use 16px text and 44px controls, and facts become single-column lists.

## Interaction and accessibility

Skip-to-main; semantic headings, tables and scoped headers; descriptive control
labels; visible 2px cyan keyboard focus with 4px offset; status text as well as
color; locally drawn decorative SVGs hidden from assistive technology. No forced
scroll-to-bottom. Table, long-message, activity and preformatted output scroll containers retain
keyboard access. Refresh also defers while these reading regions are focused. No zoom restrictions.

HTMX owns snapshots, never inference. Task refresh defers during focused controls
or checked confirmations. Director polling defers during focused controls. Open
disclosures and output scroll positions survive refresh. Existing input forms
stay outside fragment refreshes. HTMX mutation buttons disable during requests;
Director and workspace cleanup POSTs use HTMX followed by a same-origin full GET
redirect, keeping the real Origin under the existing no-referrer policy. Native
POST fallbacks preserve submitter values; no Origin exception is introduced.
Errors expose fixed safe codes with recovery links; no error-body HTML rendering.

## Trust and observable facts

No online claims from Worker configuration. `—` means unknown, never zero. Last
health observations include their timestamp. Throughput/token coverage keeps its
existing semantics. Source tools observed is not independent source verification.
Stop Director and cancel Task are distinct actions. Exact nonce, revision, expiry,
CSRF, operation ID and ownership checks remain on the server; CSS/JS confer no
approval. Requests and model output remain escaped plain text. No new routes,
provider probing on GET, storage owner, external assets or security policy changes.
