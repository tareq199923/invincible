# Work Queue — Invincible

The single ordered list of actionable work. Everything to do, in
priority order, with detail. Strategic context (phases, direction,
what's implemented) lives in [ROADMAP.md](ROADMAP.md); completed items
are logged at the bottom of this file.

Last updated: 2026-09-01.

---

## Open work — in fix order

### 1. R3 — generated `GATEWAY_API_KEY` is unexplained (~15 min)

**Finding (rehearsal):** setup generates the gateway key silently.
Nothing tells a first-time user that the `/v1/*` chat endpoints now
*require* this as a Bearer token (fail-open only applies when unset) —
a classic "why did my old curl stop working?" trap.

**Fix plan:**
- One explanatory line after generation, alongside the existing
  credential-key message:
  *"Generated GATEWAY_API_KEY — chat clients must send it as a Bearer
  token. It is in your .env."*
- Test: assert the line appears only when the key is newly generated.

### 2. R6 — no automated fresh-install test (~45 min)

**Finding (rehearsal):** the fresh-install journey (setup → upgrade →
start → register → operator bootstrap) was proven only by a manual
one-time walkthrough. `tests/conftest.py` uses a warm pre-created
database, so the real path is never exercised in CI.

**Fix plan:**
- New `tests/test_fresh_install.py`: create a scratch database (the
  CLI-tier scratch-DB fixtures already exist in conftest), run
  `db upgrade` to head, boot the app against it, register the first
  account, assert `role == "operator"`, hit `/health`.
- Do this AFTER R2/R3 — the test should pin the final shape of setup.
- This converts the entire dress rehearsal into permanent regression
  armor.

### 3. Phase 7 — deployment: move the live server off the dev PC

**Why now:** `invinseble-ai.me` currently runs from the developer's PC
through a Cloudflare tunnel, with the database in a Temp-folder
portable PG. That breaks two of Phase 7's own acceptance rules ("never
a temp-directory or otherwise ephemeral cluster"; always-on) and means
the public site dies whenever the PC sleeps/reboots. Users' chats and
memories cannot live on a machine that turns off nightly.

**The move (mostly ops, one weekend — see ROADMAP.md §Phase 7 for the
full acceptance criteria: managed Postgres with backups, non-superuser
app role, scram auth, fresh secrets):**
- Provision Neon PostgreSQL (or equivalent managed Postgres); keep the
  backup story.
- Provision an always-on host (Railway per the roadmap; any small VPS
  works) and run `inv setup --db-url <DSN>` → `inv db upgrade` →
  `inv start` there. R2 (shipped) now catches a typo'd DSN at setup
  time.
- Point `invinseble-ai.me` at the host. Decision to make: keep the
  Cloudflare tunnel from the host, or use a plain public origin —
  a tunnel adds a moving part for no benefit on a host with a public
  origin.
- Migrate any data worth keeping off the Temp-folder dev PG, then
  retire it (also resolves the Housekeeping item below).
- Do NOT rotate `INVINCIBLE_CREDENTIAL_KEY` in the move: it would
  orphan every stored BYOK credential.

**Out of scope here:** Phase 6 (CLI client mode / zero-database user
setup) — separate code project, planned after.

### 4. Phase 9 PR-D — dashboard Providers page

The BYOK API is complete (connect/list/test/remove with SSRF guard +
audit, per-user router candidate pool); only the UI is missing.
This is also what makes the promise true that setup now makes —
"configure providers later in the dashboard." Details in
[ROADMAP.md](ROADMAP.md) §Phase 9.

### 5. R4 — `dev-db` hardcodes port 5433 (~20 min)

**Finding (rehearsal):** two Invincible installs on one machine
collide on the port (and with the documented dev convention).

**Fix plan:** probe-and-increment — if 5433 is busy, try 5434+; write
whichever port won into the generated DSN. Low priority: bites
developers only.

### 6. R5 — zero provider keys still served chat (investigate, then decide)

**Finding (rehearsal):** a fresh install with NO provider keys
answered `/v1/chat/completions` with HTTP 200 via `agentrouter-glm`
(no key configured, no startup warning fired).

**Questions to answer:**
- Does agentrouter.org serve keyless requests by design (free tier)?
- Or did a key leak from the ambient environment during the rehearsal?

**Then:** document as intended behavior, or add a startup warning /
require the key. Do not change behavior before understanding it.

---

## Decisions pending

### Distribution (#2) — the strategic frontier

"Anyone can use Invincible" currently requires Python + clone + venv +
a Postgres. With R1 fixed, the journey is now
`install → setup --db-url <DSN> → start` — one command, one argument.

Open decisions (multi-day project, fold into Phase 6/7 planning):
- **Publishing:** PyPI (`pip install invincible`) vs one-file `.exe`
  vs both.
- **The database:** remote-first is now the setup story (Neon etc.),
  matching the roadmap's hosted direction. Remaining question: is a
  bundled/local option still worth offering for offline users?

### Housekeeping (10 min, whenever)

- Move the dev database out of the Temp folder before a disk cleanup
  eats it (portable PG on 5433, manual start, dies on reboot) —
  superseded by the Phase 7 deployment item above, which retires this
  PG entirely.
- Revoke stale Claude/Grok OAuth connectors (`invincible oauth list`).

---

## Completed log (newest first)

- **2026-09-01 — R2 FIXED: `setup --db-url` now verifies connectivity.**
  After normalization, one real `SELECT 1` connection (5s timeout, one
  retry with a 1s pause — covers serverless cold starts) runs before
  anything is written. Failure aborts naming host:port, keeps the
  password out of the message, and writes nothing. Escape hatch:
  `--skip-db-check` for offline pre-provisioning. Only the `--db-url`
  path is probed — a no-flag run leaves the DSN untouched and gains no
  network round-trip. Live smoke-tested both ways (5433 accept, dead
  port reject, .env untouched). 912 tests green, ruff clean.
- **2026-09-01 — R1 FIXED: setup is non-interactive** (`328ae8b`).
  Zero prompts; secrets auto-generated; provider-key prompts removed
  (dashboard/env is the path now); DB URL via `--db-url` (validated,
  `postgresql://` auto-upgraded to asyncpg, remote-first). First run
  without a URL fails cleanly with guidance. Scriptable on Windows
  (the getpass hang is gone). 908 tests green.
- **2026-09-01 — Dress rehearsal complete** (`546110c`). Full
  fresh-install journey walked on an isolated stack (scratch PG 5434,
  server 8901): setup → upgrade (0008) → start → operator bootstrap →
  OAuth PKCE consent → MCP tools incl. staged-write approval → chat
  200. Produced findings R1–R6 above; live 5433 data verified
  untouched afterward.
- **2026-08-31 — Browser-entry UX fixed (polish 3a + 3b)** (`a4cb606`).
  Anonymous browser GETs hitting a 401 redirect to
  `/login?next=<path>` (browsers via 302, HTMX via `HX-Redirect`,
  API clients keep JSON; POSTs never redirect). `invincible start`
  opens `/dashboard`, which lands anonymous users on `/login`.
  912 tests green.
- **2026-08-31 — Claims verified, tracking docs born** (`988fd57`).
  The four work items (rehearsal, distribution, polish, housekeeping)
  verified against the code before any work started.
