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
| Failover | Single loop (`core/router.py::_iter_attempts`): tier order, soft alias preference, 429/5xx/network → exponential cooldown (30s→300s cap), 401/403 permanent disable, opt-in `failover_on_400`; per-provider context trimming + send-time compression; `x-invincible-provider/model/attempts/request-id` response headers; one `runs` row per upstream attempt **with token accounting since Phase 4** (real usage or flagged estimates; streaming output attached post-completion); **reactive failover checkpoints** — one injected pre-switch task-state snapshot per request, only when a task_state exists. |
| Storage | PostgreSQL-only (SQLAlchemy 2.0 async Core / asyncpg); packaged Alembic environment; `core/db.py` metadata is the schema source of truth; explicit `invincible db upgrade`; `doctor` verifies connectivity + revision loudly. |
| Identity | Phase 1: `users`/`projects`/`api_keys`/`audit_log`/`memories` tables; system *local* owner (user + default project) seeded at startup and by migration `0002`; sessions on surrogate identity with UNIQUE(user_id, project_id, client_session_id); argon2id primitives; API-key lifecycle in CLI. Phase 2: ownership predicates on every store path (task chains/checkpoints/runs scoped by owning surrogate session), OAuth user subjects, same-subject approval binding, persistent login rate limiting, audit writers on sensitive actions, dual-realm graph. |
| Sessions | Normalized `sessions`/`turns`/`messages`; whole-turn retention cap; per-session `SELECT … FOR UPDATE` serialization; streamed replies reconstructed and persisted; store API keeps client session strings with optional ownership context falling back to the local owner. |
| Continuity | ContinuityEngine: versioned `task_states` per `(session, task_key)` with optimistic CAS (UNIQUE constraint + advisory locks), immutable checkpoints pinning versions, size-bounded continuation-brief injection, interruption detection from runs; MCP tools `task_state_set/get/checkpoint_create`. |
| Memory | Phase 4: scoped `memories` (user/project scope, explicit/auto layers, kind, confidence, provenance) written at persist time by the deterministic extractor **and** explicit "remember this"/"save this" triggers; lexical retrieval (`RetrievalService`: generated-tsvector FTS × recency half-life × kind weight × confidence, AND→OR query fallback, relevance floor, top-N); unified-budget injection via `ContextBuilder` (memory + continuity brief under one token cap); the legacy per-session `facts` table is inert history. |
| MCP | `POST /mcp` JSON-RPC 2.0: `read_file`, `execute_bash`, `write_file`, `confirm_action`; text-pattern denylists; single-use token approvals bound to the staging subject (same-subject confirmation, audit-written); opt-in PG persistence of staged actions. |
| Auth | Four separate realms: `/v1/*` resolves a **Principal** dual-realm (Phase 1) — legacy `GATEWAY_API_KEY` bearer/x-api-key timing-safe compare mapping to the system *local* owner, or per-user `inv_*` API keys (SHA-256 hashed at rest, shown once, revocable via CLI); FAILS OPEN to the local identity when the gateway key is unset (loud startup warning); browser sessions on `/auth/*` + `/projects` + `/api-keys` (Phase 3: HMAC-signed HttpOnly cookies, fail-closed without the owner secret); `INVINCIBLE_ADMIN_KEY` on `/api/v1/*` (fail-closed operator override); OAuth 2.1 + PKCE authorization server on `/oauth/*` (dynamic registration, owner-secret browser consent, hashed tokens, refresh rotation, revocation) with consent-stamped user subjects and persistent lockouts (`login_attempts`, scoped per realm since 0004); GitHub login (OAuth App, verified-email auto-link). |
| Control plane | File-backed ProviderRegistry (CRUD/enable/disable/connectivity-test), `auto`/`pinned`/`chain` routing modes, `GET /api/v1/sessions/{id}/graph` projection. |
| CLI | `setup` (.env wizard incl. DB URL), `start` (uvicorn + Cloudflare tunnel with an orphan-free lifecycle), `login` (device-flow pairing, Phase 3), `doctor`, `dev-db`, `db upgrade`, `db import` (legacy SQLite), `secret rotate`, `oauth list/revoke/test-client`, `api-key create/list/revoke`. Both `invincible` and `inv`. |
| Packaging/deploy | pyproject (name `invincible-ai`), packaged `providers.yaml` + migrations + Jinja2 templates, Dockerfile, docker-compose app+postgres pair. |
| Quality gates | pytest + pytest-asyncio against real Postgres; CI runs ruff check + pytest × Python 3.10–3.14 with a postgres:17 service; coverage artifact (~92% at last measurement). |

Honest limitations remaining after Phase 4 (the reason the platform phases
exist):

- GitHub-only accounts cannot set or reset a password yet (a reset flow is
  future work); password login for them stays unavailable by design
  (`password_hash` NULL).
- Device pairing stores one pending request per CLI start; there is no
  admin view of device history beyond audit rows.
- `facts` is inert history: service code neither reads nor writes it (only
  the legacy importer fills it). No backfill into `memories` was performed.
- Retrieval is lexical only; semantic/vector retrieval remains a designed
  seam behind `RetrievalService`.
- Streaming usage on `runs` rows is estimated and flagged
  (`meta.usage_estimated`); real in-stream counts would require a wire
  change (`stream_options.include_usage`) that some compatible providers
  reject. Provider cooldowns remain in-memory by design.
- Audit coverage covers auth/grant/approval/admin-mutation events; chat
  completions themselves are not audited.

---

## Platform phases — Planned

Execution order; each phase leaves the repository green
(`ruff check . && pytest`) and the docs truthful. A phase flips to
**In progress** when its first PR lands.

### Phase 1 — Identity and Ownership
**Status: Implemented.** Scope landed: `users` / `projects` / `api_keys` /
`audit_log` / `memories` schema; sessions rebuilt with surrogate identity +
ownership columns (`user_id`, `project_id`, `client_session_id`) with the
turns/messages FK chain repointed; security primitives (argon2id password
hashing, hashed API keys shown once with visible prefixes); an
authenticated Principal threaded through the chat endpoints' session
persistence; one Alembic revision (`0002`) with a count-preserving,
in-migration-asserted backfill to a system *local* owner; local-mode
compatibility (the legacy gateway key keeps working, mapped to that owner;
unset-key fail-open behavior preserved). API keys are mintable via
`invincible api-key create/list/revoke`; dual-realm resolution is fixed
(legacy first) and collision-tested.
**Acceptance:** migration preserves row counts everywhere (scratch-DB
tests both directions, plus downgrade); the existing suite passes unchanged
in behavior; dual-realm auth (legacy key vs API keys) resolves
unambiguously.

### Phase 2 — Isolation and Security
**Status: Implemented.** Scope landed: server-side ownership predicates on
every query path — sessions/turns/messages (surrogate `session_pk`),
`task_states`/`checkpoints`/`runs` (ownership columns + backfill, string-
keyed version UNIQUE replaced by an owner-scoped partial unique index),
facts (principal-scoped namespaces), graph; resolve-or-create session
semantics for MCP/task writes; user subjects on OAuth clients/codes/tokens
with `require_mcp_auth` resolving a Principal (`kind="mcp"`); same-subject
binding for staged-action approvals; audit-log writers for grants, owner
logins/lockouts, token revocations, api-key mint/revoke, admin mutations,
and approval resolutions; persistent login rate limiting (`login_attempts`);
graph is dual-realm — operator override plus strictly user-scoped access.
**Acceptance:** user A cannot access any user B resource through ANY
surface, including enumeration attempts — pinned by `test_isolation.py`
(graph foreign-session denial + identical negative shapes under probing,
cross-principal task chains/checkpoints/runs/facts isolation,
same-subject approvals) and scratch-DB migration tests proving two owners
sharing one client string maintain independent version chains.

### Phase 3 — Account and Project API
**Status: Implemented.** Scope landed: `UserService`/`ProjectService`/
`DeviceCodeStore` (+`IdentityStore`, `GitHubOAuth`) in `core/accounts.py`;
`/auth/register|login|logout|me`, `/projects` CRUD+archive, `/api-keys`
lifecycle (raw shown once), read-only `/sessions`; RFC 8628-style
device-code pairing backend with browser approval pages and the
`invincible login` CLI; stateless HMAC-signed HttpOnly cookie sessions
(owner-secret-derived key, fail-closed when unset); minimal signup/login/
account UI (Jinja2 templates + form posts). Plus **GitHub login**: OAuth App
authorization-code flow with a signed single-use state cookie, auto-link by
VERIFIED primary email only, identity-conflict refusal, and GitHub-only
accounts (`password_hash` NULL).
**Acceptance:** register → login → create project → create key → use key
on chat → revoke works end-to-end; the pairing flow issues working
credentials (tested against the real router); cookies never authorize
`/v1/*`; MCP bearers and the gateway key cannot touch account management;
GitHub flows covered for registration, linking, unverified rejection,
state mismatch, and identity conflict.

### Phase 4 — Memory, Continuity, and Context Intelligence
**Status: Implemented.** Scope landed: migration `0005` (nullable
`runs.input_tokens`/`output_tokens`; stored generated `tsvector` + GIN
index on `memories`, regconfig shared with the query layer); `MemoryStore`
rewritten onto scoped `memories` (auto-extracted rows at confidence 0.6;
explicit "remember this"/"save this" chat triggers at confidence 1.0,
user-scope, user-messages-only, no explicit/auto double-capture; the
per-session `facts` pipeline retired — injection path removed, table left
inert with **no backfill**); `RetrievalService` (lexical match × recency
half-life × kind weight × confidence; AND-first query shape with OR
fallback for conversational questions; relevance floor + top-N knobs);
`ContextBuilder` giving memory + continuity injections one shared token
budget (continuity priority, truncation markers, default 1200 tokens);
reactive failover checkpoints fired once per request inside
`_iter_attempts` through an injected hook (router stays continuity-agnostic;
engine no-ops without task state); usage persistence on runs (real counts
where upstream reports them, flagged estimates otherwise, streaming output
attached post-completion).
**Acceptance:** relevant memories demonstrably outrank irrelevant ones
(same-terms pairs separated by confidence × recency, weak rows dropped by
the floor); total injected context stays within budget even against the
smallest configured provider (`assemble` pinned hermetically against the
Router's own estimator); a provider failover produces exactly one pre-switch
checkpoint when a task_state exists and none otherwise.

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
| `facts` triple store | `memories` table (scopes/layers/provenance) | Phase 4 (injection retired; table inert) |
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

