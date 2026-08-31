# Current Goal — Invincible

Working document (2026-08-31): the verified state of the four open work
items, cross-referenced against [ROADMAP.md](ROADMAP.md). Every claim
below was checked against the code, not assumed.

---

## 1. The dress rehearsal 🎭 — **DONE (2026-09-01)**

**Claim:** The fresh-install journey is proven by tests, but tests run
against a warm database — so the real new-user path
(setup → start → register → connect a client → approve → tools run)
has never been walked end-to-end.

**Verified: TRUE.** `tests/conftest.py:140-143` does
`create_all_from_metadata` up front and TRUNCATEs between tests. The
actual user path — `invincible setup` wizard → explicit
`invincible db upgrade` (Alembic) → first-boot lifespan seeding — is
never exercised as a user experiences it.

### What was walked (2026-09-01, fully isolated stack)

Scratch PG 17.10 on port 5434 (initdb'd fresh; the live 5433 instance
and its data untouched, verified after), scratch directory, server on
port 8901 with `--no-tunnel`:

1. `invincible setup` (in-process, real wizard code) — ✅ wrote a
   clean `.env`: secrets auto-generated, DSN accepted, credential key
   generated with a clear backup warning.
2. `invincible db upgrade` — ✅ migrated to revision `0008` cleanly.
3. `invincible start` — ✅ server up; `/health` OK; anonymous browser
   hit on `/dashboard` correctly 302'd to `/login?next=/dashboard`
   (the 3b fix working in the real app, first time observed live).
4. Registered the first account → ✅ bootstrapped to **operator**
   (verified in the DB), dashboard rendered with correct empty-state
   count cards.
5. OAuth dynamic registration → PKCE authorize → consent as operator
   → code → token exchange — ✅ full flow, refresh token issued.
6. MCP over the wire — ✅ `tools/list` (7 tools), `read_file` call,
   and the staged `write_file` → `confirm_action` approval path wrote
   the file.
7. Chat completion — ✅ HTTP 200 via `agentrouter-glm`.

### Findings (rough edges the tests couldn't catch)

| # | Finding | Severity |
|---|---|---|
| R1 | **`setup` cannot be piped/scripted on Windows** — click's hidden prompts use `getpass`, which reads the Windows *console* (msvcrt) and ignores piped stdin; the wizard hangs silently after the first answer. Fine for a human at a terminal; blocks automation/CI/smoke-scripts. | Medium (blocks #2's installer ambitions) |
| R2 | **The DSN prompt accepts any URL without validating connectivity** — a typo surfaces only later at `start`/`doctor`. One `SELECT 1` probe at prompt time would fix it. | Low |
| R3 | **`GATEWAY_API_KEY` is generated silently** with no explanation that chat endpoints now REQUIRE it (the fail-open path only applies when unset). A first-time user who later pastes their `.env` elsewhere may be confused why their old curl no longer works. | Low |
| R4 | **`dev-db` hardcodes port 5433** — two invincible installs on one machine collide; also collides with the documented dev convention. Needs a port-choice or auto-increment. | Low |
| R5 | **Fresh install with ZERO provider keys still gets working chat** — the router called agentrouter.org with no key and got 200 (glm-5.3 answered). Pleasant out-of-box surprise, but undocumented and possibly unintended: no startup warning fired for the missing `AGENTROUTER_API_KEY`. Worth verifying intent and documenting either way. | Info / verify |
| R6 | The rehearsal itself had no scripted harness — driving it required a bespoke in-process driver. A tiny `tests/test_fresh_install.py` (scratch DB → upgrade → TestClient boot) would pin the journey CI-green permanently. | Medium (turns rehearsal into regression armor) |

**Evidence for #2 (distribution):** with the non-interactive rewrite
(below), `setup` is now one scriptable command —
`invincible setup --db-url <remote-DSN>` — so the install story is
install → setup → start, with only the DSN as user input. The
Python-clone-venv gap remains the other half.

**Follow-up (2026-09-01): R1 FIXED — setup is now non-interactive.**
Zero prompts: secrets auto-generated, the six provider-key prompts
removed (providers are configured via the dashboard's Providers page
or the env file), and the DB URL arrives via `--db-url` (remote-first:
Neon or any managed Postgres; plain `postgresql://` auto-upgraded to
asyncpg). First run without `--db-url` and without an existing value
fails cleanly with guidance instead of prompting. Verified against the
real CLI with piped stdin — the Windows getpass hang is gone. R2 (DSN
connectivity check) and R4 (dev-db port collision) remain open.

---

## 2. Distribution — the real frontier 📦 — NOT STARTED

**Claim:** "Anyone can use Invincible" currently means: anyone with
Python, who clones the repo and sets up a venv. Two gaps:

- **Not installable by normal people.** No PyPI publish, no one-file
  .exe. Matches ROADMAP historical table: "Zero-clone distribution
  (PyPI) — Not started; folds into P6/P7 packaging."
- **The database is the hard part.** `main.py:76-82` hard-fails
  without `INVINCIBLE_DB_URL` — Postgres (Docker, `dev-db`, or manual)
  is mandatory before first run. No SQLite fallback, no bundled
  portable-Postgres path for end users. This is the biggest barrier to
  "grandma-friendly."

**Status:** genuine multi-day project with real decisions (PyPI vs
.exe; bundled portable Postgres vs embedded SQLite fallback). Deciding
scope belongs in Platform Phase 6/7 planning — decide with fresh eyes
**after** the rehearsal (#1) provides evidence about how big the
database problem really is.

---

## 3. Small polish (~30 min each) — **3a + 3b DONE (2026-08-31)**

### 3a. `start` opens health-JSON instead of the dashboard — ✅ FIXED

`cli.py` now opens `/dashboard` (which redirects anonymous browsers to
`/login`); was verified as opening `/` (health JSON). Pinned by
`tests/test_browser_entry.py::test_start_opens_dashboard_not_health_json`.

### 3b. Unauth'd `/dashboard` shows raw JSON 401 — ✅ FIXED

`main.py` now has a global HTTPException handler: 401 + GET/HEAD +
browser `Accept: text/html` → 302 to `/login?next=<path>`; HTMX
requests get `HX-Redirect`; every other client keeps the byte-identical
JSON 401. The login page replays the `next` target through a hidden
form field into the existing `_safe_next` POST handling (open-redirect
safe). POSTs are never redirected. Pinned by
`tests/test_browser_entry.py` (11 tests).

### 3c. Password reset flow — ✅ Verified gap

Zero matches for reset/forgot anywhere in `invincible/`. Already
listed in ROADMAP "Honest limitations." Barely matters for personal
instances; future work.

---

## 4. Housekeeping (10 min, do sometime)

- **Move the dev database out of the Temp folder** before a disk
  cleanup eats it (portable PG in Temp on port 5433, manual start,
  dies on reboot).
- **Revoke stale Claude/Grok connectors** — server-side state; check
  via `invincible oauth list`. Not verifiable from the repo.

---

## Recommended order

1. ~~**#3a + #3b** — prerequisites.~~ **DONE 2026-08-31.**
2. ~~**#1 dress rehearsal** — fast, produces evidence.~~ **DONE
   2026-09-01** (findings R1–R6 above).
3. **Decide #2** (distribution) with fresh eyes, informed by the
   rehearsal. **← NEXT** — the evidence says: bundle portable
   Postgres + publish (PyPI or .exe), and fix R1 first because any
   installer must script the wizard.
4. **#4 housekeeping** whenever.

## Roadmap context (from ROADMAP.md)

- **Phases 1–5: Implemented** (identity, isolation, account API,
  memory/continuity/context, dashboard).
- **Phase 9 (BYOK): In progress** — PR-A/B/C landed (Fernet crypto,
  migration `0007`, connect/list/test/remove API with SSRF guard,
  per-user router candidate pool). **Remaining: dashboard Providers
  UI (PR-D).**
- **Planned:** Phase 6 CLI Client Experience → Phase 7 Deployment
  (Neon + Railway, invincible-ai.me) → Phase 8 Cleanup.
- Distribution (#2 above) folds into Phase 6/7 packaging.
