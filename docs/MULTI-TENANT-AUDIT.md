# Multi-Tenant Isolation Audit — Full Report & Work Plan

**Date:** 2026-09-07
**Auditor scope:** all 41 source modules (~18.3k lines), 8 Alembic migrations, test suite.
**Status:** Audit complete. Step 1 APPLIED 2026-09-07 (HIGH-1 + HIGH-2 fixed, 5 regression tests added). Step 2 APPLIED 2026-09-07 (all silent local-owner fallbacks fail loudly; MEDIUM-2 + LOW-1 closed along the way). Step 3 APPLIED 2026-09-07 (MEDIUM-1 bootstrap gated on INVINCIBLE_ALLOW_FIRST_OPERATOR / no-secret; MEDIUM-4 per-IP rate limits on /oauth/register + /auth/device/code; agent-routing deployment posture documented in SECURITY.md §10). Step 4 APPLIED 2026-09-07 (LOW-2/LOW-3/LOW-5 closed - see the per-finding FIXED notes below). Step 5 test gaps remain open. This document is the handoff.

---

## 1. Background — why these problems exist

Invincible was originally built as a **single-user** tool (one owner, one machine,
one provider pool). Mid-development it pivoted to a **multi-user platform**
(accounts, BYOK providers, per-user memory/sessions, local agents). The pivot was
done well — most per-user isolation landed correctly — but the single-user
foundation still leaks through in specific places.

### The product vision (from the owner, verbatim intent)

> Every user uses ALL Invincible functions — MCP tools, AI providers, settings,
> everything — without interrupting other users' work.

### The root-cause pattern behind most findings: the "local owner" ghost

When Invincible was single-user, code never had to ask *"whose data is this?"*.
A system account exists — `local@invincible.local`, the **local owner**
(`invincible/core/db.py:219`) — and many code paths still say:

> *"If no identity is specified... just assume it's the local owner."*

Every one of those silent fallbacks is now a place where **user B's action can
land in the operator's account** (or vice versa). Both HIGH findings are this
exact pattern. The fix strategy (see §4) is to make these fallbacks **fail
loudly** so the bug class dies, not just the two known instances.

### The key design distinction to preserve

Two things the current code blurs into one "operator" role:

1. **Product functions** (chat, MCP tools, BYOK providers, memory, agent,
   dashboard) → every user gets all of these, fully isolated. *Already ~90% done.*
2. **Server administration** (shared `providers.yaml` pool, routing config,
   server flags, `invincible users ...` CLI) → physically global, one server.
   *This is the only place an operator/admin concept should remain.*

---

## 2. Architecture as verified (what is already correct)

Do not change these — they are the model the fixes should imitate.

| Surface | Where | Verdict |
|---|---|---|
| Dual-realm gateway auth | `endpoints/auth.py` | legacy gateway key → local owner; `inv_` key → its user; fail-open anonymous only when `GATEWAY_API_KEY` unset (documented local mode) |
| MCP auth | `endpoints/mcp.py:302-345` `require_mcp_auth` | OAuth 2.1 + PKCE bearer tokens resolve to the consenting user's `subject_user_id` |
| Agent dispatch | `core/agent_registry.py`, `endpoints/agents.py` | **Structurally isolated**: queues/futures keyed by `user_id`; `/agent/poll` + `/agent/result` auth via inv_ key (`require_agent_auth`); `submit_result` rejects wrong-owner/unknown/timed-out jobs indistinguishably. No code path can route user A's job to user B's machine |
| Agent sandbox | `agent/sandbox.py` | reads/writes confined to user home (or `INVINCIBLE_AGENT_ROOT`), credential-file denylist, client-side re-run of server denylist (wall 2) |
| BYOK routing | `endpoints/byok.py:92-153`, `core/router.py:270-275,504-531` | candidates built ONLY from caller's credential rows; empty list = 400, no fallback to operator pool in either direction; per-credential `health_id` (`byok:{id}`) prevents cross-user cooldown poisoning; lazy decrypt |
| Credential storage | `core/credential_store.py`, `core/credential_crypto.py` | Fernet at rest; fail-closed without `INVINCIBLE_CREDENTIAL_KEY`; only `key_masked` ever returned; ownership predicate on every read/write |
| SSRF guard | `core/url_safety.py` | https-only, blocks private/loopback/metadata ranges, rejects userinfo/dotless hosts; re-checked at create AND every test/chat use (known residual: validate-then-connect DNS rebind window, seconds wide, accepted) |
| Memory store | `core/memory.py` | every method takes mandatory `user_id`, no local-owner fallback; delete is ownership-predicated, foreign = 404 |
| Continuity/runs reads | `core/continuity.py`, `core/run_store.py` | scoped by `session_pk` resolved under acting principal; `usage_summary`/`list_for_user` isolate via `sessions` join; NULL-`session_pk` legacy rows are inert |
| Dashboard | `endpoints/dashboard.py` | all routes `require_user_session`; session detail via `lookup_by_pk` with full ownership triple; anti-enumeration everywhere (foreign = 404-shaped) |
| Admin API | `endpoints/admin_api.py` | fail-closed 503 without `INVINCIBLE_OWNER_SECRET`; operator-role session/key only; 403 for plain users |
| OAuth flow | `endpoints/oauth.py`, `core/oauth_store.py` | PKCE-only, single-use codes, hashed tokens, subject stamped at consent, POST-only consent (SameSite=Lax CSRF posture), per-scope persistent login lockouts |
| Browser sessions | `core/accounts.py` `SessionManager` | HMAC-signed v2 cookies with `session_version` pinning — password change orphans all prior cookies |
| Migration 0003 | `migrations/versions/20260826_0003_isolation.py` | backfills all legacy rows to the local owner (correct for single-operator era); NULL-owned rows never match user-scoped queries |

---

## 3. Findings (full detail)

### 🔴 HIGH-1 — Non-streaming `/v1/messages` persists turns to the LOCAL OWNER's session

- **Files:** `invincible/endpoints/anthropic_compat.py:236-244` (broken call site),
  `:62-96` (`_persist` helper), `invincible/core/session_store.py:93-107`
  (`_owner` local-owner fallback).
- **What happens:** the non-streaming path calls:
  ```python
  await _persist(
      store,
      session_id,
      internal_messages,
      _assistant_message_from_provider(choices[0]["message"]),
      memory,
  )
  ```
  `principal` is omitted → `_persist` runs `store.append(session_id, new_turns,
  **{})` → `SessionStore._owner(None, None)` falls back to the local owner.
  The **streaming** path is correct (`save_complete` passes `principal`, line
  ~187) — only the non-streaming branch is broken.
- **Impact (cross-tenant write + leak + prompt poisoning):**
  1. Any `inv_`-key user's non-streaming Anthropic conversation is appended to
     the **operator's** session row with the same client session string.
  2. Operator sees the user's content in `/dashboard/sessions` + session detail.
  3. Operator's own next request with the same session string **loads the
     user's turns as history** → user content injected into the operator's
     LLM prompt.
  4. `memory.record_memories` silently skipped for these turns (principal None
     early-return) — functional bug that also masks the misroute.
- **Why tests missed it:** `tests/test_isolation.py` covers graph/task/run/
  facts/approval scoping, but no cross-user test exercises the non-streaming
  `/v1/messages` persist path.
- **Fix (one line):**
  ```python
  await _persist(store, session_id, internal_messages,
                 _assistant_message_from_provider(choices[0]["message"]),
                 memory, principal)
  ```
- **Regression test to add:** user B (api_key realm) sends non-streaming
  `POST /v1/messages` with session "default"; assert the local owner's
  `SessionStore.load("default", user_id=local_owner)` returns [] and B's own
  load returns the turns.

### 🟠 HIGH-2 — Any dashboard user can view & revoke the local owner's / unowned MCP OAuth clients

- **Files:** `invincible/endpoints/dashboard.py:691-760` (`mcp_page`,
  `revoke_mcp_client_tokens`); `invincible/core/oauth_store.py:346-371`
  (`list_clients_manageable` — includes `owner_user_id IS NULL` and the local
  owner for any caller).
- **What happens:** the ownership check is:
  ```python
  owner = client_row["owner_user_id"]
  if owner is not None:
      local_uid, _ = await ensure_local_owner(...)
      if owner not in (principal.user_id, local_uid):   # ← hole
          raise HTTPException(404, ...)
  ```
  Every logged-in user passes the test for the admin's clients (and for any
  unowned client). `mcp_page` also lists them: client names, redirect URIs,
  active token counts.
- **Why it exists (deliberate):** pre-Phase-5 clients belong to a system
  account that never logs in; the relaxation kept them manageable. Correct for
  single-user self-hosting; breaks the tenant invariant on the hosted site.
- **Impact:** any registered user can `DELETE /dashboard/mcp/clients/{id}/tokens`
  and instantly kill every live MCP token of the operator's AI connections —
  repeatable denial-of-service, plus info disclosure.
- **Fix:** gate local-owner-era/unowned client management (and listing) to
  `ROLE_OPERATOR` sessions. Regular users see/manage only
  `owner_user_id == principal.user_id`. Need the user row's role —
  `require_user_session` already resolves it via `resolve_session` (returns
  `role`); thread it through or re-fetch with `UserService.get()`.
- **Regression test:** plain user gets 404 on revoke of a local-owner client;
  operator session succeeds; plain user still revokes own client.

### 🟡 MEDIUM-1 — First-human registration bootstrap grants `operator` to first self-registered account

- **File:** `invincible/core/accounts.py:112-150` (`UserService._insert`,
  shared by password AND GitHub registration paths).
- First `is_system=false` account on a fresh instance is auto-promoted to
  operator. On a public deploy where the real owner hasn't registered yet, a
  stranger wins the race → can approve OAuth clients, manage providers
  (`/api/v1/*`), read any session's graph (operator override,
  `endpoints/graph.py:79-80`).
- **Latent on current deployment** (owner registered first), **active for every
  future public deploy**.
- **FIXED (Step 3, 2026-09-07):** the bootstrap now fires only when
  `settings.allow_first_operator()` is true — no owner secret configured
  (bare self-host) OR `INVINCIBLE_ALLOW_FIRST_OPERATOR` explicitly set.
  `invincible setup` writes the flag into FRESH .env files only, so the
  out-of-the-box one-person self-host keeps the terminal-free bootstrap
  while hosted/public deploys (hand-built env) never grant operator to a
  stranger winning the registration race (elevation there =
  `invincible users promote`, audit-logged). Regression tests:
  `test_secret_set_flag_absent_first_registration_is_plain_user` and
  `test_no_secret_bootstraps_without_the_flag`
  (tests/test_first_operator_bootstrap.py).

### 🟡 MEDIUM-2 — Legacy pending actions with `owner_subject=None` confirmable by anyone (fail-open)

- **File:** `invincible/core/tool_executor.py:337-339`:
  ```python
  owner = record.get("owner_subject")
  if owner is not None and requester_subject != owner:
      return None
  ```
  `owner is None` → no check → any MCP-authenticated user confirms it.
- Reachable via `load_persisted()` (lines 213-242) when
  `INVINCIBLE_PERSIST_PENDING_ACTIONS` is on and rows were staged by a
  pre-Phase-2 process. Tiny window (rows expire at TTL and `_sweep()` runs on
  load) → **latent fail-open pattern**, not a live hole.
- **FIXED (Step 2, 2026-09-07):** `take()` now treats a subject-less record
  as not-found for any subject-holding requester (fail closed); subject-less
  requesters keep access. Regression test:
  `test_subject_less_record_not_confirmable_by_subject`
  (tests/test_tool_executor.py).
- **Fix:** fail closed — when `requester_subject is not None and owner is
  None`, treat as not-found (or discard subject-less records at load).

### 🟡 MEDIUM-3 — Device-code interception binds the victim's agent to the attacker's account

- **Files:** `endpoints/accounts.py:678-694` (`device_approve` binds the
  *approver's* identity); `core/accounts.py:588-598` (`DeviceCodeStore.approve`).
- Pairing semantics: "the approver donates their identity to the device."
  Attacker who learns the 8-char `user_code` within its 10-min TTL (shoulder
  surf / pasted in chat) and approves it FIRST with their own session gives
  the victim's `invincible agent` an API key bound to the ATTACKER's user →
  attacker's MCP `confirm_action`/`read_file` dispatch to and execute on the
  VICTIM's machine (bounded by the home-dir sandbox + denylists). Victim's own
  approve attempt then reads "Unknown or expired code" — the only tell.
- Mitigations already present: codes unguessable (31^8), single-approve,
  10-min TTL, sandbox walls.
- **Fix idea:** display a short hash of the `device_code` on both CLI and
  approval page so the approver verifies the code belongs to the machine in
  front of them; optionally rate-limit `/auth/devices/{code}` probes.
- **FIXED (2026-09-07):** both surfaces now show the machine fingerprint —
  first 8 hex chars of sha256(device_code), one shared derivation
  (`DeviceCodeStore.fingerprint` / `fingerprint_from_hash`). The CLI
  (`invincible login` and `invincible agent`'s first-run self-pairing,
  via `_pair_device` → `on_code`) prints it next to the user_code; the
  approval page (`device.html`) shows the same value with a
  must-match/deny-on-mismatch instruction. The raw device_code never
  reaches the page. Regression tests:
  `test_device_page_shows_machine_fingerprint`,
  `test_device_fingerprint_identifies_the_request`
  (tests/test_accounts_ui.py);
  `test_pair_device_fingerprint_matches_approval_page`,
  `test_pair_device_fingerprint_is_device_code_hash`
  (tests/test_cli_login.py). Probe rate-limiting was already covered by
  Step 3's MEDIUM-4 device-code scope.

### 🟡 MEDIUM-4 — Unauthenticated, unthrottled endpoints

- `POST /oauth/register` (`endpoints/oauth.py:336-366`) — open by design (the
  gate is consent, not registration) but has **no rate limit / cap** →
  `oauth_clients` table bloat, junk entries surface in operator's client lists.
- `POST /auth/device/code` (`endpoints/accounts.py:608-628`) — same shape
  (rows do expire/sweep).
- **FIXED (Step 3, 2026-09-07):** both endpoints now carry per-IP
  fixed-window caps (10 requests / 15 min) reusing `LoginRateLimiter` /
  the `login_attempts` table with dedicated scopes (`client-register`,
  `device-code`) — deliberately separate from every login scope, so an
  anonymous flood can never lock a real user out of signing in. Every
  attempt counts (valid or not: each is a potential row write). 429s
  match each surface's existing error shape and are audit-logged
  (`oauth.register_limited`, `device.code_limited`). Regression tests
  in tests/test_anonymous_rate_limits.py (over-cap 429 + no row,
  under-cap OK, different IP unaffected, scope separation from login).

### 🟢 LOW-1 — Anonymous fail-open principal rides the operator's provider pool

- `endpoints/auth.py:76-83`; `byok_attempt_source` (`endpoints/byok.py:112-113`)
  only scopes `kind == "api_key"`. With `GATEWAY_API_KEY` unset, any
  unauthenticated caller = local owner (their provider spend, sessions,
  memory). Loudly warned at startup; documented local mode.
- **FIXED (Step 2, 2026-09-07):** `require_auth` refuses the anonymous
  principal once more than one human (`is_system = false`) account exists —
  "local mode" is meaningless on a multi-user instance. Exactly-one-human
  local mode still works. Regression tests in tests/test_dual_realm.py
  (`test_fail_open_survives_exactly_one_human_account`,
  `test_fail_open_refused_once_multi_user`).

### 🟢 LOW-2 — `ProjectService.rename`/`archive` UPDATE lacks `user_id` predicate

- `core/accounts.py:483-518` — `_owned()` precheck then
  `UPDATE projects ... WHERE id = X` without `user_id`. Not currently
  exploitable (ownership checked first and never changes) but breaks the
  predicate discipline.
- **FIXED (Step 4, 2026-09-07):** both UPDATEs now carry
  `projects.c.user_id == user_id` alongside the id predicate.

### 🟢 LOW-3 — `/v1/models` discloses operator pool's provider/model names to all users

- `endpoints/openai_compat.py:195-208` — BYOK-only users see models they can
  never route to.
- **FIXED (Step 4, 2026-09-07):** `list_models` resolves the caller via
  `require_auth` and lists the caller's EFFECTIVE pool through
  `byok_attempt_source` (same split as chat routing): api_key/BYOK
  principals see only their own connected credentials (empty list with
  none, mirroring the chat 400); legacy/anonymous local-mode principals
  keep the operator pool. Regression tests in tests/test_chat_byok.py
  (BYOK-only list, empty pool, gateway-key keeps operator pool).

### 🟢 LOW-4 — `INVINCIBLE_DEBUG_400` dumps full conversation payloads to disk

- `core/router.py:129-161` — opt-in, gitignored, but on a shared host these
  files mix tenants' chat content. Document or scope per-request.

### 🟢 LOW-5 — Minor robustness

- `IdentityStore.link` (`core/accounts.py:693-712`) check-then-insert without
  `IntegrityError` handling → concurrent race 500s (unique constraint still
  holds the isolation line).
- One-time raw-key page (`endpoints/accounts.py:548-551`) rendered without
  `Cache-Control: no-store` → bfcache may retain the raw `inv_` key.
- `setup_page`'s `?new_key=` query param (`endpoints/dashboard.py:675`) is
  read but never set by any flow — dead parameter; remove before someone sets
  it (raw keys must never ride URLs).
- Agent long-poll connections unbounded per user (in-memory registry, 1-replica
  constraint) — cheap cap if it ever matters.
- **FIXED (Step 4, 2026-09-07), all four items:**
  1. `IdentityStore.link` is now insert-first: a lost uniqueness race hits
     the `IntegrityError`, re-reads the winner's row, and returns it
     (reporting the row's actual owner) — never a 500. Tests in
     tests/test_accounts.py (`test_identity_link_duplicate_returns_existing_owner`,
     plus re-link assertions).
  2. The one-time raw-key page is served with `Cache-Control: no-store`
     (tests/test_account_keys_ui.py
     `test_one_time_key_page_is_never_cached`).
  3. The dead `?new_key=` param is removed from `setup_page` (the template
     never rendered it).
  4. `AgentRegistry.poll` enforces `MAX_POLLS_PER_USER = 5` concurrent held
     polls per user (`PollCapacityExceeded` → endpoint 429 + `Retry-After`,
     slots released in `finally` so aborted connections never leak). Tests
     in tests/test_agent_registry.py and tests/test_agent_endpoints.py.

---

## 4. The work plan (punch list, ordered)

### Step 1 — Fix the two live bugs (APPLIED 2026-09-07)

1. HIGH-1: ✅ FIXED — `principal` now passed in `anthropic_compat.py`
   non-streaming `_persist` call; regression test
   `test_anthropic_non_streaming_persists_to_the_caller`
   (tests/test_isolation.py) asserts turns land under the caller, not
   user B, not the local owner.
2. HIGH-2: ✅ FIXED — `dashboard.py` `mcp_page` +
   `revoke_mcp_client_tokens` gate local-owner-era/unowned clients to
   `ROLE_OPERATOR` sessions (role via `resolve_session`; regular users
   see/manage only `owner_user_id == principal.user_id`). Also fixed:
   `oauth_store.list_clients_manageable`'s `owner_user_id IS NULL` arm
   was unconditional — now opt-in via `include_unowned=True`
   (otherwise unowned rows leaked even with correct `user_ids`).
   Regression tests in tests/test_dashboard_mcp.py: plain user denied
   (404-shaped, anti-enumeration), operator still manages legacy
   pools, plain user keeps own-client control, operator cannot touch
   another user's client.

### Step 2 — Kill the local-owner fallback bug CLASS (APPLIED 2026-09-07)

Make silent fallbacks fail loudly so future forgotten-`principal` bugs become
obvious errors, not silent cross-user data mixing:

1. ✅ `SessionStore._owner` (`core/session_store.py`): the fallback is GONE.
   `user_id`/`project_id` are now REQUIRED keyword args on
   `load`/`save`/`append`/`session_meta`/`turn_overview` (matching
   `lookup`/`resolve_or_create`, which always required them) — an owner-less
   call is a loud `TypeError` at the signature, and `_owner` (plus its
   latent dead-branch bug at old line ~106) is deleted outright. Every
   production call site already passed an explicit owner (verified by
   grep before the change); the only fallback users were tests, which now
   pin the owner explicitly. `anthropic_compat._persist` also takes a
   required `principal` now (both call sites passed it since Step 1).
   Regression test: `test_owner_less_calls_raise`
   (tests/test_session_store_v2.py).
2. ✅ `require_mcp_auth` (`endpoints/mcp.py`): a token with no
   `subject_user_id` (or a missing engine) → 401 with the
   WWW-Authenticate challenge, never a local-owner principal. Regression
   test: `test_mcp_subject_less_token_returns_401`
   (tests/test_mcp_endpoint.py).
3. ✅ `require_auth` fail-open (`endpoints/auth.py`): the anonymous
   principal is refused once more than one human (`is_system = false`)
   user exists (see LOW-1 above for tests).
4. ✅ MEDIUM-2 along the way: fail-closed `PendingActionStore.take()` for
   `owner is None` + non-None requester (see MEDIUM-2 above).

### Step 3 — Deployment-posture hardening (APPLIED 2026-09-07)

1. ✅ MEDIUM-1: first-human operator bootstrap gated on
   `INVINCIBLE_ALLOW_FIRST_OPERATOR` / no-owner-secret (see MEDIUM-1
   above); `invincible setup` opts fresh self-hosts in.
2. ✅ MEDIUM-4: per-IP rate limits on `/oauth/register` and
   `/auth/device/code` (see MEDIUM-4 above).
3. ✅ Agent-routing posture: production keeps
   `INVINCIBLE_AGENT_ROUTING=1` (Railway vars); the flag stays
   opt-in (defaulting on at multi-user detection was considered and
   rejected — live env reads, no startup user-count gate, and it would
   flip the OAuth consent relaxation as a side effect). Public-deploy
   requirement documented in docs/SECURITY.md §10 ("Deployment
   posture"), including the Railway → Azure migration check.

### Step 4 — Smaller cleanups (APPLIED 2026-09-07)

1. ✅ LOW-2: ownership predicates (`projects.c.user_id == user_id`) on the
   ProjectService rename/archive UPDATEs.
2. ✅ LOW-3: `/v1/models` lists the caller's effective pool (BYOK split
   mirrors chat routing; see LOW-3 above).
3. ✅ LOW-5: link race handled insert-first; `Cache-Control: no-store` on
   the one-time raw-key page; dead `?new_key=` param removed; per-user
   concurrent long-poll cap (MAX_POLLS_PER_USER = 5, 429 + Retry-After).

### Step 5 — Test gaps to close (regression armor)

Existing isolation tests live in `tests/test_isolation.py` (graph, task
states, checkpoints, runs, facts, approval subject, api keys). Add:

1. Cross-user **non-streaming** `/v1/messages` persist (would have caught HIGH-1).
2. Dashboard MCP client revoke authorization matrix (plain user vs operator vs
   own client) — would have caught HIGH-2. ✅ SHIPPED (Step 1, in
   tests/test_dashboard_mcp.py: `test_plain_user_cannot_see_or_revoke_legacy_clients`,
   `test_operator_cannot_revoke_another_users_client`).
3. ✅ Two-user agent dispatch: user A's job never appears in user B's
   `/agent/poll`; B's result submission for A's job_id is rejected
   indistinguishably from unknown/timed-out ids (anti-enumeration), and the
   forgery never resolves A's future. Shipped 2026-09-07 in
   tests/test_isolation.py (`test_agent_jobs_never_reach_another_users_poll`,
   `test_cross_user_result_submission_is_indistinguishable`).
4. ✅ Cross-user BYOK routing: user A's chat never routes through user B's
   credential — pinned with per-host counting MockTransport handlers
   (A's host hit exactly once; B's host and the whole operator pool
   untouched), plus the cooldown arm: A's sole credential in cooldown
   (`byok:{id}` health key) fails cleanly without consulting B's pool or
   the operator's. Shipped 2026-09-07 in tests/test_isolation.py
   (`test_byok_chat_never_routes_through_another_users_credential`,
   `test_byok_cooldown_never_falls_back_to_another_users_pool`).
5. Fallback-loudness tests: ✅ SHIPPED (Step 2) — `SessionStore` with no
   owner raises; MCP token with no subject 401s; anonymous principal
   refused once multi-user; subject-less pending actions not confirmable
   by a subject-holding requester.

---

## 5. Environment & verification notes (from project memory)

- Production: invincible-ai.me (Railway + Neon since 2026-09-02).
  `INVINCIBLE_AGENT_ROUTING=1` must stay set in Railway vars; 1 replica max
  while the agent registry is in-memory; push = auto-deploy.
- Local test Postgres: start `C:\Users\SARK\pgdev` via pg_ctl (dies on reboot);
  port 5433; drop/recreate `invincible_test` when tests hit
  `UndefinedColumnError` (create_all is non-additive; pytest truncates the DB
  and wipes dev users).
- Run tests: `pytest` (config in `pytest.ini`).
- After fixes: run the full suite, and verify HIGH-1 manually with two
  accounts (one non-streaming `/v1/messages` each; check the dashboard session
  lists don't cross).

## 6. One-paragraph summary for a fresh session

The audit found the multi-user pivot is ~90% correctly implemented (agent
dispatch, BYOK, memory, dashboard, admin gates all verified clean). The two
live bugs are both instances of the single-user "local owner" fallback: (1)
`anthropic_compat.py` non-streaming path omits `principal` so user chats are
saved into the operator's session; (2) the dashboard MCP page lets any user
view/revoke the operator's OAuth clients. **Both are now FIXED (2026-09-07,
Step 1 complete).** Step 2 is also APPLIED (2026-09-07): every silent
local-owner fallback now fails loudly (required owner on `SessionStore`, 401
for subject-less MCP tokens, anonymous principal refused once multi-user,
fail-closed pending actions). Step 3 APPLIED (bootstrap gate, anonymous
endpoint rate limits, deployment posture). Step 4 APPLIED (LOW-2/LOW-3/LOW-5
cleanups). Step 5 APPLIED (2026-09-07): all regression-armor tests shipped
(items 1-5, see §4 Step 5) and MEDIUM-3 fixed (device-code machine
fingerprint on both the CLI and the approval page). Remaining: the manual
two-account prod verification (§5).
