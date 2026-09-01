# Work Queue — Invincible

The single ordered list of actionable work. Everything to do, in
priority order, with detail. Strategic context (phases, direction,
what's implemented) lives in [ROADMAP.md](ROADMAP.md); completed items
are logged at the bottom of this file.

Last updated: 2026-09-01.

---

## Open work — in fix order

### 1. Phase 7 — deployment: move the live server off the dev PC

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

- **2026-09-01 — PR-D verified and closed: the dashboard Providers page
  already shipped.** Commit `27bebf4` (Aug 30, "Phase 9 (4/4)") had
  landed the full UI — catalog connect cards with connected-state flip,
  custom provider form, HTMX test/remove with row delete, nav entry —
  but the WORKQUEUE/ROADMAP still listed it as the remaining work.
  Re-verified against the live 5433 dev PG: all 12
  `tests/test_dashboard_providers.py` tests pass, the four other BYOK
  suites pass (53 total), full suite 922 green, ruff clean. Phase 9 is
  **Complete** in ROADMAP.md. No code changed — docs only.
- **2026-09-01 — R5 FIXED: no provider key was ever keyless — it was a
  .env leak.** The rehearsal's "fresh install" answered chat because
  `main.py`'s module-level `load_dotenv()` resolved the `.env` by
  walking up from *the module's own directory* (python-dotenv's frame
  walk), silently loading the developer's repo-root `.env` — provider
  keys and all — into any process importing `invincible.main`, on any
  source/editable install, regardless of launch directory. agentrouter
  was simply serving a valid key the whole time. Fix:
  `load_dotenv(find_dotenv(usecwd=True), override=False)` — direct
  uvicorn launches from the project folder behave identically; launches
  elsewhere no longer smuggle an unnamed `.env` in. Regression test in
  `tests/test_env_isolation.py` runs the leak case as a real subprocess
  script (the frame-walk only triggers when `__main__` has a `__file__`;
  `python -c` never reproduces it) — verified it fails on the pre-fix
  code. 922 tests green, ruff clean.
- **2026-09-01 — R4 FIXED: `dev-db` no longer hardcodes port 5433.**
  Before starting a NEW Postgres (Docker), provisioning probe-and-
  increments to the first free port (raw TCP check — anything listening
  counts as busy, up to 10 ports) and the winner flows into the printed/
  written DSN plus a "port X is busy - using Y instead" note. Probing
  an existing server is unchanged (auth-based, no new round-trip).
  docker-compose.yml ports mapping parameterized via
  `INVINCIBLE_DB_PORT` (passed through the compose process env —
  compose has no `-e KEY=VAL` flag). 920 tests green, ruff clean.
- **2026-09-01 — R6 FIXED: automated fresh-install test.** New
  `tests/test_fresh_install.py` pins the whole dress-rehearsal journey
  against a throwaway scratch database: real `setup` (connectivity
  probe + R3 announcement asserted), real `db upgrade` to head, the
  real FastAPI lifespan (not the hand-wired conftest client), `/health`,
  and first-registration bootstrap — first account becomes `operator`,
  second stays `user`. Auto-skips without a local Postgres, same as the
  other live-tier tests. 915 tests green, ruff clean.
- **2026-09-01 — R3 FIXED: setup announces a generated
  `GATEWAY_API_KEY`.** Fresh runs (and `--force` rotations) now print
  "Generated GATEWAY_API_KEY - chat clients must send it as a Bearer
  token on /v1/*. It is in <env path>." right where the credential-key
  message sits. Silent when the key already exists and `--force` is
  absent; the secret value never reaches the terminal. 914 tests green,
  ruff clean.
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
