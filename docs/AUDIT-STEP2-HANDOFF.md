# Handoff — Multi-Tenant Audit Step 2: kill the local-owner fallback bug class

> **STATUS: EXECUTED 2026-09-07** (see docs/MULTI-TENANT-AUDIT.md §4 Step 2
> for what shipped). This document is retained for reference only.

**For:** next work session. **Prepared:** 2026-09-07. **Prereq done:** Step 1
(HIGH-1 + HIGH-2) fixed, tested, deployed — commit `0583bb5` + lint fix
`382cd1d`. Full context: `docs/MULTI-TENANT-AUDIT.md` (§4 Step 2 = this task).

## Goal

Every silent fallback to the local owner (`local@invincible.local`) must fail
loudly, so a future forgotten-`principal` bug becomes an obvious error instead
of silent cross-user data mixing. Root-cause pattern and rationale: audit §1.

## The four changes (audit §4 Step 2, with file anchors)

1. **`SessionStore._owner`** (`invincible/core/session_store.py:93-107`):
   raise when `user_id is None` on request-path callers, instead of falling
   back to the local owner. Keep explicit None ONLY for documented operator
   paths (`owner_context` / graph operator override) if legitimate ones
   remain — audit them first.
   - While in there, fix the latent dead-branch bug at line ~106:
     `fallback_project if project_id is None else user_id` — the else arm
     returns `user_id` as the project id. Wrong type/value; unreachable
     today but a trap.
2. **`require_mcp_auth`** (`invincible/endpoints/mcp.py:334-337`): a token
   with no `subject_user_id` → 401, not a local-owner principal.
3. **`require_auth` fail-open** (`invincible/endpoints/auth.py:76-83`):
   refuse the anonymous principal when more than one human
   (`is_system = false`) user exists in the DB — cheap `users` count check.
   Keep documented local mode (GATEWAY_API_KEY unset + single user).
4. **MEDIUM-2** (`invincible/core/tool_executor.py:337-339`):
   `PendingActionStore.take()` — when `requester_subject is not None and
   owner is None`, treat as not-found (fail closed). Alternatively discard
   subject-less records at `load_persisted()`.

## Tests to add (audit §5 item 5)

- `SessionStore` append/load with no owner raises.
- MCP token with no subject 401s.
- Anonymous principal refused once a second human user exists (and still
  works with exactly one).
- PendingAction: subject-less record not confirmable by a subject-holding
  requester.

## Hard constraints

- **Do not break** the §2 verified-clean surfaces (agent dispatch, BYOK,
  memory, dashboard, admin gate) — their patterns are the model to imitate.
- The gateway-key realm (`kind="legacy"`), anonymous local mode, and the
  local owner itself are legitimate: they carry an explicit Principal; only
  the *silent* fallbacks die.
- Local PG: start `C:/Users/SARK/pgdev/pgsql/bin/pg_ctl.exe -D
  C:/Users/SARK/pgdev/data -l C:/Users/SARK/pgdev/pg_ctl_start.log start`
  (port 5433, dies on reboot). Drop/recreate `invincible_test` on
  `UndefinedColumnError`; pytest truncates it (wipes dev users).
- **push = Railway auto-deploy to production.** Full `pytest` green first
  (suite was 1042 passing + new tests after Step 1; lint with
  `python -m ruff check invincible tests` — CI failed on I001 last time).
- Run ruff BEFORE committing; CI matrix is Python 3.10–3.14.

## Gotchas learned in Step 1

- Fresh-table first registration auto-bootstraps as **operator** (MEDIUM-1)
  — plain-user tests must demote via raw SQL
  `UPDATE users SET role='user' WHERE id=:id`.
- `Principal` has no `role` field; role lives on the `resolve_session`
  user dict (`user["role"] == ROLE_OPERATOR`) — the established pattern.
- Test patterns to imitate: `tests/test_isolation.py`
  (`_mint_user_and_key`, `alpha_handler`), `tests/conftest.py`
  (`register_account`, `promote_operator`, `operator_session`).

## After Step 2

Step 3 (MEDIUM-1 bootstrap gating, rate-limit `/oauth/register` +
`/auth/device/code`), Step 4 (LOW cleanups), Step 5 remaining tests. Update
`docs/MULTI-TENANT-AUDIT.md` status lines as steps land.
