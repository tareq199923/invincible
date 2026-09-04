# Work Queue — Invincible

The single ordered list of actionable work. Everything to do, in
priority order, with detail. Strategic context (phases, direction,
what's implemented) lives in [ROADMAP.md](ROADMAP.md); completed items
are logged at the bottom of this file.

Last updated: 2026-09-02.

---

## Open work — in fix order

### 1. Phase 7 wrap-up — host migration before the trial ends

**The deployment is live and on its own domain** (2026-09-02): Neon
Postgres (ap-southeast-1, least-privilege roles verified by probe) +
the app on Railway's one-month trial, serving
`invincible-ai.me` (Cloudflare CNAME → Railway, DNS-only; verified
end-to-end including a chat round-trip). Fresh-start database; full
acceptance journey smoke-tested. Details in
[ROADMAP.md](ROADMAP.md) §Phase 7. Remaining:

- **Host migration before the trial ends (~2026-10-02):** move the
  container to Azure for Students (no card needed, $100 credit; a
  reminder is set for 2026-09-25). The Neon DB is host-agnostic —
  nothing changes there. Then update the DNS record to the new host.
- The Temp-folder portable PG is now dev/test-only (it holds no live
  data; live data lives on Neon). It stays for the local test suite;
  moving it out of Temp before a disk cleanup eats it remains a nice-
  to-have, no longer urgent.

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

- **2026-09-05 — ONBOARDING COLLAPSED: 6 steps → 2 commands.** The
  new-user journey to "my AI runs commands on my PC" is now
  `pip install invincible-ai` + `invincible agent` — the agent
  self-pairs on first run (the same device flow as `invincible login`,
  now shared through `_pair_and_save`) and falls straight into the
  polling loop, printing the one remaining setup step (add
  `<server>/mcp` to the AI client) at the moment it matters. The
  browser funnel for brand-new users is unbroken end-to-end: the
  anonymous 401 from the approval page bounces to `/login?next=…`, and
  registration now honors the `next` target (previously it
  hard-redirected to `/account`, dropping the pairing context while
  the CLI kept polling); login's error re-renders preserve it too, all
  through the existing `_safe_next` guard — no new open-redirect
  surface. Deliberate limits: only a *missing* config self-pairs (a
  corrupt one is surfaced, never overwritten), and device approval
  still requires an authenticated session. Full suite 1,011 green,
  ruff clean. Docs updated: README local-agent section, MCP_PROTOCOL
  agent-routing section, SECURITY limit 16, CONFIGURATION CLI
  reference, account page Pair-a-device box.
- **2026-09-02 — DOMAIN LIVE: `invincible-ai.me` serves the app.**
  Railway custom domain added on the service (port 8000), Cloudflare
  CNAME `@` → the Railway host, DNS-only mode (Railway's Let's
  Encrypt cert answers from its Singapore edge; flipping to proxied
  later is optional and needs no Railway change). Verified end-to-end:
  `/health`, dual-realm 401/302 behavior on `/dashboard`, and a real
  chat round-trip through the domain. Along the way it surfaced that
  `invinseble-ai.me` — the spelling these docs carried for weeks —
  was never registered; a typo from the start. The real domain is
  `invincible-ai.me` (both docs corrected).
- **2026-09-02 — PHASE 7 DEPLOYED: the live server is off the dev PC.**
  Host: Railway trial (`invincible-gateway-production.up.railway.app`,
  `railway.json` = start command + `/health` healthcheck; `PORT=8000`
  so the Dockerfile CMD's fixed port routes). Database: Neon
  (ap-southeast-1, pooled DSN; `invincible_migrate` schema owner +
  `invincible_app` CRUD-only role created from `01-roles.sh` adapted
  for Neon — memberships needed `WITH SET OPTION` before ownership
  could transfer; DDL-denial and wrong-password rejection verified by
  direct probe). Fresh-start decision: the dev dataset (~2 accounts of
  rehearsal traffic) was not worth migrating; first registration
  bootstrapped the operator. All four acceptance criteria (a–d) pass;
  journey smoke-tested live incl. a real chat round-trip. Two en-route
  fixes: `?sslmode=require` is not a valid asyncpg URL param (removed;
  Neon enforces TLS regardless), and Windows CRLF + quoted `.env`
  values corrupted staged env vars (re-parsed with python-dotenv).
  Remaining wrap-up tracked as the open item above (Azure migration
  before the trial ends).
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
