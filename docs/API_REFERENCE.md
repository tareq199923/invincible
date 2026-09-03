# API Reference — Chat completions & Anthropic Messages

The OpenAI-compatible chat surface, plus the Anthropic Messages
compatibility layer. Both endpoints converge on the same Router — the
provider behind either request is chosen by the Router, never by the client.

---

## 1. Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | none | Health check → `{"status": "healthy"}` |
| `HEAD` | `/` | none | 200 OK (Claude Code base-URL probe) |
| `GET` | `/health` | none | `{"service", "status", "version"}` |
| `POST` | `/v1/chat/completions` | `Bearer <GATEWAY_API_KEY>` or `Bearer inv_…` API key | OpenAI chat completion with failover |
| `POST` | `/v1/messages` | same as above | Anthropic Messages completion with failover |
| `GET/POST/PATCH/DELETE` | `/api/v1/providers[...]` | Operator session cookie or `Bearer inv_…` (operator role) | Provider management: list, add, update, remove, enable/disable, test connectivity |
| `GET/PUT` | `/api/v1/routing` | Operator session cookie or `Bearer inv_…` (operator role) | Routing mode: `auto` / `pinned` / `chain` |
| `GET` | `/api/v1/sessions/{id}/graph` | Operator session (operator override) or user Principal (scoped) | Continuity-graph projection: runs chain, task states, checkpoints as nodes/edges/timeline |
| `POST` | `/auth/register`, `/auth/login`, `/auth/logout`, `/auth/password` · `GET` `/auth/me` | session cookie realm | Account auth + password set/change (Phases 3/5) — see §10 |
| Mixed | `/projects`, `/api-keys`, `/sessions`, `/auth/device/*`, GitHub login | cookie (own `inv_` key accepted on some) | Account management + pairing (Phase 3) — see §10 |
| `GET` | `/dashboard`, `/dashboard/sessions[/{pk}]`, `/dashboard/tasks`, `/dashboard/memory`, `/dashboard/usage`, `/dashboard/settings` | session cookie realm | Full-dashboard pages (Phase 5) — see §10 |

Auth details (dual-realm since Phase 1, resolved in this fixed order):

1. A token matching `GATEWAY_API_KEY` (timing-safe compare) authenticates
   as the system *local* owner — the single-tenant behavior.
2. Otherwise a token matching an **API key** (`inv_…`, minted via
   `invincible api-key create`) authenticates as that key's user; its
   session history is stored under that user's project.
3. If `GATEWAY_API_KEY` is **unset**, both chat endpoints are open — every
   request maps to the local owner (documented fail-open local mode).
4. Wrong/missing token with the key set → `401`.

The management surface (`/api/v1/*`) authenticates through the **operator
account realm** — the same realm as the dashboard: an operator-role
browser session cookie, or that operator's own `Bearer inv_…` API key for
terminal use. It answers **503 when `INVINCIBLE_OWNER_SECRET` is unset**
(no account sessions — fail closed), **403** for a logged-in non-operator
account, and neither chat credential is accepted there. (The former
`INVINCIBLE_ADMIN_KEY` bearer was retired: a single-operator deployment
should not carry a second top-level secret.)

---

## 2. Request

```
POST /v1/chat/completions
Authorization: Bearer <GATEWAY_API_KEY>          # required if key is set
X-Session-Id: <id>                               # optional, default "default"
Content-Type: application/json
```

### Body (`ChatRequest`)

| Field | Type | Required | Notes |
|---|---|---|---|---|
| `messages` | array of objects | **yes** | Standard OpenAI messages (`role`, `content`, optional `tool_calls`/`tool_call_id`). |
| `stream` | boolean | no | `true` → SSE stream of `chat.completion.chunk` events ending in `data: [DONE]`; `false`/absent → JSON. |
| `model` | string | no | Soft routing hint (Phase 6): matches a configured alias or exact `model_id` to prefer that provider; failover still covers the rest. Unknown names are ignored. |

Other OpenAI fields (`temperature`, `max_tokens`, …) are **not accepted** —
`ChatRequest` only defines `messages`, `stream`, and `model`, so sending
other extra fields yields `422 Unprocessable Entity` from Pydantic (strict by
default). The upstream `model` is set per-provider from `providers.yaml`,
never from the client.

### Sessions

- History is loaded from PostgreSQL keyed by `X-Session-Id` (default
  `default`), **prepended** to the request's `messages`, sent upstream.
- On a successful reply, the request's new turns plus the assistant message
  (`choices[0].message`) are **appended** to the stored history (serialized
  per session via `SELECT … FOR UPDATE` on the session row, so concurrent
  requests to one session never lose each other's turns).
- **System messages are not persisted.** Clients resend the system prompt
  on every request; storing it would accumulate duplicates that trimming
  never removes (system messages are always kept). System prompts still go
  upstream every request — only the stored history excludes them (same
  behavior as the Anthropic endpoint).
- `session_id` is a **partition key, not a credential** — since Phase 2,
  every principal's history, task state, runs, and memories are scoped to
  its own ownership triple; the same string under two principals yields
  two independent sessions.
- Trimming happens per-provider at send time; the stored history is
  untrimmed (the raw conversation accumulates in the DB).
- **Injected context (Phase 4)**: retrieved memories + the continuity
  brief are rendered as system messages under one token budget and are
  routed upstream but never persisted. Explicit "remember this…" /
  "save this…" phrases in your messages persist scoped memories for later
  retrieval; set `INVINCIBLE_MEMORY=0` to disable all of it.

---

## 3. Response

Every chat completion response (both protocols, streaming and not) carries
`x-invincible-*` headers describing the attempt that actually served it:

| Header | Meaning |
|---|---|
| `x-invincible-provider` | Registry name of the provider that served the request |
| `x-invincible-model` | The exact upstream `model_id` sent (pinned/chain may override the provider default) |
| `x-invincible-attempts` | Upstream attempts made (`1` = no failover occurred) |
| `x-invincible-request-id` | Gateway-side id correlating the failover chain across `runs` records |

Headers are absent on gateway error paths where no attempt reached a
provider. Each attempt is also recorded in the `runs` table (session
database): the queryable answer to "which model handled what, and why did
it move".

**Success (`200`)**: the upstream provider's JSON is forwarded **verbatim**
— no normalization. Shape is standard OpenAI:

```json
{
  "id": "cmpl-...",
  "model": "gemini-2.5-flash",
  "choices": [
    {"message": {"role": "assistant", "content": "Hello!"}}
  ]
}
```

Session persistence only happens if `choices[0].message` exists.

---

## 4. Status codes & error semantics

| Status | When | Body shape |
|---|---|---|
| `200` | Upstream success | Upstream body verbatim, or SSE stream (`stream: true`) |
| `401` | Missing/invalid `GATEWAY_API_KEY` | `{"detail": {"error": {"message": "...", "type": "auth_error"}}}` (FastAPI HTTPException) |
| `422` | Body fails Pydantic validation (missing `messages`, extra fields) | FastAPI validation detail |
| `4xx` (forwarded) | Upstream returned a non-failover error (see below) | **Upstream's own error body**, status copied |
| `503` | All providers failed / in cooldown, or an unexpected exception | `{"error": {"message": "All providers failed or are in cooldown.", "type": "gateway_error"}}` |

### Failover semantics (per upstream response)

The router (`invincible/core/router.py::route_request`) tries providers in
`tier` ascending order. Per attempt:

| Upstream status | Router behavior |
|---|---|
| `200` | `record_success` (resets cooldown), return body |
| `429` or `5xx` | `record_failure` → cooldown → **try next provider** |
| `401` / `403` | `disable` (permanent for process lifetime) → **try next provider** |
| Other `4xx` (e.g. `400`) | **Abort immediately** — raise `UpstreamClientError`, which the endpoint forwards with the provider's status and body. No failover. |
| Network error (`httpx.RequestError`) | `record_failure` → cooldown → **try next provider** |
| Provider in cooldown / no API key | Skipped silently (log only) |

Exhausted all providers (including all in cooldown) → the `503` above.

### Cooldown curve

`record_failure` sets `cooldown_until = now + min(30 * 2**(failures-1), 300)`:
**30s → 60s → 120s → 240s → 300s (cap).** `record_success` resets the
counter and clears the cooldown. `disable` (401/403) blocks the provider
forever — both cooldowns and disables are **in-memory only** and reset on
process restart.

---

## 5. Context trimming (per provider)

Before each upstream call the conversation is trimmed to the provider's
`max_context` (default `32000` tokens):

- All `system` messages are always kept.
- The remaining messages are grouped into **turns** (a new turn starts at
  each `user` message) and dropped oldest-first as atomic units, so an
  assistant `tool_calls` is never separated from its tool results.
- A `1000`-token reserve is subtracted for the provider's response.
- The most recent turn is **always** sent, even if it alone exceeds the
  budget.
- Token estimation is a heuristic: `len(json.dumps(message)) // 4`
  (~4 chars/token).

Full detail: [docs/ARCHITECTURE.md](ARCHITECTURE.md) → *Context trimming*.

---

## 6. Timeouts

Per-provider split timeouts (defaults `connect 5s / read 60s / write 5s /
pool 2s`, overridable in `providers.yaml`). The shipped config gives Gemini
`90s`, Groq `45s`, and the OpenRouter fallback `20s` reads. See
[docs/CONFIGURATION.md](CONFIGURATION.md).

---

## 7. Anthropic Messages (`POST /v1/messages`)

An additive compatibility layer: translates Anthropic requests into the
same internal message model the OpenAI endpoint uses, calls the **same
Router** (provider selection, failover, cooldowns, trimming, sessions all
apply unchanged), and translates the response back to Anthropic format.

```
POST /v1/messages?beta=true       # the ?beta=true is ignored
Authorization: Bearer <GATEWAY_API_KEY>
anthropic-version: 2023-06-01      # accepted, not required
anthropic-beta: ...                # accepted, ignored
X-Session-Id: <id>                 # optional, default "default"
Content-Type: application/json
```

### Declared fields

| Field | Type | Notes |
|---|---|---|
| `messages` | array | **Required.** `role` (`user`/`assistant`) + `content` (string or content blocks). See flattening below. |
| `model` | string, optional | **Client hint only.** Echoed in the response; never affects routing and never requires the provider to expose Claude model names. |
| `system` | string \| [blocks], optional | Becomes a leading `system` message (always kept by trimming). |
| `max_tokens` | int, optional | Accepted; the Router controls the upstream output budget per provider. |
| `stream` | bool, optional | `true` → Anthropic SSE events; otherwise JSON. |

All other Anthropic request fields — `tools`, `tool_choice`, `metadata`,
`temperature`, `top_p`, `top_k`, `stop_sequences`, and any unknown field —
are **accepted and ignored** (`extra="ignore"`). Claude Code never receives
a `422` for optional features Invincible doesn't implement.

### Content-block flattening

Flattening to plain text only applies to `system` content (it has no
structured equivalent downstream):

| Block type | Result |
|---|---|
| `text` | text concatenated |
| `tool_use` | `[tool_use: <name>]` placeholder tag |
| `tool_result` | its text (string or nested text blocks) |
| `image` / unknown | skipped |

For `user`/`assistant` messages the blocks are preserved structurally
instead: `tool_use` blocks become OpenAI-shaped `tool_calls` entries (the
id is kept verbatim) and `tool_result` blocks become `role: "tool"`
messages keyed by `tool_use_id` — nothing is flattened. The response
re-emits them as real `tool_use` content blocks (non-streaming and SSE
alike) with `stop_reason: "tool_use"`. `image` blocks are skipped in all
cases.

### Non-streaming response

```json
{
  "id": "msg_...",
  "type": "message",
  "role": "assistant",
  "model": "claude-sonnet-4",
  "content": [{"type": "text", "text": "Hello!"}],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {"input_tokens": 12, "output_tokens": 3}
}
```

- `stop_reason` maps from OpenAI `finish_reason`: `stop`→`end_turn`,
  `length`→`max_tokens`, `tool_calls`→`tool_use` (see
  `translate_finish_reason`), unknown/absent→`end_turn`.
- `usage` token counts are **estimates** (the Router's own `estimate_tokens`
  heuristic) because upstream streaming responses rarely report usage.

### Streaming (`stream: true`)

Anthropic SSE events, one per frame (`event: <name>\ndata: <json>\n\n`),
in canonical order:

```
message_start → content_block_start → content_block_delta (× text deltas)
→ content_block_stop → message_delta → message_stop
```

- `message_start.message.content` is `[]` and carries the estimated usage.
- Each `content_block_delta` carries `{"type": "text_delta", "text": …}`.
- `message_delta` carries the final `stop_reason` and `output_tokens`.
- A **mid-stream upstream failure** emits a well-formed Anthropic `error`
  event and closes cleanly — no malformed SSE, no `message_stop`.

### Sessions & cross-protocol sharing

`X-Session-Id` works identically and history is stored in the shared internal
format (`{"role", "content"}`), so an OpenAI client and Claude Code on the
same session id see the same conversation. The streamed reply is
reconstructed from deltas and persisted once the stream completes.

### Error translation

Router errors become Anthropic-shaped, sanitized errors
(`{"type": "error", "error": {"type": …, "message": …}}`):

| Status | Anthropic type |
|---|---|
| `400` | `invalid_request_error` |
| `401` | `authentication_error` |
| `403` | `permission_error` |
| `404` | `not_found_error` |
| `429` | `rate_limit_error` |
| `500` | `api_error` |
| `503` | `overloaded_error` |

`422` is reserved for Pydantic structural validation (e.g. missing
`messages`). Upstream provider error bodies are **never** forwarded verbatim.
The router's failover/trimming semantics in sections 4–5 apply unchanged.

---

## 9. Continuity graph (`GET /api/v1/sessions/{id}/graph`, Phase 15c)

A pure PROJECTION over the authoritative stores - runs, task states,
checkpoints, turns. The graph owns nothing and is never a source of truth.

Response shape:

- `nodes[]` - `kind`: `session` | `run` | `task_state` | `checkpoint` |
  `turn`. Run nodes carry provider/model/outcome/error class/attempt index;
  state nodes carry version/status/payload/actor.
- `edges[]` - `failover_from` (same request_id, consecutive attempts:
  "why did work move from A to B"), `followed_by` (different requests),
  `supersedes` (state v(n)->v(n+1)), `pins` (checkpoint->state version),
  `attempted_for` / `canonical_for` / `contains` (session anchors).
- `timeline[]` - node ids ordered by timestamp.
- `summary` - providers used, attempt/failover counts, latest task
  versions/payloads, and the current interruption note ("previous attempt
  ended unexpectedly on provider X ...").

Query: `?limit=N` caps how many runs/state versions are projected
(default 200, max 1000).

---

## 10. Account surface (`/auth/*`, `/projects`, `/api-keys`, `/sessions`)

Phase 3. Browser realm: HMAC-signed HttpOnly session cookies; JSON bodies
for scripts, urlencoded form posts for the built-in pages (form posts get
redirects / rendered errors). Management endpoints also accept the caller's
own `inv_` API key — MCP bearers and `GATEWAY_API_KEY` never work here.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/auth/register` | none | `{email, password}` → 201 + session cookie (409 duplicate email, 400 validation) |
| `POST` | `/auth/login` | none | Enumeration-safe login; persistent per-IP lockout (429, scope `auth-login`) |
| `POST` | `/auth/logout` | cookie | Clears the session cookie |
| `GET` | `/auth/me` | cookie | `{id, email, kind, project_id}` |
| `POST` | `/auth/password` | cookie | Set first password or change existing (min 8 chars); bumps `session_version` so all other browser cookies stop working — details below |
| `GET/POST` | `/projects` | cookie | List (archived hidden unless `?include_archived=true`) / create |
| `PATCH` | `/projects/{id}` | cookie (owner) | Rename |
| `POST` | `/projects/{id}/archive` | cookie (owner) | Soft archive (default project refused) |
| `GET/POST` | `/api-keys` | cookie or own `inv_` key | List (prefix only) / create (raw shown once) |
| `DELETE` | `/api-keys/{id}` | cookie or own `inv_` key | Revoke (owner-scoped, idempotent) |
| `GET` | `/sessions` | cookie | Read-only listing of the caller's sessions |
| `GET` | `/login`, `/register`, `/account` | page | Jinja2 UI pages |

Device pairing (RFC 8628-flavored):

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/auth/device/code` | none | → `{device_code, user_code, verification_uri, expires_in, interval}` |
| `GET` | `/auth/devices/{user_code}` | cookie | Approval page (Approve/Deny forms) |
| `POST` | `/auth/devices/{user_code}/approve\|deny` | cookie | Bind/deny the request |
| `POST` | `/auth/device/token` | none | Poll: `authorization_pending` / `slow_down` / `access_denied` / `expired_token`; on success returns the minted API key **once** |

GitHub login (enabled when `INVINCIBLE_GITHUB_CLIENT_ID` +
`INVINCIBLE_GITHUB_CLIENT_SECRET` are set):

- `GET /auth/github/login` → 302 to GitHub with a signed single-use
  `state`; callback at `GET /auth/github/callback` verifies state,
  exchanges the code, resolves the VERIFIED primary email, then logs in /
  auto-links / auto-registers and sets the session cookie. Failures bounce
  to `/login?github_error=1`.

### Password set/change (`POST /auth/password`, Phase 5)

Cookie realm only — a valid `inv_*` API key does NOT authorize this
endpoint (`require_user_session` reads the signed session cookie alone).
Body `{"new_password": …, "current_password": …}`, accepted as JSON or a
urlencoded form post (JSON gets status codes + bodies; forms get 303
redirects). Which flow applies follows the **stored** account state,
never caller-chosen fields — omitting `current_password` cannot flip
semantics:

- No stored password (GitHub-only account) → **set**: only
  `new_password` is required (minimum 8 characters). The NULL-hash
  predicate is the atomic guard: two racing setters resolve to one
  winner; the loser gets `password_exists` (409) and never overwrites.
- Stored password → **change**: `current_password` must verify
  (`wrong_password`, otherwise indistinguishable semantics); weak
  replacement → `weak_password`.

Both paths execute the hash replacement and the per-user
`users.session_version + 1` bump **inside one UPDATE** — there is no
window where a fresh hash coexists with old-version sessions. Cookies
are payloads `v2.<uid>.<session_version>.<expiry>` (HMAC-SHA256), and
resolution rejects any cookie whose embedded version differs from the
live column value exactly like a forged or expired token. Net effect:
every browser session minted before the change dies immediately instead
of surviving its 30-day TTL; the acting client receives a fresh cookie
in the response and keeps its login. `inv_*` API keys and other users'
sessions are unaffected. Both actions are audit-written
(`password.set` / `password.changed`); the column ships in migration
`0006`. HTML errors bounce to `/dashboard/settings?pw_error=<code>`,
rendered from a fixed message map — query strings are never echoed
verbatim.

### Dashboard views (Phase 5)

Server-rendered Jinja2 + HTMX pages on the same cookie realm:

| Path | Purpose |
|---|---|
| `/dashboard` | Overview: owned count cards (projects, sessions, active keys, task heads, memories, 7-day tokens) + 10 recent sessions |
| `/dashboard/sessions` | Owned sessions index |
| `/dashboard/sessions/{session_pk}` | Continuity-projection detail: runs chain, failover pairs, checkpoints, task states, activity |
| `/dashboard/tasks` | Cross-session active task heads |
| `/dashboard/memory` | Browse/filter/paginate/search owned memories, explicit create, audited delete buttons |
| `/dashboard/usage?days=N` | Day bars + per-provider totals; buckets pinned to UTC, window clamped to 1–90 days |
| `/dashboard/settings` | System flags, read-only provider/routing panel, password forms |

HTMX interactions drive three JSON siblings on the same realm:
`GET /memories`, `POST /memories` (explicit layer, confidence 1.0,
content ≤2000 chars — refused entirely while `INVINCIBLE_MEMORY=0`,
though browse/delete stay available so the toggle never traps data),
`DELETE /memories/{id}` (audited owner-scoped; HTMX requests get an
empty 204 so `hx-swap="delete"` drops the row), and `GET /usage?days=`.
Foreign ids are indistinguishable from unknown ones on every dashboard
surface — identical 404 bodies/pages, no existence leak.
