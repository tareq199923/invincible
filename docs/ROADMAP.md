# Roadmap — Invincible

Where the project is, where it is going, and the status of every piece of
work. This document supersedes the previous phase-numbered plan; that
history is preserved in compressed form at the bottom.

Status labels used throughout:

| Label | Meaning |
|---|---|
| **Implemented** | Shipped and described accurately by the current docs |
| **In progress** | First PR landed; not yet complete |
| **Planned** | Agreed next work; not started |
| **Deferred** | Deliberately postponed; the design leaves room for it |
| **Deprecated** | Scheduled for replacement/removal; still functional |

---

## Direction

Invincible is becoming a **remote-first, multi-user AI continuity
platform**. The product principle:

> The LLM is replaceable. The user's identity, projects, memory, and
> continuity are not.

Target shape:

```text
                     ┌──────────────────┐
                     │     Web UI       │
                     │ invincible-ai.me │
                     └────────┬─────────┘
                              │
                     ┌────────▼────────┐
                     │ Invincible API  │
                     │ auth/MCP/chat   │
                     └────────┬────────┘
           Identity · Projects · API Keys
                              │
                    PostgreSQL / Neon
                              │
        Memory · Continuity · Sessions/Runs
                              │
                   Context Intelligence
                              │
                      Provider Router
                      /      |      \
                    LLM    LLM     LLM
```

Locked principles:

1. Invincible owns continuity — not the LLM provider.
2. Memory, task state, conversation context, and continuity are related
   but distinct concepts; they are never merged into one undifferentiated
   store.
3. The Context Intelligence layer decides what context the model actually
   receives.
4. Maximize **continuity per token**; never dump whole stores into prompts.
5. Hosted mode is the primary product; local/self-hosted mode remains a
   supported developer mode sharing the same core.
6. Evolve existing engines; do not build duplicate ones.
7. Never trust client-supplied resource IDs without server-side ownership
   verification.
8. Compatibility techniques (e.g., scoped session keys) are migration
   tools, not the final domain model.
9. Design seams for BYOK, vector retrieval, and future collaboration —
   implement none of them ahead of need.
10. Documentation follows implementation.

Decisions recorded for the platform work:

- **Dashboard**: FastAPI + Jinja2 + HTMX (no separate SPA/toolchain).
- **Accounts**: email + password (argon2id); API keys for programmatic
  clients (hashed at rest, shown once, revocable).
- **Local mode is preserved indefinitely** as a developer/self-hosted mode.
- **Providers v1**: platform-managed pool; per-user/project BYOK is
  designed (a config-source seam) but deliberately not built.
- **Session identity** migrates to relational ownership
  (`user_id`/`project_id`/`client_session_id`); string namespacing is a
  transitional technique only.
- **Saving**: reactive failover checkpointing in v1; predictive limit
  saving is deferred.

---

## Implemented today

Verified snapshot of shipped capability (file pointers in
[ARCHITECTURE.md](ARCHITECTURE.md)):

| Area | State |
|---|---|
| Gateway | OpenAI `POST /v1/chat/completions` + Anthropic `POST /v1/messages`, SSE streaming on both, translated through one internal message model (`invincible/compat/`). |
| Failover | Single loop (`core/router.py::_iter_attempts`): tier order, soft alias preference, 429/5xx/network → exponential cooldown (30s→300s cap), 401/403 permanent disable, opt-in `failover_on_400`; per-provider context trimming + send-time compression; `x-invincible-provider/model/attempts/request-id` response headers; one `runs` row per upstream attempt. |
| Storage | PostgreSQL-only (SQLAlchemy 2.0 async Core / asyncpg); packaged Alembic environment; `core/db.py` metadata is the schema source of truth; explicit `invincible db upgrade`; `doctor` verifies connectivity + revision loudly. |
| Sessions | Normalized `sessions`/`turns`/`messages`; whole-turn retention cap; per-session `SELECT … FOR UPDATE` serialization; streamed replies reconstructed and persisted. |
| Continuity | ContinuityEngine: versioned `task_states` per `(session, task_key)` with optimistic CAS (UNIQUE constraint + advisory locks), immutable checkpoints pinning versions, size-bounded continuation-brief injection, interruption detection from runs; MCP tools `task_state_set/get/checkpoint_create`. |
| Memory | Deterministic `(entity, relation, target)` fact extraction at persist time; idempotent; bounded latest-N system-message injection; `INVINCIBLE_MEMORY*` toggles. |
| MCP | `POST /mcp` JSON-RPC 2.0: `read_file`, `execute_bash`, `write_file`, `confirm_action`; text-pattern denylists; single-use token approvals; opt-in PG persistence of staged actions. |
| Auth | Three separate realms: `GATEWAY_API_KEY` bearer/x-api-key on `/v1/*` (timing-safe compare; FAILS OPEN when unset — loud startup warning); `INVINCIBLE_ADMIN_KEY` on `/api/v1/*` (fail-closed); OAuth 2.1 + PKCE authorization server on `/oauth/*` (dynamic registration, owner-secret browser consent, hashed tokens, refresh rotation, revocation). |
| Control plane | File-backed ProviderRegistry (CRUD/enable/disable/connectivity-test), `auto`/`pinned`/`chain` routing modes, `GET /api/v1/sessions/{id}/graph` projection. |
| CLI | `setup` (.env wizard incl. DB URL), `start` (uvicorn + Cloudflare tunnel with an orphan-free lifecycle), `doctor`, `dev-db`, `db upgrade`, `db import` (legacy SQLite), `secret rotate`, `oauth list/revoke/test-client`. Both `invincible` and `inv`. |
| Packaging/deploy | pyproject (name `invincible-ai`), packaged `providers.yaml` + migrations, Dockerfile, docker-compose app+postgres pair. |
| Quality gates | pytest + pytest-asyncio against real Postgres (27 test files); CI runs ruff check + pytest × Python 3.10–3.14 with a postgres:17 service; coverage artifact (~92% at last measurement). |

Honest single-tenant limitations (the reason the platform phases exist):

- No users, projects, or API-key entities exist anywhere; there is one
  shared `GATEWAY_API_KEY` and one global admin key.
- `sessions.session_id` is a client-supplied global PK; ownership is
  unverified on every path (e.g., the graph endpoint serves any session to
  whoever holds the admin key).
- MCP tokens identify a client, not a user; any token holder can reach any
  session bucket and approve any staged action.
- `facts.user_id` exists but is pinned to the sentinel `"default"`.
- Usage is computed (`compat/common.py::build_usage`) but never persisted;
  there is no audit log; login rate limiting and provider cooldowns are
  in-memory only.

---

## Platform phases — Planned

Execution order; each phase leaves the repository green
(`ruff check . && pytest`) and the docs truthful. A phase flips to
**In progress** when its first PR lands.

### Phase 1 — Identity and Ownership
**Scope:** `users` / `projects` / `api_keys` / `audit_log` / `memories`
schema; sessions rebuilt with surrogate identity + ownership columns
(`user_id`, `project_id`, `client_session_id`) with the turns/messages FK
chain repointed; security primitives (argon2id password hashing, hashed
API keys shown once with visible prefixes); an authenticated Principal
threaded through stores; one Alembic revision with a count-preserving
backfill to a system *local* owner; local-mode compatibility (the legacy
gateway key keeps working, mapped to that owner).
**Acceptance:** migration preserves row counts everywhere; the existing
suite passes unchanged in behavior; dual-realm auth (legacy key vs API
keys) resolves unambiguously.

### Phase 2 — Isolation and Security
**Scope:** server-side ownership predicates on every query path
(sessions, memory, tasks, checkpoints, runs, graph); resolve-or-create
session semantics for MCP/task writes; user subjects on OAuth
clients/tokens; audit-log writes for sensitive actions; persistent login
rate limiting; ID-enumeration and cross-user denial tests on every
resource type.
**Acceptance:** user A cannot access any user B resource through ANY
surface, including enumeration attempts.

### Phase 3 — Account and Project API
**Scope:** UserService/ProjectService/ApiKeyService;
`/auth/register|login|logout|me`, `/projects` CRUD+archive,
`/api-keys` lifecycle, read-only `/sessions`; device-code pairing backend
for the CLI; signed HttpOnly cookie browser sessions; minimal
signup/login UI (Jinja2 + HTMX scaffold).
**Acceptance:** register → login → create project → create key → use key
on chat → revoke works end-to-end; the pairing flow issues working
credentials.

### Phase 4 — Memory, Continuity, and Context Intelligence
**Scope:** `memories` with scopes (`user`/`project`) and layers
(`explicit`/`auto`); explicit-save triggers ("remember this", "save
this"); deterministic extractor seam (cheap-model extractor later);
lexical retrieval (PG FTS × recency × kind × confidence) behind
`RetrievalService`; a unified-budget ContextBuilder replacing independent
memory/continuity injections; reactive failover checkpoints fired inside
the router path **before** the next provider attempt; usage-token
persistence on runs.
**Acceptance:** relevant memories demonstrably outrank irrelevant ones;
total injected context stays within budget even against the smallest
configured provider; a provider failover produces a pre-switch checkpoint.

### Phase 5 — Full Dashboard
Projects, sessions, tasks, memory, API keys, usage, and settings views on
Jinja2 + HTMX. *(Planned)*

### Phase 6 — CLI Client Experience
`inv setup` device-code pairing through the browser; `inv start` client
mode; PostgreSQL/Alembic/tunnel/OAuth details hidden from ordinary users.
*(Planned)*

### Phase 7 — Deployment
Neon PostgreSQL + Railway hosting, production secrets discipline, health
checks, connection-pooler handling; domain invincible-ai.me. *(Planned)*

### Phase 8 — Cleanup
Remove hosted-mode fail-open paths; retire superseded local-era pieces
(see Deprecated) once the hosted flow is stable. Local mode itself stays.
*(Planned)*

---

## Deferred

Design seams exist; implementation deliberately postponed:

- Vector/semantic retrieval (behind `RetrievalService` /
  `EmbeddingProvider`).
- Per-user/per-project provider credentials (BYOK) beyond the
  config-source abstraction.
- Teams/workspaces/collaboration.
- Predictive quota/context-limit saving (token accounting groundwork
  lands in Phase 4; prediction does not).
- Social/passkey login; Prometheus metrics surface.

---

## Deprecated (scheduled — still functional)

| Item | Replacement | When |
|---|---|---|
| `GATEWAY_API_KEY` fail-open + single shared secret | Per-user API keys; fail-closed hosted mode | Phase 8 |
| Owner-secret-only MCP consent (`INVINCIBLE_OWNER_SECRET` as sole identity) | User-bound OAuth subjects | Phase 2+ |
| `facts` triple store | `memories` table (scopes/layers/provenance) | After Phase 4's migration |
| Legacy SQLite importer (`db import`) | Direct hosted signup/onboarding | After hosted launch stabilizes |
| Client-supplied `session_id` as storage identity | Relational session identity | Phase 1 (transitional helper retained briefly) |

Local/self-hosted mode is **not** deprecated.

---

## Historical record — previous phase plan

Compressed; details live in git history. Statuses reflect what actually
stands.

| Old phase | Outcome | Note |
|---|---|---|
| 0 Baseline snapshot | Superseded | Pre-PostgreSQL state; SQLite era retired by Phase 16 |
| 6 More providers | Done | Schema-validated providers.yaml; aliases; docs |
| 9 Context compression | Done | Send-time only; stored history verbatim |
| 10 Context memory | Done | Regex facts; bounded injection (evolves in Platform P4) |
| 11 Repo hygiene | Done | Lockfile strategy deferred |
| 12 Correctness/security fixes | Done | Streamed tool_calls persisted; `failover_on_400` flag; timing-safe compare; bounded limiter |
| 13 Failover unification + Settings | Done | One `_iter_attempts`; `core/config` + `core/trimming` split; `settings.py` |
| 13.5 Provider control plane | Done | Registry, routing modes, runs table, x-invincible-* headers, admin API |
| 14 Continuous integration | Done | Matrix 3.10–3.14; Postgres service folded into the test job; coverage artifact |
| 15a/b/c Canonical sessions · ContinuityEngine · Graph API | Done | Landed work previously unrecorded here; recorded now |
| 15 Observability (/metrics etc.) | Not started | Folded into platform backlog (candidate around P5/P7) |
| 16 PostgreSQL storage migration | Done | Slips honored elsewhere: audit_log → Platform P2; provider-health persistence → backlog; TIMESTAMPTZ deferred |
| 2 Zero-clone distribution (PyPI) | Not started | Folds into P6/P7 packaging |
| 3 Documentation site | Deferred | Revisit post-platform |
| 4 Multi-user system | Superseded | Realized as Platform Phases 1–3 |
| 5 Dashboard | Superseded | Realized as Platform Phase 5 |
| 7 More MCP tools | Backlog | Template approach still valid |
| 8 Deployment | Superseded | Realized as Platform Phase 7 |
| 1 Security hardening | Partially absorbed | Audit log + rate limiting → Platform P2; timing-safe compare already landed in 12 |

Working conventions unchanged: a phase moves **In progress** on its first
PR and **Done/Implemented** only when its acceptance criteria pass; every
phase PR ships tests and keeps `ruff check .` + `pytest` green;
security-adjacent changes update [SECURITY.md](SECURITY.md) in the same PR.

