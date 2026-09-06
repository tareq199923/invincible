# Handoff — Multi-Tenant Audit Step 3: deployment-posture hardening

> **Prereq done:** Step 1 (HIGH-1+HIGH-2, commit `0583bb5` + `382cd1d`) and
> Step 2 (fail-loud fallbacks, commit `e113842`) are fixed, tested (suite
> 1047 passing), and **deployed to production** (pushed 2026-09-07).
> Full context: `docs/MULTI-TENANT-AUDIT.md` (§4 Step 3 = this task).

## Goal

Harden deployment posture per audit §4 Step 3 so a public deploy can't be
subverted by pre-registration races or unthrottled anonymous endpoints.

## The changes

1. **MEDIUM-1 — gate the first-human operator bootstrap**
   (`invincible/core/accounts.py:112-150`, `UserService._insert`, shared by
   password AND GitHub registration paths).
   Today the first `is_system=false` account on a fresh instance is
   auto-promoted to operator. On a public deploy where the real owner hasn't
   registered yet, a stranger wins the race → can approve OAuth clients,
   manage providers, read any session's graph.
   - **Chosen approach (audit's preferred option):** bootstrap only when
     `INVINCIBLE_OWNER_SECRET` is unset (true self-host). On the hosted site
     the secret IS set, so no stranger ever bootstraps — the owner's
     account was created first anyway. Optionally also support an explicit
     `INVINCIBLE_ALLOW_FIRST_OPERATOR=1` env escape hatch for self-hosters
     who want the old behavior.
   - Update the comment in `_insert` explaining the gating.
   - Watch for test fallout: `tests/conftest.py:operator_session()` relies
     on first-human bootstrap (registers a FRESH account expecting operator).
     The test env sets `TEST_OWNER_SECRET`/`INVINCIBLE_OWNER_SECRET` —
     check `tests/conftest.py` and decide: either make the tests' secret
     unset for that fixture, or use `promote_operator` there instead.
     Also grep for other tests relying on the bootstrap
     (`first-human`, `operator bootstrap`, `_insert`).
   - Regression tests: (a) with owner-secret SET, first registration is a
     plain `user` role; (b) with secret unset, first registration still
     bootstraps operator (self-host preserved); (c) the env-flag escape
     hatch if implemented.

2. **MEDIUM-4 — rate-limit the unauthenticated registration endpoints**
   - `POST /oauth/register` (`invincible/endpoints/oauth.py:336-366`) —
     open by design (the gate is consent, not registration) but currently
     has no rate limit / cap → `oauth_clients` table bloat, junk entries in
     operator's client lists.
   - `POST /auth/device/code` (`invincible/endpoints/accounts.py:608-628`) —
     same shape (rows do expire/sweep but writes are unbounded).
   - **Approach (audit's suggestion):** per-IP fixed-window limiter, reusing
     `LoginRateLimiter` / the `login_attempts` table with a new scope (e.g.
     "register" / "device_code"). Look at how login rate limiting is wired
     in `endpoints/accounts.py` (LoginRateLimiter usage) and imitate.
     429 response shape should match the project's existing error style.
   - Regression tests: N rapid calls from one IP → 429; calls under the
     limit still succeed; different IP unaffected (if the limiter is
     per-IP keyed in a testable way).

3. **Deployment posture check (no code):** confirm Railway keeps
   `INVINCIBLE_AGENT_ROUTING=1` (it does per project memory — agent routing
   is what makes "every user has MCP tools" safe: each user's commands run
   on their own machine). Consider making it the DEFAULT when multi-user
   mode is detected — if you attempt this, check
   `invincible/core/settings.py` for how the flag is read and whether a
   `users`-count gate is practical at startup. If it's risky, just document
   the requirement and skip.

## Hard constraints

- **Do not break** the audit §2 verified-clean surfaces.
- Local PG on 5433: start via
  `C:/Users/SARK/pgdev/pgsql/bin/pg_ctl.exe -D C:/Users/SARK/pgdev/data
  -l C:/Users/SARK/pgdev/pg_ctl_start.log start` (dies on reboot).
  Drop/recreate `invincible_test` on `UndefinedColumnError`; pytest
  truncates it (wipes dev users).
- **push = Railway auto-deploy to production.** Full `pytest` green AND
  `python -m ruff check invincible tests` clean BEFORE any commit; ask the
  user before pushing. CI matrix: Python 3.10–3.14, lint failed once on
  import sorting (I001) — run ruff locally.
- Test gotchas: fresh-table first registration auto-bootstraps operator —
  plain-user tests demote via raw SQL (this will CHANGE if MEDIUM-1 lands);
  `Principal` has no role field — role lives on `resolve_session`'s user
  dict; tests that read gateway-key/legacy writes now need explicit owner
  args (`local_owner_kwargs(engine)` helper in conftest, per Step 2).

## After Step 3

Step 4 (LOW cleanups: project UPDATE ownership predicates, /v1/models
filtering to caller's pool, IdentityStore.link IntegrityError handling,
`Cache-Control: no-store` on the raw-key page, remove dead `?new_key=`
param on setup_page, agent long-poll cap). Then Step 5's remaining test
gaps (agent dispatch two-user test, cross-user BYOK routing test — items
2/3/4; item 1 shipped with Step 1, item 5 with Step 2). Update
`docs/MULTI-TENANT-AUDIT.md` status lines as steps land.
