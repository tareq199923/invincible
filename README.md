# Invincible — AI Continuity Gateway & Local MCP Agent

<p align="center">
  <a href="https://pypi.org/project/invincible-ai/"><img src="https://img.shields.io/pypi/v/invincible-ai" alt="PyPI version" /></a>
  <a href="https://pypi.org/project/invincible-ai/"><img src="https://img.shields.io/pypi/pyversions/invincible-ai" alt="Python versions" /></a>
  <a href="https://github.com/tareq199923/invincible/actions/workflows/tests.yml"><img src="https://github.com/tareq199923/invincible/actions/workflows/tests.yml/badge.svg" alt="Tests" /></a>
  <a href="https://github.com/tareq199923/invincible/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT" /></a>
  <a href="https://invincible-ai.me/docs"><img src="https://img.shields.io/badge/docs-invincible--ai.me-6f6cf0.svg" alt="Documentation" /></a>
</p>

> **Project direction:** Invincible is a **remote-first, multi-user AI
> continuity platform** — accounts, projects, shared memory across models,
> context intelligence, and a web dashboard (invincible-ai.me). Users
> install one CLI command on their own PC and pair with the hosted service;
> the database, provider credentials, and all state live server-side. The
> same code runs as a single-user self-host. See
> [docs/ROADMAP.md](docs/ROADMAP.md) for the direction, what is implemented
> today, and what is planned.

> **Status:** `0.6.x` beta. Pre-1.0 releases may contain breaking changes;
> see [CHANGELOG.md](CHANGELOG.md).

<p align="center">
  <img src="docs/images/invincible-landing.png" alt="Invincible landing page" width="1200" />
</p>

<p align="center"><sub>
<a href="#what-is-invincible">What it is</a> ·
<a href="#screenshots">Screenshots</a> ·
<a href="#installation">Install</a> ·
<a href="#quick-start">Quick start</a> ·
<a href="#api">API</a> ·
<a href="#mcp-support">MCP</a> ·
<a href="#architecture">Architecture</a> ·
<a href="#documentation">Docs</a> ·
<a href="#contributing">Contributing</a>
</sub></p>

---

## What is Invincible?

A **remote-first, multi-user AI gateway**: one Python (FastAPI) service that
many users sign into, each with their own projects, API keys, provider
credentials, memory, and continuity — served over HTTPS from any host. The
same code runs as a single-user self-host on a laptop (the local/self-hosted
mode is supported indefinitely); nothing else changes between the two.

It serves three roles in one process:

1. **Self-Service BYOK Gateway** — an OpenAI-compatible `/v1/chat/completions`
   endpoint where every user authenticates with their own `inv_` API key and
   routes through **their own connected provider credentials** (BYOK), with
   transparent failover on rate limits (429) and server errors, so a
   free-tier 429 no longer kills an agent's workflow. It also speaks the
   **Anthropic Messages API** (`POST /v1/messages`) and the **OpenAI
   Responses API** (`POST /v1/responses`), so Claude Code, Codex CLI, and
   other native clients plug in with a one-line config change.
2. **Multi-user account platform** — browser accounts (email + password, or
   GitHub), a dashboard for sessions, memory, usage, and provider
   credentials, and strict per-user ownership: one account can never read
   another's sessions, memory, or history.
3. **MCP Tool Server** — a JSON-RPC 2.0 `/mcp` endpoint exposing
   `read_file`, `execute_bash`, and `write_file` to a cloud-hosted AI. With
   `INVINCIBLE_AGENT_ROUTING=1` (required on any public multi-user
   deployment) the tools execute on **the user's own paired machine**, so a
   remote AI acts on *their* files under *their* account.

### Why it exists

- **The 429 problem.** AI coding agents using free/open-source providers get
  killed when they hit a rate limit. Invincible sits between the agent and
  the providers; on a 429 (or 5xx) it records the failure, puts the provider
  in a short cooldown, and retries the next credential in the user's own
  routing order. The agent sees a single, stable endpoint.
- **The cloud-to-local gap.** Cloud AI tools (e.g. the Claude web/mobile app)
  can reason well but cannot read the user's files, write to disk, or run
  terminal commands. Invincible's MCP server exposes those capabilities over
  HTTPS, so a remote model can act on each user's own machine — under that
  user's confirmation for anything destructive.

---

## Screenshots

| Memory graph | Usage |
|---|---|
| <img src="docs/images/dashboard-memory.png" alt="Dashboard memory graph" width="470" /> | <img src="docs/images/dashboard-usage.png" alt="Dashboard usage statistics" width="470" /> |

---

## Features

| Feature | What it gives you |
|---|---|
| **Remote-first, multi-user** | One deployment serves many accounts over HTTPS: browser sign-up (email + password or GitHub), per-user projects, API keys, provider credentials, and a dashboard. |
| **BYOK routing** | Every user connects their own provider credentials and routes only through them — `auto`, `pinned`, or explicit `chain` order. There is no shared gateway key and no shared provider pool. |
| **Failover + cooldown** | 429/5xx/network errors move to the next credential; cooldowns grow 30s → 60s → 120s → 240s, capped at 300s, and reset on success. |
| **Conversation memory** | PostgreSQL-backed, keyed by the `X-Session-Id` header. History is merged into every request and the reply is persisted back. |
| **Continuity** | Versioned task state, checkpoints, and continuation briefs, so a session can resume across models and restarts. |
| **Context trimming** | Per-credential `max_context`; system messages are always kept; history is dropped as atomic turns, never splitting an assistant `tool_calls` from its tool results. |
| **Per-provider timeouts** | Separate connect/read/write/pool timeouts with sane defaults and per-provider overrides. |
| **MCP tool server** | `read_file`, `execute_bash`, `write_file`, guarded by denylists and staged, single-use approval tokens. Tools run on the user's paired machine when agent routing is enabled. |
| **Accounts & projects** | Browser accounts with argon2id passwords or GitHub login, self-service projects, and `inv_` API keys shown once (SHA-256 at rest). |
| **Protocol-agnostic** | OpenAI, Anthropic, and Responses protocols translated into one internal message model, with tool calls round-tripping losslessly. |
| **Dashboard** | Jinja2 + HTMX: sessions, memory graph, tasks, usage, providers, and machines. |

---

## Installation

**Requirements:** Python 3.10+ and, for a self-hosted server, PostgreSQL.
The hosted service needs neither — just the CLI.

**Users: one install command. That's all.**

```bash
pip install -U invincible-ai     # safe to re-run: installs, or upgrades in place
invincible connect               # first run: browser opens → sign in or register → Approve
```

The PyPI package is `invincible-ai`; the import name is `invincible`, and both
CLI commands (`invincible` and `inv`) are identical.

Prefer an isolated CLI that never touches your project's Python environment?
`pipx install invincible-ai` (or `uv tool install invincible-ai`); upgrade it
with `pipx upgrade invincible-ai` / `uv tool upgrade invincible-ai`.

> **Already on an older release?** Re-run the same command
> (`pip install -U invincible-ai`). `No such command 'connect'` means you are
> on 0.5.x: upgrade, or use the old spelling `invincible harness connect`.
> Self-hosters run `invincible db upgrade` after every upgrade.

The first run pairs your machine with the hosted service
(`https://invincible-ai.me` — the default): your browser opens, you
register or sign in, click **Approve**, and the agent starts. There is
**no database to set up, no `.env`, no provider configuration** on your
machine — your account, connected provider keys, memory, and sessions all
live on the hosted service. The local agent only executes confirmed tool
actions on your PC. Finish by adding `https://invincible-ai.me/mcp` as the
MCP connector in Claude (or any MCP client) — see
[Quick Start](#quick-start). (`invincible login` does the same pairing
without starting the agent loop.)

**Operators only: running your own server.** Everything above works
against the hosted service without it. If you self-host or operate an
instance, you need Python 3.10+ **and** a PostgreSQL database (all state —
conversations, OAuth grants, task state, staged approvals — lives there;
`INVINCIBLE_DB_URL` is required to start):

```bash
pip install -e .
invincible setup --db-url postgresql://user:pass@your-db-host:5432/invincible
invincible db upgrade                # create/migrate the schema (explicit, never auto-run)
invincible start --host 0.0.0.0      # omit --host for a loopback-only dev server
```

TLS, `$PORT`, proxy headers, the two-role database split, the container
start command, required secrets, and the go-live checklist:
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Development shortcuts
(`invincible dev-db`, `docker compose up`) and the full CLI reference:
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

### Upgrading

One upgrade command per install channel:

| Install | Upgrade | Verify |
|---|---|---|
| pip (user site or venv) | `pip install -U invincible-ai` | `invincible --version` |
| pipx | `pipx upgrade invincible-ai` | `invincible --version` |
| uv tool | `uv tool upgrade invincible-ai` | `invincible --version` |
| editable checkout | `git pull && pip install -e .` | `invincible --version` |

`invincible update` runs the correct pip command for pip installs (`--check`
for a dry run, `--yes` for scripted upgrades). It **refuses** pipx, uv,
editable, and container installs and prints the command you should use
instead, so it can never corrupt a managed environment.

`login`, `connect`, and `doctor` print a one-line notice when a newer
release is on PyPI (at most one check per 24h, cached under
`~/.invincible/`). Disable it with `INVINCIBLE_NO_UPDATE_CHECK=1`. The check
is a read-only GET to pypi.org — no account or machine data is sent.

After any upgrade: self-hosters run `invincible db upgrade` (migrations are
never automatic), and always-on agents restart with
`invincible harness service restart`.

---

## Quick Start

### A. Use the hosted service (one install command)

```bash
pip install -U invincible-ai
invincible connect   # first run: browser opens → sign in or register → Approve
```

Pairing registers your account on `https://invincible-ai.me` and saves the
minted key to `~/.invincible/config.json`. Then, in the dashboard:

1. **Providers** — connect your own provider keys (BYOK).
2. **MCP connector** — add `https://invincible-ai.me/mcp` to Claude (or any
   MCP client) and approve the client when the browser asks.

With the agent running, a remote AI can read/write files and run commands
**on your machine**, under your account's approvals. Prefer raw API access
instead? Mint an `inv_` key (dashboard → Account → API keys) and call the
gateway directly:

```bash
export INVINCIBLE_BASE=https://invincible-ai.me
export INVINCIBLE_API_KEY=inv_...        # dashboard -> Account -> API keys

curl $INVINCIBLE_BASE/v1/chat/completions \
  -H "Authorization: Bearer $INVINCIBLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}]}'
```

Every account is independent: the same commands work for every user with
their own `inv_` key, their own connected provider credentials, and their own
sessions — no account can see or route through another's.

### B. Run your own server (operators / self-hosters — users never do this)

```bash
invincible setup --db-url postgresql://user:pass@db-host:5432/invincible
invincible db upgrade               # create/migrate the schema (explicit, never auto-run)
invincible start                    # http://127.0.0.1:8000 (dev loopback)
invincible start --host 0.0.0.0     # reachable from other machines
```

`invincible setup` writes missing secret values (`INVINCIBLE_OWNER_SECRET`)
as random `secrets.token_urlsafe(32)` tokens, generates the BYOK credential
master key (`INVINCIBLE_CREDENTIAL_KEY`), and takes the `INVINCIBLE_DB_URL`
via `--db-url` (non-interactive) — preserving your existing `.env` comments
and values. `INVINCIBLE_OWNER_SECRET` signs account browser sessions
(dashboard login, OAuth consent) — not something `/mcp` requests send.

For a real host — TLS, `$PORT`, proxy headers, the two-role database split,
the container start command (`0.0.0.0:$PORT`), and the go-live checklist —
follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Development-only database
shortcuts are covered in [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

Clients of your own server point at it explicitly:
`invincible connect --server https://mycompany.ai` (or
`ANTHROPIC_BASE_URL=https://mycompany.ai` for Claude Code). The hosted
service is the CLI default, so your users pass no flag at all.

---

## Configuration

Everything is environment variables plus one YAML file — no other config.

| Variable | Purpose |
|---|---|
| `INVINCIBLE_DB_URL` | PostgreSQL DSN for all persistent state. **Required** to start a server. |
| `INVINCIBLE_MIGRATE_DB_URL` | Schema-owner DSN used only for the startup `db upgrade` in container deploys (falls back to `INVINCIBLE_DB_URL`). |
| `INVINCIBLE_OWNER_SECRET` | Signs account browser sessions (dashboard login, OAuth consent). Unset = those surfaces fail closed. Not sent on `/mcp`. |
| `INVINCIBLE_CREDENTIAL_KEY` | Fernet master key encrypting stored BYOK credentials at rest. Back it up — losing it makes saved provider keys undecryptable. |
| `INVINCIBLE_AGENT_ROUTING` | Set to `1` to route confirmed tool calls to each user's paired machine. **Required on public multi-user deployments**; otherwise tools run on the server host. |
| `INVINCIBLE_PERSIST_PENDING_ACTIONS` | Opt-in: persist staged `execute_bash`/`write_file` approvals in PostgreSQL so they survive a restart. |

`/v1/*` requests authenticate with **per-user `inv_` API keys** (minted on
the dashboard's Account page, or by the host with
`invincible api-key create --user <email-or-id>`). Each user connects their
own provider credentials on the dashboard's Providers page (BYOK) and routes
only through them — there is no shared gateway key and no shared provider
pool.

The secrets are **independent**: a leaked `/mcp` URL alone is not enough to
reach tool execution (a live OAuth bearer token is required as well), and
rotating one secret never affects the other.

Full reference — every variable, `providers.yaml` schema, timeout resolution,
database options: [docs/CONFIGURATION.md](docs/CONFIGURATION.md). Adding a
provider, aliases, and supported shapes: [docs/PROVIDERS.md](docs/PROVIDERS.md).
The packaged `invincible/providers.yaml` is a static test fixture; live
traffic never reads it, because every `/v1/*` request routes through the
caller's own BYOK credentials.

---

## CLI Commands

Most users need the first four rows; the rest is self-host administration.

| Command | Purpose |
|---|---|
| `invincible login [--server URL]` | Pair this machine with a server (device flow): the browser opens, you click Approve, and the `inv_` key is saved to `~/.invincible/config.json`. Defaults to the hosted service. |
| `invincible connect [--server URL]` | Keep this machine online and run confirmed jobs locally. First run pairs automatically. Nothing listens for inbound connections; Ctrl+C stops it. |
| `invincible status` | Check this machine: account, online state, next step. |
| `invincible update` | Install the latest release from PyPI (`--check` for a dry run, `--yes` for scripts). Refuses managed environments and prints the right command instead. |
| `invincible setup` | Create/update `.env` non-interactively, generating missing secrets and the credential key without echoing them. |
| `invincible start` | Start the server (`--host`, `--port`, `--reload`, `--env-file`, `--no-tunnel`). |
| `invincible db upgrade` | Run packaged Alembic migrations to head. Explicit by design — nothing auto-migrates. |
| `invincible doctor` | Diagnose config, secrets, and PostgreSQL connectivity/schema revision. |
| `invincible dev-db` | Provision a local development PostgreSQL and write a working `INVINCIBLE_DB_URL`. |

Administration (`harness setup|service install|service restart`, `oauth
list|revoke|test-client`, `api-key create|list|revoke`, `users
list|reset-password`) is documented in the full CLI reference:
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) → *CLI reference*.

```bash
invincible setup --force
invincible secret rotate            # new owner secret, in place
invincible start --port 9000
```

---

## API

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | none | Health check → `{"status": "healthy"}` |
| `HEAD` | `/` | none | 200 OK — Claude Code's base-URL probe |
| `GET` | `/health` | none | Service detail → `{"service", "status", "version"}` |
| `GET` | `/v1/models` | `Bearer inv_…` | OpenAI-compatible model list built from **your** connected credentials |
| `POST` | `/v1/chat/completions` | `Bearer inv_…` | OpenAI chat completions with failover (JSON or SSE) |
| `POST` | `/v1/messages` | `Bearer inv_…` | Anthropic Messages API (same routing) |
| `POST` | `/v1/responses` | `Bearer inv_…` | OpenAI Responses API for Codex CLI (same routing) |

### Chat request

- **Body**: `{"messages": [...], "stream": false}` — OpenAI message format.
  `messages`, `stream`, and `model` are accepted. `model` is a soft routing
  hint (an alias or exact `model_id`; unknown names are ignored). Other
  OpenAI fields are rejected with **422**.
- **Streaming**: `stream: true` returns OpenAI-compatible SSE. Each event is a
  `chat.completion.chunk` (`data: {…}\n\n`) and the stream ends with
  `data: [DONE]`. Chunks are forwarded as they arrive — nothing is buffered.
  A provider that fails before the first chunk triggers normal failover; an
  error after streaming starts ends the stream with a well-formed error event.
- **Sessions**: history is loaded from PostgreSQL keyed by the `X-Session-Id`
  header (default `default`), prepended to your messages, and the assistant
  reply is persisted back. `session_id` is a partition key, not a credential.
  Streamed replies are reconstructed from deltas and saved when the stream
  completes.
- **Response**: the upstream provider's JSON is forwarded **verbatim**.

### Anthropic Messages (`POST /v1/messages`)

Invincible speaks the **Anthropic Messages API**, so Claude Code and other
Anthropic-native clients work without modification:

```bash
# No /v1 suffix — Claude appends /v1/messages itself.
ANTHROPIC_BASE_URL=http://127.0.0.1:8000
ANTHROPIC_AUTH_TOKEN=inv_...   # older Claude Code: ANTHROPIC_API_KEY instead
```

Supported request fields are `model`, `system`, `messages`, `max_tokens`, and
`stream`; everything else Claude Code sends (`tools`, `metadata`,
`temperature`, `top_p`, `anthropic-beta`, the `?beta=true` query, unknown
fields) is accepted and ignored — never a 422. Streaming emits the canonical
Anthropic event order, tool calls round-trip losslessly with ids preserved,
sessions share history with the OpenAI endpoint, and errors map to Anthropic
error types. Image content blocks are skipped today.

The `model` value overrides each connected credential's stored default in
`auto` routing and is sent verbatim upstream; `GET /v1/models` lists exactly
what your key can route to. In `chain`/`pinned` routing each step carries its
own model and the request model only picks the entry point.

### Status codes

| Status | When |
|---|---|
| `200` | Upstream success — JSON body forwarded verbatim, or SSE stream |
| `400` | Caller has no connected provider credentials (BYOK) |
| `401` | Missing/invalid `inv_` API key |
| `422` | Body fails validation (missing `messages`, extra fields) |
| `4xx` | Upstream returned a non-failover error (e.g. 400) — forwarded verbatim |
| `503` | All of the caller's credentials failed or are in cooldown |

Full contract — request/response shapes, sessions, trimming, timeout
semantics: [docs/API_REFERENCE.md](docs/API_REFERENCE.md).

---

## MCP Support

`POST /mcp` implements a minimal JSON-RPC 2.0 subset: `initialize`,
`tools/list`, and `tools/call`. Protocol version: `2025-06-18`.

- **Auth**: OAuth 2.1 + PKCE via the built-in authorization server. Clients
  discover it at `/.well-known/oauth-protected-resource` (RFC 9728), register
  at `/oauth/register`, get owner approval on the `/oauth/authorize` consent
  page, then send `Authorization: Bearer <access_token>` on every request.
  Wrong/missing/expired/revoked token → `401` with a `WWW-Authenticate:
  Bearer resource_metadata="…"` challenge.
- **Notifications**: a request without an `id` still runs its side effect but
  the server replies `204 No Content`.

| Tool | Arguments | Confirmation | Gate |
|---|---|---|---|
| `read_file` | `path` | **No** | Blocks `.env*`, `sessions.db`, `.git/`; allows the repo, tests, and config. |
| `execute_bash` | `command` | **Yes** — staged token round-trip, 30s execution timeout | Blocks high-risk commands; the token is the real safety boundary. |
| `write_file` | `path`, `content` | **Yes** — staged token round-trip | Blocks writes to `.env*`, private keys, and other protected paths. |
| `confirm_action` | `token`, `approve` | — | Approves/denies a pending action; single-use, expires after 10 minutes. |

### Run tools on your own PC (the local agent)

On a hosted deployment with `INVINCIBLE_AGENT_ROUTING=1`, confirmed tool
actions execute on **your machine**, not the server. The security checks
(denylist, staging, approval tokens) stay server-side; the work travels to a
paired local agent. From zero to "my AI just ran a command on my PC" is two
commands:

```bash
pip install -U invincible-ai
invincible connect   # first run: browser opens → sign in or register → Approve
```

The agent pairs once, saves the minted key to `~/.invincible/config.json`,
and connects outbound — no inbound ports. Its sandbox is your home directory
(`INVINCIBLE_AGENT_ROOT` to override), with `.env*`, `.git`, `.ssh`, and key
material blocked everywhere. An offline agent answers `agent_offline`
immediately — nothing hangs. Self-hosters point at their own server once:
`invincible connect --server https://mycompany.ai`.

Full transport, threat model, and every denial rule:
[docs/MCP_PROTOCOL.md](docs/MCP_PROTOCOL.md) and
[docs/SECURITY.md](docs/SECURITY.md).

---

## Provider Routing

Every `/v1/*` request routes through **the caller's own connected BYOK
credentials**, in the order the user configured (`auto` by ability, or an
explicit `pinned`/`chain` order). Per attempt:

| Upstream status | Router behavior |
|---|---|
| `200` | `record_success` (resets cooldown) → return body |
| `429` / `5xx` | `record_failure` → cooldown → **try next credential** |
| `401` / `403` | Skip that credential for this request → **try next** |
| Other `4xx` (e.g. `400`) | **Abort** — forward the provider's status and body |
| Network error | `record_failure` → cooldown → **try next credential** |
| In cooldown | Skipped silently (log only) |

All of the caller's credentials exhausted → **HTTP 503**. No credentials
connected at all → **HTTP 400**. Cooldowns follow
`30 * 2**(failures-1)`, capped at **300s**; health state is in-memory and
resets on restart.

Deep dive (failover state machine, context trimming):
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Examples

```bash
export INVINCIBLE_BASE=https://invincible-ai.me   # or http://127.0.0.1:8000
export INVINCIBLE_API_KEY=inv_...                 # dashboard -> Account -> API keys
```

### Chat with session memory

```bash
curl $INVINCIBLE_BASE/v1/chat/completions \
  -H "Authorization: Bearer $INVINCIBLE_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: my-conversation" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}]}'
```

The reply is stored under `my-conversation` and included in your next request
with the same header — the model remembers the conversation.

### Stream a chat (SSE)

```bash
curl -N $INVINCIBLE_BASE/v1/chat/completions \
  -H "Authorization: Bearer $INVINCIBLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}], "stream": true}'
```

```text
data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"Hello"}}]}
data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"!"}}]}
data: {"object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

### Use it from Claude Code

```bash
ANTHROPIC_BASE_URL=https://invincible-ai.me claude   # or your own server URL
```

Health check, model listing, full SSE frames, Anthropic requests, and the
complete MCP flow (`initialize` → `tools/list` → `tools/call` →
`confirm_action`) are in [docs/API_REFERENCE.md](docs/API_REFERENCE.md) and
[docs/MCP_PROTOCOL.md](docs/MCP_PROTOCOL.md).

---

## Architecture

```mermaid
flowchart LR
  C["OpenAI / Anthropic /<br/>Responses clients"] --> EP["FastAPI endpoints<br/>/v1/* · /mcp · /dashboard"]
  M["Cloud AI<br/>via MCP"] --> EP
  EP --> X["compat/<br/>protocol translators"]
  X --> S["core/chat_service.py"]
  S --> R["core/router.py<br/>BYOK failover"]
  R --> P[("PostgreSQL<br/>sessions · memory · BYOK · OAuth")]
  EP --> T["core/tool_executor.py<br/>denylist + approvals"]
  T --> A["User's paired agent<br/>agent/runner.py"]
  A --> F["User's files and shell<br/>sandboxed to the agent root"]
```

The compatibility layers (OpenAI, Anthropic, Responses) only translate; all
three produce the same internal message model, which is what the shared chat
pipeline, the Router, the session store, and the trimming logic consume.

### Package layout

| Path | Role |
|---|---|
| `invincible/main.py` | FastAPI app, lifespan, auth dependencies, router wiring, `/health`. |
| `invincible/endpoints/` | OpenAI/Anthropic/Responses compat, MCP, accounts, BYOK, dashboard, docs site. |
| `invincible/compat/` | Pure protocol translators: flattening, finish/error maps, SSE. |
| `invincible/core/router.py` | The single tiered-failover loop over per-user BYOK credentials. |
| `invincible/core/chat_service.py` | Shared chat pipeline (history, memory, continuity, routing). |
| `invincible/core/tool_executor.py` | Denylists, staged approvals, tool execution. |
| `invincible/core/db.py` | SQLAlchemy engine factory + schema metadata (single source of truth). |
| `invincible/core/session_store.py` | Conversation memory on PostgreSQL, partitioned by session id. |
| `invincible/core/continuity.py` | Versioned task state, checkpoints, continuation briefs. |
| `invincible/core/settings.py` | Typed accessors for every `INVINCIBLE_*` environment variable. |
| `invincible/agent/runner.py` | The local agent: pairing, poll/WS loop, home-confined sandbox. |
| `invincible/endpoints/docs.py` | Public docs site (`/docs`). |
| `invincible/cli.py` | Click CLI: setup/start/login/connect/harness/update/doctor/db/oauth/api-key/users. |
| `invincible/migrations/` | Packaged Alembic environment (`invincible db upgrade`). |

---

## Documentation

| Doc | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, request flows, trimming and failover deep dives. |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | Chat and Anthropic contracts, examples, status codes, timeouts. |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every env var, `providers.yaml` schema, database options, full CLI reference. |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running it remotely: TLS, proxy headers, two-role database, go-live checklist. |
| [docs/PROVIDERS.md](docs/PROVIDERS.md) | Adding providers, schema, aliases, auth types, troubleshooting. |
| [docs/MCP_PROTOCOL.md](docs/MCP_PROTOCOL.md) | Client-facing `/mcp` spec: JSON-RPC, tools, OAuth, tunnel. |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, auth realms, denylists, known limits. |
| [docs/TESTING.md](docs/TESTING.md) | Test infrastructure, fixtures, per-file coverage map. |
| [docs/RELEASING.md](docs/RELEASING.md) | Cutting a release: versioning, build gate, PyPI workflow. |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Direction, verified implemented state, phased plan. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup, invariants, and the PR process. |
| [SECURITY.md](SECURITY.md) | Private vulnerability reporting and scope. |
| [CHANGELOG.md](CHANGELOG.md) | Release history. |

---

## Known limits

- Invincible translates Anthropic tool calls correctly (`tool_use` →
  `tool_calls`, `tool_result` → `role: "tool"`, ids preserved), but it does
  not execute the tools itself — execution is the client's job.
- Image content blocks are **skipped** during flattening.
- Denylists are **text-pattern matches, not shell parsers** — wrappers like
  `powershell -Command` can smuggle commands past them; the token approval
  step is the real safety boundary.
- **Remote approval** — whoever holds a valid OAuth Bearer token can approve
  pending actions; there is no terminal prompt. Revoke the client with
  `invincible oauth revoke <client_id>` to cut that off.
- Sessions are stored **plaintext** in PostgreSQL (protect the DSN);
  cooldowns and provider disables are **in-memory only**. Every store path is
  ownership-scoped per principal — one user can never read another's sessions,
  graph, or history.
- **In-memory server state.** Provider cooldowns, staged approvals (unless
  `INVINCIBLE_PERSIST_PENDING_ACTIONS` is set), and the agent registry live in
  the process, so a remote deployment runs a **single instance** (no
  horizontal autoscaling) and clients retry across a restart — see
  [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Full details: [docs/SECURITY.md](docs/SECURITY.md) → *Known limits*.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

See [docs/TESTING.md](docs/TESTING.md) for the test infrastructure and
[CONTRIBUTING.md](CONTRIBUTING.md) for the full setup — PostgreSQL, migrations,
invariants, and the PR process.

---

## Contributing

Contributions are welcome: bug reports, docs fixes, provider support, and
code. Start with [CONTRIBUTING.md](CONTRIBUTING.md). Everyone participating is
expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md). `AGENTS.md` is
the same guide written for AI coding assistants.

## Security

Please do **not** open public issues for security problems. Report privately
through [GitHub Security Advisories](https://github.com/tareq199923/invincible/security/advisories/new)
or email `jahidulislam199923@gmail.com`; scope and response times are in
[SECURITY.md](SECURITY.md). The full threat model and known limits live in
[docs/SECURITY.md](docs/SECURITY.md).

## License

[MIT](LICENSE) © 2026 Islam MD. Jahidul.
