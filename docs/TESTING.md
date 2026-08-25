# Testing

The suite is pytest + pytest-asyncio. **No real provider is ever called** —
upstream HTTP is faked with `httpx.MockTransport`, and the whole FastAPI app
is exercised in-process with `httpx.ASGITransport`.

---

## 1. Running

Since Phase 16 the suite runs against a **real PostgreSQL database** (no
SQLite mocks) — storage fixtures hard-fail rather than skip when it is
unreachable:

```bash
pip install -e ".[dev]"
# Default target; override for other ports/hosts — CI sets this itself
# (service container on 5432, trust auth):
export INVINCIBLE_TEST_DATABASE_URL=postgresql+asyncpg://invincible@127.0.0.1:5433/invincible_test
pytest
```

`invincible dev-db` provisions a matching local instance (databases
`invincible` and `invincible_test`). No provider API keys are required —
every upstream is faked.

- `pytest.ini` sets `asyncio_mode = auto`, so async tests need no explicit
  markers (the two `.asyncio` markers that exist in test files are
  redundant but harmless).

---

## 2. Test doubles & fixtures (`tests/conftest.py`)

- **`provider_config(tmp_path)`** — writes a temp `providers.yaml` from a
  provider dict list and returns its path.
- **`make_router`** — builds a real `Router` against a temp config with
  `httpx.MockTransport`. It sets a fake API key in the environment for every
  provider (unless the key is in `missing_keys`) and routes mock responses
  **by hostname**:

  ```python
  handlers = {
      "alpha.example.com": httpx.Response(200, json=provider_body("alpha")),
      "beta.example.com":  my_callable,      # callable gets the httpx.Request
  }
  ```

  A handler can be a static `httpx.Response` or a function that returns one
  (to record calls, inspect `request.read()`, or raise
  `httpx.ConnectError`). Unknown hosts → `500` so a typo surfaces loudly.
- **`router_setter`** — replaces `app.state.router` (the test app is the
  real `invincible.main.app`), tracking every router so the fixture can
  close its httpx client afterwards.
- **`pg_engine`** — a function-scoped async engine on the shared test
  database (`INVINCIBLE_TEST_DATABASE_URL`): `create_all` from
  `core.db.metadata`, then `TRUNCATE … RESTART IDENTITY` after each test so
  storage tests never see each other's rows.
- **`client`** — an `httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
  base_url="http://test")`; sets `GATEWAY_API_KEY=test-gateway-key` and
  `INVINCIBLE_OWNER_SECRET=test-owner-secret` (MCP auth flows through the
  OAuth endpoints, which need the owner secret for the login step), and
  wires every store to `pg_engine` (including `app.state.engine` and
  `app.state.api_keys`, so dual-realm auth resolves against the test DB).
- **`pg_live`** — skip-gate for tests that need more than `pg_engine`
  (scratch databases, CLI provisioning flows); skips cleanly when no local
  Postgres is reachable.
- **`provider_body(name, content)`** — a canned OpenAI-shaped success body.

### Pattern: pending-action approval tests

`tool_executor._run_command` / `_write_file` are monkeypatched with probes
that record calls (or fail loudly), proving an action is staged but **never**
executed until `confirm_action` approves its token — decline, unknown, and
expired tokens never reach the real execution path.

### Pattern: fake clock

`test_health_tracker.py` monkeypatches `time.monotonic` with an advancing
closure (`fake_clock(seconds)`), so cooldown expiry is tested without
sleeping.

---

## 3. Coverage map

| File | What it pins down |
|---|---|
| `test_api.py` | Health check; chat success; streaming shape/deltas/`[DONE]`; missing/invalid/absent auth (401 vs open); 422 on empty body; all-providers-fail → 503; upstream 400 forwarded verbatim; gateway warning fires loudly when `GATEWAY_API_KEY` is unset and stays silent when it is set; **Phase 6**: `model` field accepted (no 422), `model: fast` routes to the aliased provider, aliases listed after model ids in `/v1/models`. |
| `test_anthropic_api.py` | `HEAD /` → 200; `/health` detail; `/v1/messages` non-stream shape, model-hint echo, `tool_use`/`tool_result` structural round-trip (ids preserved, response re-emits `tool_use` blocks with `stop_reason: "tool_use"`), system-content flattening (the only place content degrades to a `[tool_use: name]` placeholder); streaming: canonical event sequence (`message_start → … → message_stop`), `tool_use` content-block events (`input_json_delta` frames), delta reassembly, streamed session persistence, cross-protocol session sharing; mid-stream failure → well-formed `error` event; streaming/non-streaming failover; upstream 400 mapped + sanitized; auth (missing/wrong/open); invalid bodies (missing `messages` → 422, empty/unknown-role → 400); ignored optional Anthropic fields (`tools`, `temperature`, headers, `?beta=true`) never 422; pure-helper matrix for `translate_finish_reason`, `flatten_content_blocks`, `build_error`, `anthropic_to_internal`; **Phase 6**: `model: fast` routes to the aliased provider with its real `model_id` in the upstream payload. |
| `test_router.py` | Lowest-tier-first success; tier sorting; failover on 429 / 5xx / network error; **non-JSON 200 → malformed-json failover** (recorded as a failure, response closed); skipping providers in cooldown or with missing keys; 401 → permanent disable (verified across a second request); non-failover 4xx aborts with `UpstreamClientError`; all-fail raises; required-field validation; **Phase 6**: model aliases prefer the aliased provider (and stream_open too), an alias is soft (failing preferred provider still fails over through the tier order), exact `model_id` match prefers that provider, unknown model keeps tier order; query auth sends `?key=` with no Authorization header, custom `auth_param` + `chat_path` respected, `chat_path` override keeps bearer auth. |
| `test_provider_schema.py` | **Phase 6 schema validation**: valid config and empty provider list pass; missing `providers` key / non-list / non-mapping entries; missing required fields name the field; `tier` must be a bare positive int; duplicate names; `base_url` must be `http(s)://`; empty string fields; `max_context` positive int; timeout must be a mapping of positive known fields; aliases must be a list of non-empty strings with no cross-provider duplicates; `auth_type` restricted to `bearer`/`query`; `auth_param`/`chat_path` shape; unknown fields rejected; shipped `providers.yaml` validates with the expected aliases. |
| `test_health_tracker.py` | Exponential curve 30→60→120→240→300 (capped); success reset; cooldown expiry restores availability; disable survives any clock advance; providers tracked independently. |
| `test_context_trimming.py` | No-op under budget; oldest turns dropped but system kept; **tool_calls never split from tool results**; most-recent-turn kept even if oversized; turn grouping; token estimation bounds; per-provider `max_context` honored (payload sizes differ). |
| `test_session_store.py` | History replayed on second request within a session; **no cross-session leakage** (session-a's secret not visible in session-b); corrupt rows → empty history; **concurrent appends lose no turns** (25 parallel `append` calls all land); **OpenAI system messages not persisted** (mirrors the Anthropic guarantee) while still sent upstream every request; streamed replies persisted and replayed. |
| `test_timeouts.py` | Defaults when no `timeout:` block; partial override merges with defaults; full override; **real shipped `providers.yaml` parses** with the expected read timeouts (guards YAML typos). |
| `test_mcp_endpoint.py` | MCP auth (missing/wrong → 401, unset secret → 503); `tools/list` names (incl. `confirm_action`); blocked command → `isError` with **no token issued**; two-call approval flow: `execute_bash`/`write_file` stage pending without executing/writing, `confirm_action` approve → real result / deny → `Declined.` / unknown or non-boolean approve → nothing runs; token replay → `Unknown or expired`, never double-executes; unknown tool → -32601; read_file success / `.env` blocked / own-source allowed; write to protected path blocked without staging; JSON-RPC hardening: parse error -32700, non-object -32600, bad params -32602; notifications (no `id`) → 204 with empty body, even on param errors. |
| `test_tool_executor.py` | Parameterized denylist sweep over ~24 dangerous commands (Unix + Windows) and ~10 safe ones (incl. `rm -rf ./build`, `rd /s C:\build`, `del C:\temp\out.txt`); blocked commands/paths never issue a token; pending calls return `pending_confirmation` without executing/writing (probe asserts no run); `confirm_action` approve runs/writes for real, decline doesn't, unknown/expired → `not_found`; tokens are single-use (no double execution); write denylist blocks `.env*`, `providers.yaml`, `sessions.db`, `invincible/`, `tests/`, `.git/`; read denylist blocks only secrets but **allows** `providers.yaml`, `invincible/`, `tests/`; paths outside the repo are not write-denied; read errors are structured, not exceptions; **pending actions survive a restart over the shared PostgreSQL test database** (bash + write_file, still single-use, via `attach_engine` + `load_persisted`), expired entries are purged on load, an unreachable database degrades to memory-only without breaking staging, and persisted rows use wall-clock `created_at` (`time.time()`), so expiry works across real process restarts. |
| `test_main.py` | Lifespan wiring: without `INVINCIBLE_PERSIST_PENDING_ACTIONS` the `PendingActionStore` is memory-only (`_db is None` — clean slate on restart); with it set, the shared db file is passed through (persistence active). |
| `test_cli.py` | CLI registration + version; both console scripts declared in pyproject; `setup` creates/updates `.env`, preserves existing values/comments, generates secrets only when missing; `start` port validation, env-file loading, config path handling. |
| `test_identity.py` | **Phase 1**: argon2id round-trip + rejection (incl. NULL/garbage hashes); API-key minting shape (`inv_` prefix, sha256 hash, visible prefix, uniqueness); lifecycle create→resolve→revoke→unresolvable (idempotent revoke); revoked keys excluded from resolution; `last_used_at` telemetry; listings never leak raw/hash; default-project get-or-create ("personal", reused); audit-log record/recent round trip. |
| `test_dual_realm.py` | **Phase 1 auth acceptance**: legacy gateway key → session lands under the system *local* owner; API key → session under that user's project; same client string under two principals yields distinct rows (Phase 2 groundwork); unknown/missing tokens → 401 when gateway key set; revoked API keys → 401; unset gateway key preserves fail-open local behavior (and still prefers valid API keys); crafted cross-realm collision resolves as legacy (precedence pinned); Anthropic streaming threads the principal into persistence. |
| `test_migration_identity.py` | **Phase 1 migration acceptance** (live tier, scratch DBs): populated-0001 → head preserves row counts for sessions/turns/messages with full content mapping (unicode, tool_calls JSONB) and seeds exactly one local owner; fresh `create_all` database takes the seed-only branch; downgrade to 0001 restores the legacy string-keyed shape without losing rows and re-upgrades cleanly. |
| `test_migration_isolation.py` | **Phase 2 migration acceptance** (scratch DBs at 0002): counts preserved while `session_pk` ownership lands on the right surrogate sessions (including orphan-string backfill), OAuth subjects stamped to the local owner, legacy version constraint swapped for the owner-scoped partial unique index — proven by two owners sharing one client string maintaining independent version chains without collision; downgrade restores 0002. |
| `test_isolation.py` | **Phase 2 acceptance**: graph hides another principal's session (known:false, empty projection) with byte-identical negative shapes under enumeration probes; admin override still resolves any session; MCP task states/checkpoints are invisible across principals and independent chains coexist under one shared string; runs carry the owner's surrogate session; facts extracted from A's chat never reach B's outgoing context; staged `execute_bash` cannot be approved by a different subject (unknown-token semantics, action left intact) yet the owner's approval executes. |
| `test_accounts.py` | **Phase 3 services**: registration validation/normalization/duplicate-409, indistinguishable auth failures; session cookies (round-trip, tamper, expiry via re-signed payloads, fail-closed without a secret); projects CRUD + archive visibility + cross-user 404 + default-archive refusal; device codes (shape, pending→approve→single-winner claim minting a working key, deny, slow_down interval, unknown/expired, user_code uniqueness constraint); identity links (idempotent, per-provider); GitHub client against MockTransport (token exchange payload, profile, verified-primary email selection incl. fallback and unverified→None, authorize URL shape); scoped login lockouts (`auth-login` vs `owner` never share counters). |
| `test_migration_accounts.py` | **Phase 3 migration acceptance** (scratch DBs at 0003): pre-existing lockout rows land in the `owner` scope, composite (ip, scope) PK enforces per-scope counters, `user_identities`/`device_codes` constraints hold (unique provider account, unique user_code); downgrade restores the 0003 world (tables dropped, scope column gone, single-column PK back) and re-upgrades to head. |
| `test_accounts_api.py` | **Phase 3 HTTP acceptance**: full register → login → project → key → chat → revoke walk (revoked key dies immediately); registration validation + explicit 409; enumeration-safe login + persistent lockout; logout/me; project surface incl. archived visibility toggle and cross-user 404s; API-key listings never leak raw values, revoke is owner-scoped; realm separation — browser cookie worthless on `/v1/*`, MCP bearer and gateway key rejected on `/api-keys`; device flow e2e issuing working chat credentials with pending/slow_down/replay semantics, deny path, approval-requires-session; GitHub flows — new-account registration, verified-email auto-link onto the existing user, unverified-only rejection, state-mismatch rejection, identity-conflict refusal, disabled-without-credentials. |
| `test_cli_login.py` | **Phase 3 CLI pairing**: `_pair_device` happy path against the ASGI app (code printed once, key minted and usable on chat), denied request fails loudly, config save round-trip (`{"server", "api_key"}`), default path under `~/.invincible`, Click wiring (server normalization, output text, failure exit codes). |
| `test_accounts_ui.py` | **Phase 3 pages**: `/login` renders the form and shows the GitHub button only when configured; `/register` page; form-mode register/login (303 redirects, error re-render with messages); `/account` requires a session and shows email/projects/keys; device approval page template with code + forms; unknown-code result page (404). |

---

## 4. Writing a new test — quick recipe

```python
# 1. Router-level (no HTTP app):
router = make_router(
    handlers={"alpha.example.com": lambda req: httpx.Response(429)}
)
result = await router.route_request([{"role": "user", "content": "hi"}])

# 2. API-level:
async def test_something(client, router_setter):
    router_setter(handlers={"alpha.example.com": httpx.Response(200, json=provider_body("alpha"))})
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-gateway-key"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200

# 3. MCP tool layer (bypasses HTTP entirely):
store = tool_executor.PendingActionStore()
staged = tool_executor.execute_bash("echo hi", store)
result = await tool_executor.confirm_action(store, staged["token"], True)
```
