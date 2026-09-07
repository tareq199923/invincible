# UI / UX Improvements — Invincible Console

Last audited: 2026-09-06, against the dark-terminal design system shipped in
`b228982..f0b7603` (sidebar shell, design tokens in `base.html`, provider
marks, usage chart, setup steps).

**Status 2026-09-08:** Tier 0 (T0-1..T0-3) and Tier 1 (T1-1..T1-7) shipped
— same commit as this note, suite 1071 green. Notes: T0-1 was real but
different from the suspicion — the template's regex was literally `\\n`
(a no-op), so the copy included both lines *plus* the button label glued
to line two; fixed with `data-copy` attributes as prescribed. T0-3: the
Test button now swaps its row in place (`_provider_row.html` partial);
the rare blocked-URL path still full-redirects to the `test_error`
banner on purpose (the explanatory copy matters there). Tier 2/3 remain
open.

This is a living backlog. Pick items top-down within a tier; each item lists
impact, effort (S < 1h, M ~half-day, L multi-day), and acceptance criteria.

---

## Ground rules (do not break)

These are contracts the UI currently relies on — check them before and after
any change:

- **Page-text test assertions.** Tests assert literal strings rendered by
  templates (e.g. the session-detail failover "from → to" line, empty-state
  copy). Renaming visible text means updating the matching tests in the same
  commit. Grep `tests/` for the string before changing it.
- **Design tokens live in `base.html` `:root`.** No CDNs, no webfonts — the
  monospace stack *is* the identity. New colors come from
  `--accent / --ok / --warn / --err` (and their `-dim` variants), not new
  hex values. The usage chart and provider marks share `_SOURCE_PALETTE`.
- **htmx is the only interactivity layer** (+ tiny inline scripts). No build
  step, no JS framework.
- **One replica max in production** while the agent registry is in-memory —
  anything needing server-side session affinity (websockets, server-sent
  events per user) is blocked on that.
- **Local dev:** pytest truncates `invincible_test` (wipes dev users);
  start the Temp-folder PG via `pg_ctl` before running anything.

---

## Audit coverage

| Page | Template | Audited |
|---|---|---|
| Shell / nav | `base.html` | ✅ |
| Dashboard | `dashboard.html` | ✅ |
| Usage | `usage.html` | ✅ |
| Sessions / detail | `sessions.html`, `session_detail.html` | ✅ |
| Providers | `providers.html` | ✅ |
| Setup | `setup.html` | ✅ |
| Settings | `settings.html` | ✅ |
| Landing | `landing.html` | ❌ (not yet reviewed) |
| Account, MCP, Memory, Memory graph, Tasks, auth pages | `account.html`, `mcp.html`, `memory.html`, `memory_graph.html`, `tasks.html`, `login.html`, `register.html` | ❌ (not yet reviewed) |

---

## Tier 0 — Suspected bugs / correctness

### T0-1. Setup copy buttons may drop the first config line
`setup.html` copy buttons use
`innerText.replace(/^.*\n/, '')` — a regex that strips everything up to the
first newline. The config block's first line of text *is*
`export ANTHROPIC_BASE_URL=…` (the button node comes later in the same text
flow), so the copy may only contain the second line. **Verify with the app
running** (see `/verify`); if broken, replace with an explicit
`data-copy` attribute on the `.config-block` and
`navigator.clipboard.writeText(btn.dataset.copy)` — no innerText parsing at
all. Same pattern applies to the one-time key copy on `account.html`.
- Impact: H · Effort: S
- Accept: clicking Copy puts the *full* two-line config on the clipboard,
  verified by pasting; no regex-on-innerText remains in templates.

### T0-2. Success messages render as warnings
`providers.html` renders "Provider connected." and "Connection test passed."
with `.banner` — the **warn** style (amber, left bar). Users read amber as
"something went wrong". Add a `.banner-ok` variant (`--ok` / `--ok-dim`,
matching `.setup-complete`) and use it for success flashes; keep `.banner`
for warnings only.
- Impact: M · Effort: S
- Accept: connect/test success shows green banner; failure stays amber;
  no test asserts the old styling.

### T0-3. Provider "Test" button gives no in-place feedback
`hx-post="/providers/mine/{{id}}/test"` has no `hx-target`, no
`htmx-indicator`, no disabled state. During the round-trip the button looks
dead, and the result lands wherever htmx's default swap goes. Give it
`hx-target="closest tr"` + `hx-swap="outerHTML"` returning a re-rendered row
(with status badge updated), and wrap label in an `.htmx-indicator` spinner.
- Impact: M · Effort: M
- Accept: clicking Test shows a spinner, row status updates in place, no
  full-page reload.

---

## Tier 1 — Quick wins (polish what exists)

### T1-1. Tables need real `<thead>` + horizontal scroll on mobile
`sessions.html`, `dashboard.html`, and others use bare `<tr><th>…` in the
body — no `<thead>`/`<tbody>`, so screen readers announce them wrong, and
`th` styles are only correct by accident. Also on phones, wide tables
(providers has 7 columns) overflow the viewport with no way to scroll.
Wrap tables in a `.table-scroll` div (`overflow-x: auto`) and use proper
thead semantics globally.
- Impact: M · Effort: S
- Accept: all tables use thead; providers page usable at 375px width.

### T1-2. Visible focus states
`a`, `.nav-link`, buttons, and rows have hover styles but no `:focus-visible`
styles — keyboard users are invisible on the dark theme. Add a global
`a:focus-visible, button:focus-visible, input:focus-visible …` outline using
`--accent` (e.g. `outline: 2px solid var(--accent); outline-offset: 2px;`).
- Impact: H (a11y) · Effort: S
- Accept: tab through login → dashboard → providers; every interactive
  element shows a clear ring.

### T1-3. `prefers-reduced-motion` support
The blink cursor, page `rise` animation, and staggered entrances all ignore
the media query. One block in `base.html`:
`@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }`
- Impact: M (a11y) · Effort: S
- Accept: with the OS flag set, no entrance/blink animation runs.

### T1-4. Timeago needs an absolute fallback
`{{ s.updated_at | timeago }}` shows "3d ago" with no machine-readable
fallback — you can't see the actual timestamp without dev tools. Add
`title="{{ s.updated_at }}"` (or `datetime=` in a `<time>` element) wherever
timeago is used.
- Impact: M · Effort: S
- Accept: hovering any relative time shows the full timestamp.

### T1-5. Empty states should end in a next action
Empty rows ("No sessions yet.") tell users where they are, not where to go.
Give each empty state a single link — e.g. sessions → `/dashboard/setup`,
providers → "Connect your first provider" anchor to the connect cards.
The dashboard banner already does this well; copy that pattern.
- Impact: M · Effort: S
- Accept: every `td.empty` / `.empty` block on audited pages includes one
  CTA link; updated copy passes greps in `tests/`.

### T1-6. Usage chart: label the y-axis and keep tooltips
Gridlines at 25/50/75% have no numbers, so "peak 1.2k/day" is the only
calibration. Render 3–4 faint tick values on the left inside the SVG.
Native `<title>` tooltips are fine to keep; also add `cursor: pointer`
feedback is *not* wanted unless clicking a day does something (see T2-3).
- Impact: M · Effort: S
- Accept: chart shows at least min/mid/max token values; hover on a bar
  still shows day + tokens + attempts.

### T1-7. `project_id` column shows raw IDs
Sessions tables show the bare project id (likely a UUID-ish value). If a
display name exists on the model, show it with the id as `title=`; if not,
at least `code`-style it so it reads as an identifier, not garbage text.
- Impact: M · Effort: S–M (depends on model)
- Accept: sessions/dashboard show human-meaningful project labels or
  consistently styled IDs.

---

## Tier 2 — Structural improvements

### T2-1. Sessions list: pagination + filter
`sessions.html` renders every session in one table. At a few hundred rows
this page becomes the heaviest in the app. Add `?page=` with prev/next
(using the existing `.segmented` control style from usage), plus a
project filter and a client-session-id search box (server-rendered, GET
form). No JS needed.
- Impact: H · Effort: M
- Accept: page renders ≤ 50 rows + pager; filter/search are link-shareable
  URLs; empty-search shows the standard empty state.

### T2-2. Flash message system
Success/warn/error feedback is currently per-page ad-hoc (query params +
`{% if %}` banners, warn-styled for everything). Standardize one partial +
CSS trio (`.banner-ok/.banner/.banner-err`) rendered from a single
`flash` context key, with `role="status"` / `role="alert"` so assistive
tech announces them. Providers, account, settings all migrate to it.
- Impact: M · Effort: M
- Accept: all post-redirect flows show correctly-styled, announced banners;
  T0-2 is the first consumer.

### T2-3. Usage chart day drill-down
Clicking a day bar links to a sessions/activity view filtered to that day
(if the backend can filter runs by date; otherwise skip). Make bars
`<a>`-wrapped rects or add `tabindex` + keydown. If skipped, remove the
misleading `:hover` fill change so bars don't look clickable.
- Impact: M · Effort: M–L
- Accept: hover affordance matches actual behavior.

### T2-4. Session detail: collapsible activity + payload truncation
`session_detail.html` renders full task payloads
(`code.payload` with `tojson`) and every activity row — long sessions make
this page enormous. Use `<details>` for payloads (summary shows first ~80
chars), cap activity at N rows with a "show all" toggle, both pure-HTML.
- Impact: M · Effort: M
- Accept: a 200-event session detail renders without scroll-aghast;
  failover chain line text unchanged (test-asserted).

### T2-5. Provider connect forms: staged UX + validation
The catalog cards each embed a 4-field form — visually heavy, and one
bad base URL fails after submit with a generic banner. Improvements, in
order of value:
1. Client-side `required` + `type="url"` on base_url (already partly there).
2. Collapse the form behind a "Connect" per-card button (`<details>` or a
   tiny htmx swap), so the page is scannable first.
3. Inline error per field on failure (server re-renders form with errors).
- Impact: M · Effort: M
- Accept: connect flow usable without scrolling the whole catalog; errors
  name the offending field.

### T2-6. Command palette / global search (P2, post-1-replica is fine)
`/` or Ctrl-K opens a search over sessions, providers, memories, and nav
destinations. Can be done as a small htmx dialog + server endpoint; no
framework. Genuinely fits the operator-terminal identity.
- Impact: H (power users) · Effort: L
- Accept: keyboard-only navigation to any page and any session by id
  fragment.

---

## Tier 3 — Bigger bets (needs design thought first)

- **T3-1 Live updates.** Dashboard cards + sessions list refresh on new
  activity. Blocked-ish by the single-replica constraint for anything
  stateful; htmx polling (`hx-trigger="every 30s"`) is the replica-safe
  version. Impact H, Effort M.
- **T3-2 Landing page pass.** `landing.html` (345 lines) hasn't been
  audited; next step is a read + section-by-section notes here. Likely
  candidates: a live terminal-style demo block reusing the
  `.config-block` motif, and a clearer "your key, your billing" trust
  section.
- **T3-3 Light theme?** Current design is deliberately dark-only
  (`color-scheme: dark`). If ever requested, tokens make it feasible —
  but treat it as a product decision, not a UI bug.
- **T3-4 Onboarding replay.** The setup page is strong; consider embedding
  the current step state in the sidebar (e.g. a small "1 of 2" progress
  marker next to "get started") so users don't lose the thread.

---

## Definition of done for any UI change

1. `pytest` green — including page-text assertions (update them with the
   copy, never weaken them).
2. Manual pass at 1440px and 375px widths, and a keyboard-only tab-through
   of the changed page.
3. New colors/spacing come from `:root` tokens in `base.html`; no new hex
   literals outside the shared palette.
4. If it touches flash/success copy: uses the banner variant that matches
   its meaning (T0-2 pattern).
