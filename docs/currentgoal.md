# Current Goal — Invincible

Working document (2026-08-31): the verified state of the four open work
items, cross-referenced against [ROADMAP.md](ROADMAP.md). Every claim
below was checked against the code, not assumed.

---

## 1. The dress rehearsal 🎭 — NOT DONE (recommended next)

**Claim:** The fresh-install journey is proven by tests, but tests run
against a warm database — so the real new-user path
(setup → start → register → connect a client → approve → tools run)
has never been walked end-to-end.

**Verified: TRUE.** `tests/conftest.py:140-143` does
`create_all_from_metadata` up front and TRUNCATEs between tests. The
actual user path — `invincible setup` wizard → explicit
`invincible db upgrade` (Alembic) → first-boot lifespan seeding — is
never exercised as a user experiences it.

**Plan:** wipe a scratch database, walk the exact brand-new-user path.
Any rough step is a real finding. Known past finds: password confusion,
health-JSON instead of dashboard, the promote bump.

**Sequencing note:** polish items 3a + 3b below are prerequisites for
the rehearsal passing cleanly — do them first (~1 hour combined), then
rehearse.

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

## 3. Small polish (~30 min each) — CONFIRMED, DO FIRST

### 3a. `start` opens health-JSON instead of the dashboard — ✅ Verified

`cli.py:716-717` opens `http://host:port/`; `main.py:183-185` routes
`/` to `health_check()` returning `{"status": "healthy"}`. The comment
at `cli.py:709` ("start opens the dashboard for you") disagrees with
the URL it actually opens.

**Fix:** open `/dashboard` (redirecting to `/login` when not signed
in — requires 3b). Note the existing `--open-browser/--no-open-browser`
flag (`cli.py:667-669`) and the tested headless guard
(`_browser_session_available`) are already in place.

### 3b. Unauth'd `/dashboard` shows raw JSON 401 — ✅ Verified

`accounts.py:137-148` (`require_user_session`) raises a 401
`HTTPException` with a JSON body; `main.py` registers no exception
handler to convert it to a login redirect for browser requests. Every
dashboard page inherits this.

**Fix:** accept-header-aware handler — `Accept: text/html` gets a 302
to `/login` (preserving a same-origin bounce target), everything else
keeps the structured JSON 401. **Coupled with 3a:** pointing `start`
at `/dashboard` without this lands users on a JSON 401.

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

1. **#3a + #3b** (~1 hour) — prerequisites, trivially verified fixes.
2. **#1 dress rehearsal** — fast, produces evidence.
3. **Decide #2** (distribution) with fresh eyes, informed by the
   rehearsal.
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
