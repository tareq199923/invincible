# AGENTS.md — Working on Invincible

Guide for contributors and coding agents working in this repository.

- Package name: `invincible` (repository directory: `ai-gateway`).
- Console scripts: `invincible` and `inv` (identical entry points).
- Python **3.10+** (`requires-python = ">=3.10"`; CI tests 3.10–3.14).

Invincible today is a **single-tenant local AI gateway**: an OpenAI- and
Anthropic-compatible tiered-failover proxy with PostgreSQL-backed
conversation memory, a continuity engine (versioned task state +
checkpoints), and an MCP tool server. It is evolving toward a
remote-first, multi-user AI continuity platform — see
[docs/ROADMAP.md](docs/ROADMAP.md) for the direction and phase status.

**Documentation follows implementation.** Never document a feature as
existing before its code and tests land.

---

## Setup and commands

```bash
pip install -e ".[dev]"     # runtime + dev tools (pytest, pytest-asyncio, ruff, ...)
ruff check .                # lint gate — must be clean before commit
pytest                      # full test suite
```

### PostgreSQL is required for most tests

The suite runs against a real PostgreSQL database (there is no SQLite
fallback):

| What | Value |
|---|---|
| Local convention | `postgresql+asyncpg://invincible@127.0.0.1:5433/invincible_test` |
| Provision locally | `invincible dev-db` (Docker fallback included), or `docker compose up db` |
| Override | `INVINCIBLE_TEST_DATABASE_URL` env var |
| CI | postgres:17 service container on port 5432 |

Fixture semantics (`tests/conftest.py`):

- `pg_engine` **hard-fails** when Postgres is unreachable — it never skips.
- `pg_live` / `admin_pg` auto-skip live-tier tests (doctor/CLI-db checks)
  when unreachable.
- Fully hermetic suites (run without Postgres): router, provider
  registry/schema/selection policy, health tracker, timeouts, compression,
  context trimming.

---

## Architecture map

| Path | Role |
|---|---|
| `invincible/main.py` | FastAPI app; lifespan wires engine + all stores; auth dependencies; `/health`, `HEAD /` |
| `endpoints/auth.py` | Dual-realm Principal resolution for `/v1/*` (legacy gateway key vs API keys; fail-open local when unset) |
| `core/principal.py` | Authenticated Principal model (`legacy` / `api_key` / `anonymous`) |
| `core/identity.py` | argon2id primitives, API-key lifecycle (sha256 at rest, shown once), audit log |
| `endpoints/openai_compat.py` | `POST /v1/chat/completions` (+ SSE streaming), `GET /v1/models` |
| `endpoints/anthropic_compat.py` | `POST /v1/messages` (Anthropic protocol + canonical SSE) |
| `endpoints/mcp.py` | `POST /mcp` JSON-RPC 2.0 tool server (OAuth-bearer protected) |
| `endpoints/oauth.py` | Built-in OAuth 2.1 + PKCE authorization server (owner-consent browser flow) |
| `endpoints/admin_api.py` | `/api/v1/*` management surface (fail-closed `INVINCIBLE_ADMIN_KEY`) |
| `endpoints/graph.py` | `GET /api/v1/sessions/{id}/graph` continuity projection |
| `core/router.py` | THE single tiered-failover loop (`_iter_attempts`); run recording |
| `core/provider_health.py` | Failure counts + exponential cooldowns (in-memory) |
| `core/provider_registry.py` | File-backed provider CRUD/enable/disable/test + routing state |
| `core/selection.py` | Pure auto/pinned/chain routing decisions |
| `core/config.py` | `providers.yaml` schema validation/loading; timeout resolution |
| `core/settings.py` | Typed live-read accessors for every `INVINCIBLE_*` env var |
| `core/db.py` | Engine factory + schema metadata (**single source of schema truth**) + system local-owner bootstrap |
| `core/session_store.py` | Normalized `sessions`/`turns`/`messages` persistence (surrogate identity + ownership triple since Phase 1) |
| `core/memory.py` | Deterministic fact extraction/injection (`facts` table) |
| `core/continuity.py` | ContinuityEngine: versioned task states, checkpoints, continuation briefs |
| `core/run_store.py` | One `runs` row per upstream provider attempt |
| `core/oauth_store.py` | OAuth clients/codes/hashed tokens on PostgreSQL |
| `core/tool_executor.py` | MCP tool execution, denylists, staged token approvals |
| `core/trimming.py` | Token estimation, turn grouping, per-provider context trimming |
| `core/compression.py` | Send-time-only request compression (stored history stays verbatim) |
| `core/db_import.py` | One-shot legacy SQLite → PostgreSQL importer |
| `cli.py` | Click CLI: setup/start(+tunnel)/doctor/dev-db/db/secret/oauth |
| `compat/common.py`, `compat/anthropic.py` | Protocol-neutral internal message model; Anthropic translators/SSE |
| `models/anthropic.py` | Lenient Anthropic request model (unknown fields ignored) |
| `migrations/` | Packaged Alembic environment (baseline revision `0001`) |

---

## Conventions (non-negotiable)

1. **Schema truth lives in `core/db.py` metadata.** Alembic migrations are
   packaged under `invincible/migrations/` and run ONLY via
   `invincible db upgrade` — never auto-run at startup (startup warns;
   `doctor` fails loudly on stale/unmanaged schemas). Any table change
   updates the metadata AND adds a packaged Alembic revision verified
   against `create_all`.
2. **All in-app env reads go through `core/settings.py`.** `cli.py` is the
   documented exemption (launcher/checker, not service code).
3. **Exactly one failover loop exists** (`router._iter_attempts`);
   `route_request`/`stream_open` are thin wrappers. Never write a parallel
   attempt loop or duplicate the failover classification.
4. **Layering:** the compat layer never imports the Router; route handlers
   stay thin; stores are thin repositories over SQLAlchemy async Core;
   business logic lives in `core/` modules.
5. **Data types:** timestamps are epoch floats (TIMESTAMPTZ deliberately
   deferred); JSON-shaped columns are PostgreSQL JSONB and bind objects
   natively — never pre-dump with `json.dumps` before insert.
6. **Auth realms are separate by design:**
   - `GATEWAY_API_KEY` guards `/v1/*` (timing-safe compare; FAILS OPEN when
     unset — there is a loud startup warning).
   - `INVINCIBLE_ADMIN_KEY` guards `/api/v1/*` management/graph surface
     (fails CLOSED).
   - `/mcp` uses OAuth bearer tokens (hashed at rest, revocable via CLI).
   Do not merge realms or flip fail-open/fail-closed semantics casually.
7. **Secrets discipline:** secrets are never logged or echoed; DSNs are
   always password-masked in output (`_mask_url`); provider credentials are
   referenced by env-var NAME in configuration and resolved at request time.
8. **Protocol compatibility is tested behavior.** Wire shapes of
   `/v1/chat/completions`, `/v1/messages`, and `/mcp` (SSE event order,
   tool-call round-trips, error mapping) are guarded by tests. Change them
   only deliberately, updating those tests — never by weakening them.

---

## Testing conventions

- `pytest.ini`: `asyncio_mode = auto`, `pythonpath = .` — bare
  `async def test_*` functions work without decorators.
- Upstream providers are ALWAYS faked via `httpx.MockTransport`
  (`make_router` / `default_providers` helpers in `tests/conftest.py`).
  Real providers are never called from tests.
- The full-app `client` fixture builds `app.state.*` manually without the
  lifespan; `bearer_headers` walks the real OAuth register→consent→token
  flow.
- Migration changes must be exercised against scratch databases (patterns
  in `tests/test_cli_db.py` and `tests/test_doctor.py`).

---

## Documentation index

| Doc | Covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, request flows, trimming/failover deep dives |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | Chat endpoints contract, sessions, failover semantics |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Env vars, providers.yaml schema, CLI reference |
| [docs/PROVIDERS.md](docs/PROVIDERS.md) | Adding providers; full provider schema |
| [docs/MCP_PROTOCOL.md](docs/MCP_PROTOCOL.md) | /mcp client-facing spec, tools, tunnel setup |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, auth realms, denylists, known limits |
| [docs/TESTING.md](docs/TESTING.md) | Test infrastructure and per-file coverage map |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Platform direction, current state, phase plan |

---

## Change discipline

- Every feature ships with tests; `ruff check .` clean; full suite green.
- Security-adjacent changes (`tool_executor.py`, `oauth_store.py`,
  `endpoints/oauth.py`, auth dependencies) update
  [docs/SECURITY.md](docs/SECURITY.md) in the same PR.
- Roadmap statuses flip `Planned → In progress` on first PR and
  `In progress → Implemented/Done` only when acceptance criteria pass.
- Docs follow implementation — update them as part of the change, never
  ahead of it.

