# Architecture

How Invincible is put together, how a request flows through it, and the
non-obvious algorithms (context trimming, cooldowns, config resolution).

---

## 1. Module map

```
invincible/
├── main.py                     FastAPI app, lifespan, auth wiring, /health, HEAD /
├── cli.py                      Click CLI (setup / start / login / doctor / dev-db / db / oauth / secret / api-key)
├── providers.yaml              Canonical provider config (packaged)
├── templates/                  Jinja2 UI (login/register/account/device pages + dashboard)
├── migrations/                 Packaged Alembic environment (0001 baseline … 0006 session_version)
├── endpoints/
│   ├── auth.py                 require_auth: dual-realm Principal resolution for /v1/* (Phase 1)
│   ├── accounts.py             Phase 3: /auth/*, /projects, /api-keys, /sessions,
│   │                           device pairing, GitHub login, password set/change
│   │                           via /auth/password (session-cookie realm)
│   ├── dashboard.py            Phase 5: /dashboard* views (overview, sessions,
│   │                           detail, tasks, memory, usage, settings) plus the
│   │                           /memories and /usage JSON siblings — all behind
│   │                           require_user_session (cookies only; inv_* keys,
│   │                           admin key, MCP bearers excluded by construction)
│   ├── openai_compat.py        POST /v1/chat/completions, GET /v1/models
│   ├── anthropic_compat.py     POST /v1/messages (Anthropic protocol)
│   ├── mcp.py                  POST /mcp (JSON-RPC 2.0 dispatch, Bearer resource server)
│   ├── oauth.py                Built-in OAuth 2.1 + PKCE authorization server
│   │                           (/.well-known/oauth-*, /oauth/register|authorize|token|revoke)
│   ├── admin_api.py            /api/v1/* management surface (provider CRUD, routing modes)
│   └── graph.py                GET /api/v1/sessions/{id}/graph (continuity projection)
├── models/
│   └── anthropic.py            Pydantic request model (ignores unknown fields)
├── compat/
│   ├── common.py               Protocol-neutral internal-message helpers
│   └── anthropic.py            Pure Anthropic translators + SSE streaming
└── core/
    ├── router.py               Provider loading, failover, trimming, timeouts
    ├── provider_health.py      Per-provider failure counts + cooldowns
    ├── settings.py             Typed live-read accessors for every INVINCIBLE_* variable
    ├── principal.py            Authenticated Principal model (legacy | api_key | anonymous | session)
    ├── identity.py             Phase 1: argon2id primitives, API-key lifecycle, audit log
    ├── accounts.py             Phase 3: UserService/SessionManager/ProjectService/
    │                           DeviceCodeStore/IdentityStore/GitHubOAuth;
    │                           password set/change carries the session_version bump
    ├── db.py                   Engine factory + schema metadata + local-owner bootstrap
    ├── session_store.py        Conversation memory on PostgreSQL (sessions/turns/messages)
    ├── oauth_store.py          OAuth store on PostgreSQL (clients, codes, hashed tokens)
    ├── memory.py               Scoped memory writes (memories table; explicit
    │                           "remember this" triggers + deterministic extractor)
    ├── retrieval.py            Lexical memory retrieval (FTS x recency x kind
    │                           x confidence; AND→OR query fallback)
    ├── context_builder.py      One unified token budget for memory + continuity
    │                           injections (Phase 4)
    ├── run_store.py            Provider-run records incl. token accounting (runs table)
    ├── projection.py           Shared session/run/task-state projection builder —
    │                           one engine under the graph endpoint AND the
    │                           dashboard session-detail view (Phase 5B extraction)
    ├── continuity.py           Task-state/checkpoint engine + continuation brief
    │                           + reactive failover checkpoints (Phase 4)
    └── tool_executor.py        MCP tool execution + denylists + approval
```

Packaging (`pyproject.toml`):

- `providers.yaml` ships as **package data** and is loaded via
  `importlib.resources` (`invincible/core/router.py::load_providers_config`),
  so config resolution is identical from a checkout, editable install, or
  wheel. There is no repository-root fallback copy.
- Two console scripts, `invincible` and `inv`, both point at
  `invincible.cli:cli`.
- `python-dotenv` loads `.env` at import time in `main.py`.

---

## 2. Process lifecycle (`main.py`)

```
import main  →  load_dotenv()  →  build FastAPI app (title "Invincible")
                     │
        startup (lifespan)     │
        url = INVINCIBLE_DB_URL (required; missing → error pointing at
                                 `invincible dev-db`)
        engine = make_engine(url)
        create_all_from_metadata(engine)   (dev bootstrap from core.db metadata)
        warn_if_schema_stale(engine)       (LOUD warning if alembic_version is
                                            absent or ≠ head — never auto-migrates;
                                            `invincible db upgrade` is explicit)
        ProviderRegistry(seed=packaged providers.yaml, file=
                         INVINCIBLE_PROVIDERS_FILE)   (Phase 13.5)
        Router(registry=...)  + runs recorder + failover_hook
                              (both bound after construction; the hook points
                               at ContinuityEngine.reactive_checkpoint)
        OAuthStore(engine)  MemoryStore(engine)  RetrievalService(engine)
        RunStore(engine)    SessionStore(engine)
        ContinuityEngine(engine, runs=RunStore)
        PendingActionStore(); attach_engine(engine) only when
                INVINCIBLE_PERSIST_PENDING_ACTIONS is set (opt-in persistence)
                     │
        serving               app.include_router(openai_router, deps=[require_auth])
                              app.include_router(anthropic_router, deps=[require_auth])
                              app.include_router(admin_router)  (own INVINCIBLE_ADMIN_KEY)
                              app.include_router(mcp_router, deps=[require_mcp_auth])
                              app.include_router(accounts_router)   (cookie realm)
                              app.include_router(dashboard_router)  (cookie realm, Phase 5)
                              app.include_router(oauth_router)      (no dep — own auth)
                              app.include_router(graph_router)      (admin realm)
                     │
         shutdown (lifespan)   await router.close()  (httpx client)
                               await continuity.close() / runs.close()
                               await retrieval.close() / memory.close()
                               await oauth_store.close()
                              await pending.flush_persisted()  (drain staged-action writes)
                              await engine.dispose()           (lifespan owns the engine)
```

One `httpx.AsyncClient` lives inside the `Router` and is shared by all chat
requests. All stores share **one async engine** built in the lifespan from
`INVINCIBLE_DB_URL` — there is no per-store connection and no shared SQLite
handle. Every store write is its own transaction: concurrent session
appends serialize on `SELECT … FOR UPDATE` of the session row, and JSONB
columns are bound as native objects (never pre-encoded). Schema truth is
`core/db.py`'s `metadata`; the packaged Alembic environment tracks it via
`alembic_version` (`invincible db upgrade`, verified by `doctor`). The
continuity engine renders a continuation brief (canonical task state +
checkpoints + interruption notice from runs) into every outgoing chat
prompt, and exposes the same state to MCP tools (`task_state_set/get`,
`checkpoint_create`) — one canonical store for LLMs and tools alike.

The browser realm (`/auth/*`, `/projects`, `/api-keys`, the full Phase 5
dashboard) resolves its own Principal kind (`session`): an HMAC-signed
cookie payload `v2.<uid>.<session_version>.<expiry>` whose signature and
expiry the engine-free `SessionManager` checks, then reconciled against
the live `users` row — including the per-user `session_version` column
added by migration `0006`. Every password write (`set_password` /
`change_password`) bumps that version inside its own UPDATE statement,
so any cookie minted earlier fails resolution exactly like a forged or
expired token: credential rotation retires other browsers immediately,
while the acting client is re-issued a fresh cookie by the endpoint.
No window exists where a new hash coexists with old-version sessions.

---

## 3. Chat request flow

```
client
  │  POST /v1/chat/completions  (Authorization: Bearer …)
  ▼
main.py::require_auth           401 if key set and header wrong/missing
  ▼
openai_compat::chat_completions
   │  1. stream:true → 400
   │  2. session_id = X-Session-Id or "default"
   │  3. history = session_store.load(session_id)
   │  4. injections = context_builder.build_context_messages(...)
   │     (retrieved memories + continuity brief under one budget — §4a)
   │  5. full = history + injections + body.messages
  ▼
router.route_request(full, model=body.model)   # model = soft alias hint (Phase 6)
  │  for provider in providers (sorted by tier, ascending):
  │     skip if health_tracker.is_available(provider) is False   (cooldown/disabled)
  │     skip if os.getenv(api_key_env) is missing
  │     trimmed = trim_messages(full, provider.max_context)
  │     POST {base_url}{chat_path or /chat/completions}  (timeout=resolve_timeout(provider))
  │       429/5xx        → record_failure → continue
  │       401/403        → disable        → continue
  │       other 4xx      → raise UpstreamClientError(status, body)  [abort]
  │       httpx.RequestError → record_failure → continue
  │       2xx            → record_success → return body
  │  exhausted          → raise Exception("All providers failed or are in cooldown.")
  ▼
openai_compat
  │  choices[0].message exists → session_store.save(session_id, full + [message])
  │  UpstreamClientError → forward (status, body)
  │  any other Exception → 503 gateway_error
  ▼
client
```

---

## 3a. Anthropic request flow (`/v1/messages`)

A pure compatibility layer over the same Router. The Router never knows the
client was Anthropic — it only ever receives the internal message model.

```
Claude Code
  │  HEAD /  → 200                       (base-URL probe)
  │  POST /v1/messages?beta=true
  │  anthropic-version / anthropic-beta headers (accepted, ignored)
  ▼
endpoints/auth::require_auth              dual-realm Principal (Phase 1):
                                          legacy GATEWAY_API_KEY or inv_* API
                                          key; fail-open local when unset
  ▼
anthropic_compat::anthropic_messages
  │  1. anthropic_to_internal(messages, system) → internal model
  │       system            → flattened to text, leading {role: system}
  │       text blocks       → text concatenated
  │       tool_use          → OpenAI tool_calls entry (id preserved)
  │       tool_result       → {role: "tool"} message (tool_use_id preserved)
  │       image / unknown   → skipped
  │  2. session_id = X-Session-Id or "default"
  │  3. full = session_store.load(session_id) + internal_messages
  │  4. input_tokens = estimate_token_sum(full)
  │  5. stream?  router.stream_open(full) : router.route_request(full)
  ▼
router (identical to section 3 — failover, cooldowns, trimming all apply)
  ▼
anthropic_compat
  │  non-stream: internal_to_anthropic(result, model_hint, input_tokens)
  │              → message payload (id msg_*, stop_reason, estimated usage)
  │  stream: build_stream_events() → message_start → content_block_start
  │          → content_block_delta* → content_block_stop → message_delta
  │          → message_stop; mid-stream failure → well-formed error event
  │  session save: same internal {role, content} format as OpenAI
  │  errors: mapped to Anthropic types, sanitized (never forwards upstream)
  ▼
Claude Code
```

The translation functions in `compat/anthropic.py` are pure: no FastAPI,
no Router imports. They only convert data, so a future protocol
(e.g. a raw Cursor/Open WebUI dialect) adds a compat module without
touching the core.

---

## 4. Context trimming (`router.py`)

Purpose: each provider has its own context window (`max_context`); the
router must send a conversation that fits without ever splitting a logical
unit.

### Token estimation

```python
def estimate_tokens(message):
    return max(1, len(json.dumps(message)) // 4)
```

A heuristic (~4 chars/token), explicitly not a tokenizer. Good enough to
decide what to drop; not for billing.

### Turn grouping

```python
def group_into_turns(messages):
    # a new turn begins at each "user" message
```

Non-system messages are chunked into *turns* starting at each `user`
message. This keeps an assistant `tool_calls` message glued to the `tool`
result(s) that answer it and the follow-up assistant message — the three
belong to one user turn and are never split apart.

### The trim algorithm

```python
budget = max(max_context - reserve_tokens - system_tokens, 0)   # reserve = 1000
kept = [most_recent_turn]
for turn in older turns, newest→oldest:
    if used + turn_tokens(turn) > budget: break
    keep turn
return system_messages + kept_turns flattened
```

Rules, in order of importance:

1. **All system messages are always kept.**
2. The **most recent turn is always sent** — even if it alone overflows
   budget (there's nothing better to send).
3. Older turns are kept newest-first only while they fit in
   `max_context - 1000` (reserve for the provider's response) after system
   tokens.
4. Turns are atomic: either a whole turn goes, or none of it.

Note the asymmetry: `budget` is computed once per provider, and older turns
are only skipped when they push `used` over `budget` — a single oversized
older turn can therefore "shadow" everything before it.

Because rule 1 keeps system messages unconditionally, anything injected as
a system message is invisible to trimming — which is exactly why Phase 4
routes all injections through one explicit budget (next section).

---

## 4a. Injected context & the unified budget (`context_builder.py`, Phase 4)

Two kinds of content are injected above the stored history, both rendered
as `system` messages and **never persisted**:

1. **Continuation brief** (`continuity.py::context_message`) — canonical
   task state; already self-bounded at 4096 chars.
2. **Retrieved memories** (`retrieval.py`) — lexical matches from the
   scoped `memories` table against the newest user message.

```
memories row written at persist time:
    auto:     regex triples -> "relation: target", confidence 0.6
    explicit: "remember this|that …" / "save this|that …"
              (user messages ONLY), verbatim-ish, confidence 1.0

retrieval on the next request:
    scope:   user_id + (project_id OR NULL)   — never another user/owner
    match:   generated tsvector @@ websearch_to_tsquery(query)
             AND-first; empty result retries with an OR of sanitized
             tokens (questions carry words the memory lacks)
    rank:    ts_rank x recency(14-day half-life) x kind weight x confidence
    cut:     top INVINCIBLE_MEMORY_TOP_N above INVINCIBLE_MEMORY_MIN_SCORE

budget (context_builder.assemble):
    continuity brief first (canonical beats fuzzy);
    memory block fills the remainder;
    either may be truncated to fit — total never exceeds budget.
```

The budget exists because trimming keeps system messages unconditionally:
without a shared cap, oversized injections would blow small providers'
contexts in the one way `trim_messages` cannot prevent. Default budget:
1200 tokens (~4.8k chars), `INVINCIBLE_INJECTION_BUDGET_TOKENS`.

---

## 5. Failover & health state machine (`provider_health.py`)

Per provider, two pieces of state: `consecutive_failures` and
`cooldown_until` (monotonic clock), plus the global `disabled` set.

```
record_failure(n):  failures += 1
                    cooldown = min(30 * 2**(failures-1), 300)   # 30,60,120,240,300
                    cooldown_until = now + cooldown

record_success(n):  failures = 0
                    cooldown_until = None

disable(n):         add to disabled set   (401/403 → permanent)

is_available(n):    False if disabled
                    False if now <= cooldown_until
                    True  otherwise
```

All in-memory: process restart resets cooldowns and disables. There is no
shared state between processes and no persistence.

### Reactive failover checkpoints (Phase 4)

Inside the single failover loop (`router.py::_iter_attempts`), a provider
failure fires one injected `failover_hook` per request — before the next
provider is attempted. The lifespan wires it to
`ContinuityEngine.failover_hook()`; the Router never imports continuity.
The engine snapshots every tracked task's head version as an immutable
checkpoint (`note: "auto: pre-failover snapshot (alpha failed: 429)"`) and
**no-ops when the session tracks no task state** — a checkpoint row per
failed request would be noise, and there would be nothing meaningful to
pin. Hook failures are logged and swallowed; routing never breaks.

---

## 6. MCP request flow (`endpoints/mcp.py`)

```
POST /mcp  (Authorization: Bearer <access_token>)
  │  require_mcp_auth: token validation via /oauth server
  │    (issuer, expiry, not-rotated, not-revoked, SHA-256 lookup)
  │    fail → 401 + WWW-Authenticate: Bearer resource_metadata="…"
  ▼
mcp_endpoint
  │  JSON decode fail         → -32700 Parse error, id: null
  │  body not a dict          → -32600 Invalid Request, id: null
  │  params not a dict        → -32602 Invalid params (or 204 if notification)
  │  no "id"                  → notification: side effect runs, reply 204 no body
  ▼
_dispatch(method, rpc_id, params, request)
  │  initialize   → protocolVersion 2025-06-18, capabilities.tools
  │  tools/list   → the four tool descriptors
  │  tools/call   → read_file | execute_bash | write_file | confirm_action
  │                 execute_bash/write_file: denylist, then stage a pending
  │                   action on app.state.pending_actions → token
  │                 confirm_action: approve → real action result
  │                                 deny     → {isError: true, text "Declined."}
  │                                 unknown/expired token → {isError: true,
  │                                   text "Unknown or expired confirmation token."}
  │                 ToolBlocked   → result {isError: true, text "Blocked: …"}
  │                 unknown tool  → -32601
  │  unknown method → -32601
```

Tool results are wrapped with `{"content": [{"type": "text", "text":
json.dumps(result)}], "isError": bool}` — MCP shape. The text field is
valid JSON; everything else is text.

---

## 7. Tool execution layer (`core/tool_executor.py`)

The security architecture is explicit in the module docstring:

1. **Denylist, not allowlist**, for `execute_bash` — keeps arbitrary dev
   work usable while catching high-blast-radius commands without a prompt.
2. **Path denylist** for `write_file` (and a narrower one for `read_file`).
3. **Token-based remote approval** is the *real* safety boundary — denylists
   are a fast-path to refuse the obvious, not the actual gate. A surviving
   action is staged in a `PendingActionStore` (`app.state.pending_actions`)
   under an unpredictable `secrets.token_urlsafe(16)` token and runs only
   after `confirm_action(token, approve=true)` — a second `/mcp` call.
   Tokens expire after 10 minutes and are single-use.
4. **Auth lives one layer up** (OAuth 2.1 + PKCE bearer tokens, independent
   of `GATEWAY_API_KEY`); this module assumes an authenticated caller.
   Deliberate trust-boundary change: approval is now whoever holds a valid
   bearer token, not whoever happens to be at the machine's terminal.
   Revoking the client (`invincible oauth revoke <client_id>`) cuts it off.

Execution details:

- `execute_bash`: `asyncio.create_subprocess_shell` with a **30s** timeout;
  on timeout the process is killed and a `returncode: -1` result with a
  timeout message in `stderr` is returned. stdout/stderr are decoded with
  `errors="replace"`.
- `write_file`: creates parent directories (`os.makedirs exist_ok=True`),
  writes text, returns byte count.
- `read_file`: no approval; returns content or a structured `{"status":
  "error", ...}` for missing files/directories.

Full pattern inventory and threat model:
[docs/SECURITY.md](SECURITY.md).
