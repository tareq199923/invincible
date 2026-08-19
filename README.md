# Invincible — AI Continuity Gateway & Local MCP Agent

> The Python package is named `invincible` (the repository directory is
> `ai-gateway`). Throughout this repo the project is referred to as
> **Invincible**.

---

## What is Invincible?

A local, Python (FastAPI) server that runs on your development machine and
serves two roles in one process:

1. **Local Failover Proxy** — an OpenAI-compatible `/v1/chat/completions`
   endpoint that fans requests across tiered upstream providers (NVIDIA NIM,
   Groq, OpenRouter, Gemini) and transparently fails over on rate limits (429)
   and server errors, so a free-tier 429 no longer kills an agent's workflow.
   It also speaks the **Anthropic Messages API** (`POST /v1/messages`), so
   Claude Code and other Anthropic-native clients plug in with a one-line
   config change.
2. **Local MCP Tool Server** — a JSON-RPC 2.0 `/mcp` endpoint exposing
   `read_file`, `execute_bash`, and `write_file` to a cloud-hosted AI that
   reaches your machine through a tunnel, letting it read local files, write
   code, and run commands on your box.

### Why it exists

- **The 429 problem.** AI coding agents using free/open-source providers
  (NVIDIA NIM, Groq, OpenRouter, Gemini) get killed when they hit a rate
  limit. Invincible
  sits between the agent and the providers; on a 429 (or 5xx) it records the
  failure, puts the provider in a short cooldown, and retries the next
  provider in the tier order. The agent sees a single, stable endpoint.
- **The cloud-to-local gap.** Cloud AI tools (e.g. the Claude web/mobile app)
  can reason well but cannot read your local files, write to disk, or run
  terminal commands. Invincible's MCP server exposes those capabilities over
  HTTP, so a remote model can act on the local machine — under operator
  confirmation for anything destructive.

---

## Features

| Feature | What it gives you |
|---|---|
| **Tiered failover** | Providers sorted by `tier`, tried in order; 429/5xx → cooldown + next tier; 401/403 → permanent disable; network errors → next tier. All providers down → HTTP 503. |
| **Exponential cooldown** | 30s → 60s → 120s → 240s → capped at 300s; a success resets the counter (in-memory, process-scoped). |
| **Conversation memory** | SQLite-backed, keyed by the `X-Session-Id` header (default `default`). History is merged into every request and the assistant reply is persisted back. |
| **Context trimming** | Per-provider `max_context`; system messages always kept; everything else dropped as atomic *turns* (an assistant `tool_calls` is never separated from its tool results); the most recent turn is always sent. |
| **Per-provider timeouts** | Split connect/read/write/pool with sane defaults and per-provider overrides (NIM, Gemini, and the OpenRouter fallback get 90s reads; Groq 45s). |
| **MCP tool server** | `read_file` (no approval), `execute_bash` and `write_file` (staged, then approved via a token round-trip through the `confirm_action` tool), guarded by denylists and an **OAuth 2.1 + PKCE bearer-token** auth layer (browser owner-login + per-client consent, tokens don't survive on requests like a shared header does). |
| **Protocol-agnostic** | Native **OpenAI** and **Anthropic** protocols, both translated into one internal message model. Claude Code works with `ANTHROPIC_BASE_URL` pointing at the gateway. |

---

## Installation

Requires Python 3.10+.

```bash
python -m venv venv
source venv/bin/activate            # or venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Optional: install the package itself so the `invincible` / `inv` commands
work from anywhere:

```bash
pip install -e .
invincible --version                 # verify
```

---

## Quick Start

```bash
cp .env.example .env                # then fill in API keys

invincible setup                    # generates missing secrets, prompts for keys
invincible start                    # http://127.0.0.1:8000
```

`invincible setup` writes missing secret values (`GATEWAY_API_KEY`,
`INVINCIBLE_OWNER_SECRET`) as random `secrets.token_urlsafe(32)` tokens and
prompts for the provider keys, preserving your existing `.env` comments and
values. `INVINCIBLE_OWNER_SECRET` is the one-time browser login for
approving MCP connections — not something `/mcp` requests send anymore.

See [Examples](#examples) for ready-to-run `curl` calls, or continue reading
for the full configuration, API, and tooling reference.

---

## Configuration

Everything is environment variables plus one YAML file — no other config.

### `.env` variables

| Variable | Required by | Purpose |
|---|---|---|
| `GATEWAY_API_KEY` | `/v1/*` | Bearer token for the chat endpoint. **If unset, the endpoint is open (no auth).** |
| `INVINCIBLE_OWNER_SECRET` | `/oauth/authorize` | One-time **browser login** to approve MCP connections (kept in a 30-day signed session cookie). **Not** sent on `/mcp` — requests use short-lived OAuth Bearer tokens. **If unset, no new MCP grants can be approved.** The legacy `MCP_SHARED_SECRET` key is still read as a fallback. |
| `NVIDIA_API_KEY` | provider tier 1 | NVIDIA NIM hosted: GLM-5.2 (Z.ai); strongest coding/agentic tier. |
| `GROQ_API_KEY` | provider tier 2 | Groq Llama 70B. |
| `OPENROUTER_API_KEY` | provider tier 3 | OpenRouter free fallback. |
| `GEMINI_API_KEY` | provider tier 4 | Gemini Flash — last resort. |
| `INVINCIBLE_CONFIG_PATH` | startup | Path to a custom `providers.yaml` (set by CLI `--config`). |
| `INVINCIBLE_DB_PATH` | startup | Path to the session database (set by CLI `--db-path`). |
| `INVINCIBLE_PERSIST_PENDING_ACTIONS` | startup | **Opt-in**: when set, staged `execute_bash`/`write_file` approvals are written to the session database and survive a server restart. **Off by default** — pending actions are memory-only and a restart orphans them (clean slate). |

The two secrets are **independent**: a leaked tunnel URL alone is not enough
to reach tool execution, and rotating one secret never affects the other.
Rotating `INVINCIBLE_OWNER_SECRET` does **not** kill existing MCP grants —
use `invincible oauth revoke <client_id>` for that.

### `providers.yaml`

Defines the upstream providers: `tier` (failover order, ascending), `base_url`
(OpenAI-compatible), `api_key_env` (env var *name*, never the key itself),
`model_id`, optional `aliases` (soft routing hints — request `model: fast` to
prefer Groq), `max_context`, and optional per-provider `timeout` overrides.
The canonical copy is packaged at `invincible/providers.yaml` (a deprecated
copy at the repo root is only a fallback).

Full reference — schema, validation rules, timeout resolution:
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).
How to add a provider, aliases, and supported shapes:
[docs/PROVIDERS.md](docs/PROVIDERS.md).

---

## CLI Commands

Two commands, both exposed as `invincible` and `inv`:

| Command | Purpose |
|---|---|
| `invincible setup` | Create/update `.env`: generates missing secrets (`token_urlsafe(32)`, never echoed), prompts for provider keys, preserves existing comments/values; carries a legacy `MCP_SHARED_SECRET` over to `INVINCIBLE_OWNER_SECRET` automatically. `--force` re-prompts existing values. |
| `invincible secret rotate` | Generate a brand-new `INVINCIBLE_OWNER_SECRET` and rewrite it in place — no manual `.env` editing, never echoes the value (unless `--show`). Preserves every other line; migrates a legacy `MCP_SHARED_SECRET` key away. Does **not** revoke already-issued OAuth grants (that's `invincible oauth revoke`). |
| `invincible start` | Start the server **and** a Cloudflare tunnel (named `invincible` by default) so the gateway is reachable remotely. Options: `--host` (default `127.0.0.1`), `--port` (default `8000`), `--reload`, `--log-level`, `--env-file`, `--config` (custom providers.yaml), `--db-path` (session database), `--tunnel/--no-tunnel`, `--tunnel-name` (or `INVINCIBLE_TUNNEL_NAME`). The tunnel is shut down with the server (Ctrl+C or a crash); a dead tunnel is reported as soon as it exits. |
| `invincible doctor` | Environment/config diagnostics, including the owner-secret presence. |
| `invincible oauth list` | Show registered OAuth clients, their redirect URIs, and active/revoked grants. |
| `invincible oauth revoke <client_id>` | Revoke every access/refresh token for a client immediately. |
| `invincible oauth test-client` | Headless helper: registers a client, approves it, and prints a ready-to-use Bearer token + curl for `/mcp` (no browser needed). |

```bash
invincible setup --force
invincible secret rotate            # new owner secret, in place
invincible secret rotate --show     # ...and print it (rarely needed)
invincible start --port 9000 --config ./my-providers.yaml
```

Full CLI reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md) → *CLI reference*.

---

## API

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | none | Health check → `{"status": "healthy"}` |
| `HEAD` | `/` | none | 200 OK — Claude Code's base-URL probe |
| `GET` | `/health` | none | Service detail → `{"service", "status", "version"}` |
| `GET` | `/v1/models` | `Authorization: Bearer <GATEWAY_API_KEY>` | OpenAI-compatible model list from `providers.yaml` |
| `POST` | `/v1/chat/completions` | `Authorization: Bearer <GATEWAY_API_KEY>` | OpenAI chat completion with tiered failover |
| `POST` | `/v1/messages` | `Authorization: Bearer <GATEWAY_API_KEY>` | Anthropic Messages API with tiered failover |

### Chat request

- **Body**: `{"messages": [...], "stream": false}` — OpenAI message format.
  `messages`, `stream`, and `model` are accepted. `model` is a soft routing
  hint (matches a configured alias or exact `model_id`; unknown names are
  ignored). Other OpenAI fields are rejected with **422**.
- **Streaming**: `stream: true` returns an OpenAI-compatible
  Server-Sent Events (`text/event-stream`) response. Each event is a
  `chat.completion.chunk` (`data: {…}\n\n`), and the stream ends with
  `data: [DONE]`. Chunks are forwarded from the upstream provider as they
  arrive — nothing is buffered. Providers that fail before the first chunk
  trigger the normal failover; an error after streaming has begun terminates
  the stream with a well-formed `data: {"error": …}` event.
- **Sessions**: history is loaded from SQLite keyed by the `X-Session-Id`
  header (default `default`), prepended to your messages, and the assistant
  reply is persisted back. `session_id` is a partition key, not a credential.
  For streamed responses the reply is reconstructed from the chunk deltas and
  saved once the stream completes.
- **Response**: the upstream provider's JSON is forwarded **verbatim**
  (non-streaming).

### Anthropic Messages (`POST /v1/messages`)

Invincible also speaks the **Anthropic Messages API**, so Claude Code and
other Anthropic-native clients work without modification:

```bash
# .env for Claude Code (or your shell):
ANTHROPIC_BASE_URL=http://127.0.0.1:8000
```

Claude Code probes `HEAD /`, then calls `POST /v1/messages?beta=true` — both
are served. Supported request fields: `model`, `system`, `messages`,
`max_tokens`, `stream`. Everything else Claude Code sends (`tools`,
`tool_choice`, `metadata`, `temperature`, `top_p`, `top_k`,
`stop_sequences`, unknown fields, `anthropic-beta` / `anthropic-version`
headers, the `?beta=true` query) is **accepted and ignored** — never a 422.

The `model` field is treated as a **client hint**: it is echoed back in
the response, and if it matches a configured alias (or an exact provider
`model_id`) the matching provider is *preferred* — the Router still fails
over through the rest of the tier order if that provider is down. An
unknown model name (like Claude Code's own model ids) changes nothing.
The upstream model always comes from `providers.yaml`.

- **Streaming**: `stream: true` returns Anthropic SSE events in the
  canonical order — `message_start` → `content_block_start` →
  `content_block_delta` (one per text delta) → `content_block_stop` →
  `message_delta` → `message_stop`. `tool_use` content blocks are preserved
  and streamed as structured events (`content_block_start` +
  `input_json_delta` frames) with `stop_reason: "tool_use"` at the end;
  `tool_result` blocks in the request are carried as `role: "tool"`
  messages, so tool-shaped conversations round-trip losslessly (ids
  preserved). `image` content blocks are still skipped. A mid-stream
  upstream failure emits a well-formed Anthropic `error` event and closes —
  never malformed SSE.
- **Sessions**: the same `X-Session-Id` header and SQLite store are used,
  and history is serialized in the shared internal format — an OpenAI
  client and a Claude Code session on the same id see the same
  conversation.
- **Errors**: mapped to Anthropic error types (`invalid_request_error`,
  `authentication_error`, `permission_error`, `not_found_error`,
  `rate_limit_error`, `api_error`, `overloaded_error`) with sanitized
  messages; upstream provider bodies are never forwarded.
- **Unsupported today**: image content (skipped during conversion).

### Status codes

| Status | When |
|---|---|
| `200` | Upstream success — JSON body forwarded verbatim, or SSE stream (`stream: true`) |
| `401` | Missing/invalid `GATEWAY_API_KEY` (when set) |
| `422` | Body fails validation (missing `messages`, extra fields) |
| `4xx` | Upstream returned a non-failover error (e.g. 400) — forwarded verbatim |
| `503` | All providers failed or are in cooldown (before streaming starts) |

The Anthropic endpoint uses the same statuses; error bodies are Anthropic
shaped (`{"type": "error", "error": {"type": …, "message": …}}`) and map to
Anthropic error types.

Full contract — sessions, trimming, timeout semantics:
[docs/API_REFERENCE.md](docs/API_REFERENCE.md).

---

## MCP Support

`POST /mcp` implements a minimal JSON-RPC 2.0 subset: `initialize`,
`tools/list`, and `tools/call`. Protocol version: `2025-06-18`.

- **Auth**: OAuth 2.1 + PKCE via the built-in authorization server. Clients
  discover it at `/.well-known/oauth-protected-resource` (RFC 9728), register
  at `/oauth/register`, get owner approval on the `/oauth/authorize` consent
  page, then send `Authorization: Bearer <access_token>` on every `/mcp`
  request. Wrong/missing/expired/revoked token → `401` with a
  `WWW-Authenticate: Bearer resource_metadata="…"` challenge. (No
  `X-MCP-Secret` header anymore; the legacy `MCP_SHARED_SECRET` env var is
  only read as a fallback for the owner login.)
- **Notifications**: a request without an `id` still runs its side effect
  but the server replies `204 No Content` with no body.

### Tools

| Tool | Arguments | Confirmation | Gate |
|---|---|---|---|
| `read_file` | `path` | **No** | Blocks only real secrets/state: `.env*`, `sessions.db`, `.git/`. **Allows** `invincible/`, `tests/`, `providers.yaml`. |
| `execute_bash` | `command` + a `confirm_action` token round-trip | **Yes** — staged with a token; runs only after `confirm_action(token, approve=true)` (30s execution timeout) | Blocks high-blast-radius commands (`rm -rf /`, fork bombs, `dd of=/dev/`, `mkfs`, `sudo`, `curl \| sh`, `rd /s C:\`, …). |
| `write_file` | `path`, `content` + a `confirm_action` token round-trip | **Yes** — staged with a token; writes only after `confirm_action(token, approve=true)` | Blocks writes to `.env*`, `providers.yaml`, `sessions.db`, `invincible/`, `tests/`, `.git/`. Creates parent directories. |
| `confirm_action` | `token`, `approve` | — | Approves/denies a pending `execute_bash`/`write_file`; token is single-use and expires after 10 minutes. |

Security model, full denylist inventory, and known limits:
[docs/SECURITY.md](docs/SECURITY.md).

---

## Provider Routing

Providers are tried in **`tier` ascending order** (1 first). Per attempt:

| Upstream status | Router behavior |
|---|---|
| `200` | `record_success` (resets cooldown) → return body |
| `429` / `5xx` | `record_failure` → cooldown → **try next provider** |
| `401` / `403` | `disable` (permanent for process lifetime) → **try next provider** |
| Other `4xx` (e.g. `400`) | **Abort** — forward the provider's status and body |
| Network error | `record_failure` → cooldown → **try next provider** |
| In cooldown / missing API key | Skipped silently (log only) |

All providers exhausted → **HTTP 503**. Cooldowns follow
`30 * 2**(failures-1)`, capped at **300s**; all health state is in-memory and
resets on restart.

Shipped tier order (aliases are soft routing hints — `model: fast` prefers
Groq, and failover still covers every other provider):

| Tier | Provider | Model | Max context | Alias |
|---|---|---|---|---|
| 1 | `nim-glm` | `z-ai/glm-5.2` | 1 000 000 | `strong` |
| 2 | `groq-llama` | `openai/gpt-oss-120b` | 128 000 | `fast` |
| 3 | `openrouter-fallback` | `nvidia/nemotron-3-ultra-550b-a55b:free` | 1 000 000 | `free` |
| 4 | `gemini-flash` | `gemini-2.5-flash` | 1 000 000 | `backup` |

Deep dive (failover state machine, context trimming): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Examples

### 1. Health check

```bash
curl http://127.0.0.1:8000/
# {"status": "healthy"}
```

### 2. List models

```bash
curl http://127.0.0.1:8000/v1/models
# {
#   "object": "list",
#   "data": [
#     {"id": "z-ai/glm-5.2", "object": "model", "owned_by": "invincible"},
#     {"id": "openai/gpt-oss-120b", "object": "model", "owned_by": "invincible"},
#     {"id": "nvidia/nemotron-3-ultra-550b-a55b:free", "object": "model", "owned_by": "invincible"},
#     {"id": "gemini-2.5-flash", "object": "model", "owned_by": "invincible"},
#     {"id": "strong", "object": "model", "owned_by": "invincible"},
#     {"id": "fast", "object": "model", "owned_by": "invincible"},
#     {"id": "free", "object": "model", "owned_by": "invincible"},
#     {"id": "backup", "object": "model", "owned_by": "invincible"}
#   ]
# }
```

The list is built from the running gateway's provider configuration, so it
reflects exactly what the gateway can route to. Real model ids are listed
first, then the configured aliases.

### 3. Chat with session memory

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: my-conversation" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}]}'
```

The assistant reply is stored under `my-conversation` and will be included in
your next request with the same `X-Session-Id` — the model remembers the
conversation.

### 4. Stream a chat (SSE)

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}], "stream": true}'
```

`-N` (aka `--no-buffer`) prints each event as it arrives. Tokens are streamed
as OpenAI-compatible `chat.completion.chunk` events:

```json
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1783161600,"model":"gemini-2.5-flash","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1783161600,"model":"gemini-2.5-flash","choices":[{"index":0,"delta":{"content":"!"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1783161600,"model":"gemini-2.5-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

### 5. Use it from Claude Code (Anthropic)

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8000 claude
```

Claude Code probes `HEAD /`, then calls `POST /v1/messages` with streaming.
You can send the same call directly:

```bash
curl http://127.0.0.1:8000/v1/messages \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4","max_tokens":1024,
       "messages":[{"role":"user","content":"Hello!"}]}'
```

And stream it (`stream: true`) to receive Anthropic SSE events ending in
`message_stop`.

### 6. List MCP tools

First get an MCP access token (one-time browser consent, or the headless
helper):

```bash
invincible oauth test-client   # outputs a Bearer token + curl command
export ACCESS_TOKEN=...
```

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### 7. Run a command via MCP

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"execute_bash","arguments":{"command":"git status"}}}'
```

The call returns a `pending_confirmation` result carrying a token. To
approve it, call `confirm_action` with that token (or deny with
`approve: false`):

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"confirm_action",
                 "arguments":{"token":"<token from step 2>","approve":true}}}'
```

The command runs (or the file is written) only after approval; the token is
single-use and expires after 10 minutes.

### 8. Expose to a cloud AI over a tunnel

`invincible start` brings the tunnel up automatically: it runs
`cloudflared tunnel run <name>` (name defaults to `invincible`, override
with `--tunnel-name` or `INVINCIBLE_TUNNEL_NAME`) alongside the server and
shuts it down again when the server stops. cloudflared's log lines (and the
public URL when cloudflared prints one) appear prefixed `[tunnel]`; a
tunnel that dies is reported as soon as it exits. To skip the tunnel, pass
`--no-tunnel`.

For a one-off quick tunnel instead (no named-tunnel config required):

```bash
cloudflared tunnel --url http://127.0.0.1:8000
# → https://random-name.trycloudflare.com  — call /mcp on this URL
```

The tunnel URL alone is useless without an access token — and any valid
token can be revoked immediately with `invincible oauth revoke <client_id>`.

More MCP protocol details: [docs/MCP_PROTOCOL.md](docs/MCP_PROTOCOL.md).

---

## Architecture

```
                         ┌──────────────────────────────────┐
  OpenAI-compatible      │  invincible/main.py              │
  agent  ─── /v1/chat ─► │  (FastAPI)                        │
         Claude Code     │                        compat/    │
  (Anthropic) ─ /v1/msg ►│  openai_compat ──────► anthropic │
                         │  │ mcp routers │                 │
  Cloud AI      ─── /mcp ─►  │ core/router.py               │
  (via tunnel)           │  │ core/tool_executor (denylist) │
                         └──────┬──────────────┬────────────┘
                                │              │
                  ┌─────────────▼──┐   ┌───────▼────────────┐
                  │ core/router.py │   │ core/tool_executor │
                  │ tiered failover│   │ (denylist + approval)│
                  │ + ctx trimming │   └─────────────────────┘
                  └───────┬────────┘
                          │
            ┌─────────────▼──────────────┐
            │ core/provider_health.py   │
            │ core/session_store.py     │
            │ (SQLite conversation mem) │
            └────────────────────────────┘
```

The compatibility layers (OpenAI and Anthropic) only translate; both
produce the same internal message model, which is what the Router, session
store, and trimming logic consume.

### Package layout

| Path | Role |
|---|---|
| `invincible/main.py` | FastAPI app, lifespan, auth dependencies, router wiring, `HEAD /` and `/health`. |
| `invincible/endpoints/openai_compat.py` | `POST /v1/chat/completions` (JSON + SSE streaming, session merge + upstream call); `GET /v1/models`. |
| `invincible/endpoints/anthropic_compat.py` | `POST /v1/messages`; translates Anthropic ↔ internal model, calls the same Router. |
| `invincible/models/anthropic.py` | Pydantic request model: only real fields declared; everything else ignored. |
| `invincible/compat/common.py` | Protocol-neutral internal-message/usage helpers shared by compat layers. |
| `invincible/compat/anthropic.py` | Pure Anthropic translators: flattening, finish-reason map, error map, Anthropic SSE streaming. |
| `invincible/endpoints/mcp.py` | `POST /mcp`; JSON-RPC 2.0 dispatch, `tools/list`, `tools/call`. |
| `invincible/core/router.py` | Provider loading, tiered failover, response trimming, timeouts. |
| `invincible/core/provider_health.py` | Per-provider failure counts + exponential cooldowns. |
| `invincible/core/session_store.py` | SQLite-backed conversation memory, partitioned by session id. |
| `invincible/core/tool_executor.py` | Denylists, pending-action approval (`confirm_action`), tool execution. |
| `invincible/cli.py` | Click CLI: `setup` (env file wizard) and `start` (uvicorn wrapper). |
| `invincible/providers.yaml` | Canonical provider configuration (packaged, authoritative). |

---

## Documentation

| Doc | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, request flows, context-trimming deep dive, failover state machine. |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | The `/v1/chat/completions` contract: request, response, status codes, failover semantics. |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | `.env` variables, `providers.yaml` schema, timeouts, session database, CLI reference. |
| [docs/PROVIDERS.md](docs/PROVIDERS.md) | Adding providers, the full schema, model aliases, auth types, supported shapes, troubleshooting. |
| [docs/MCP_PROTOCOL.md](docs/MCP_PROTOCOL.md) | Client-facing `/mcp` spec: JSON-RPC shape, tools, notifications, tunnel setup. |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, auth realms, denylist inventory, approval flow, known limits. |
| [docs/TESTING.md](docs/TESTING.md) | How tests work, fixtures, per-file coverage map. |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Planned future work by phase: hardening, distribution, docs site, multi-user, dashboard, more providers/tools, deployment. |

---

## Known limits (tl;dr)

- Invincible translates Anthropic tool calls correctly (`tool_use` →
  `tool_calls`, `tool_result` → `role: "tool"` messages, ids preserved, and
  responses close with `stop_reason: "tool_use"`), but it does not execute
  the tools itself — execution is the client's job (Claude Code runs the
  tool and sends back `tool_result`).
- Image content blocks are **skipped** during flattening.
- Denylists are **text-pattern matches, not shell parsers** — wrappers like
  `powershell -Command` can smuggle commands past them; the token approval
  step is the real safety boundary.
- **Remote approval** — approval goes through `/mcp` itself: whoever holds
  a valid OAuth Bearer token can approve pending actions; there is no
  terminal prompt and no separate human-authentication surface. Revoke the
  client with `invincible oauth revoke <client_id>` to cut that off.
- Sessions are stored **plaintext** in SQLite; cooldowns and provider
  disables are **in-memory only**.

Full details: [docs/SECURITY.md](docs/SECURITY.md) → *Known limits*.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

See [docs/TESTING.md](docs/TESTING.md).
