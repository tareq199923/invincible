# Roadmap — Invincible

Where the project is, where it is going, and the status of every piece of
work. This document supersedes the previous phase-numbered plan; that
history is preserved in compressed form at the bottom.

The single ordered list of actionable work (open findings, fix plans,
decision queue, completed log) lives in [WORKQUEUE.md](WORKQUEUE.md).

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
| Memory | Phase 4: scoped `memories` (user/project scope, explicit/auto layers, kind, confidence, provenance) written at persist time by the deterministic extractor (user messages
only since 2026-09-25) **and** explicit "remember this"/"save this" triggers; lexical retrieval (`RetrievalService`: generated-tsvector FTS × recency half-life × kind weight × confidence, AND→OR query fallback, relevance floor, top-N); unified-budget injection via `ContextBuilder` (memory + continuity brief under one token cap). |
| MCP | `POST /mcp` JSON-RPC 2.0, fifteen tools: machine-plane `read_file`, `execute_bash`, `write_file`, `confirm_action` (text-pattern denylists; single-use token approvals bound to the staging subject, audit-written; opt-in PG persistence of staged actions; agent routing moves confirmed execution to the caller's paired local agent) plus read-only `code_search` (ripgrep-or-walk, same sandbox), `process_list`, and agent-only `screenshot` (headless Chrome on the paired machine; the server never fetches caller URLs); continuity tools `task_state_set`/`task_state_get`/`checkpoint_create`; memory tools `memory_save`/`memory_search`/`memory_list` (data-plane, no confirm gate — confidence 0.9, `mcp:<client>` provenance, ownership-predicated, kill-switch-gated saves, no MCP delete; documented in SECURITY.md §2.0b); project tools `project_create`/`project_list`. |
| Auth | Four separate realms: `/v1/*` accepts only per-user `inv_` API keys via bearer/x-api-key (SHA-256 hashed at rest, shown once, revocable via CLI), resolving a **Principal** bound to the key's user and default project; fail-closed (401 for missing, invalid, or revoked keys), with no legacy gateway-key fallback or anonymous local-owner access; browser sessions on `/auth/*` + `/projects` + `/api-keys` (Phase 3: HMAC-signed HttpOnly cookies, fail-closed without the owner secret); the operator/admin surface is **gone** (removed with the operator role in commit `c3e768f`; the only `/api/v1/*` route left is the owner-scoped continuity graph, and host administration is the environment variables plus the CLI); OAuth 2.1 + PKCE authorization server on `/oauth/*` (dynamic registration, owner-secret browser consent, hashed tokens, refresh rotation, revocation) with consent-stamped user subjects and persistent lockouts (`login_attempts`, scoped per realm since 0004); GitHub login (OAuth App, verified-email auto-link); GitHub-only accounts adopt a first password via `POST /auth/password` (Phase 5), and every password write bumps the per-user `session_version` (migration `0006`) so browser cookies minted before the change stop resolving immediately. Anonymous browser GETs that hit a 401 anywhere are redirected to `/login?next=<path>` (`Accept: text/html` → 302, HTMX → `HX-Redirect`; API clients keep the JSON body; POSTs never redirect) — see `main.py`'s exception handler. |
| Dashboard | Phase 5 (Jinja2 + HTMX; script vendored at `/static/htmx.min.js`): `/dashboard` overview (owned count cards + 10 recent sessions), sessions index + per-session detail rendering the shared projection (`core/projection.py`, also backing the graph endpoint), cross-session task board, memory management (browse/filter/search, explicit create, audited owner-scoped delete — `INVINCIBLE_MEMORY=0` blocks creation only), memory-graph view (2026-09-06: `/dashboard/memory/graph` server-rendered SVG — center-radial project clusters, source-colored dots, timeline strip; Level 1 derived relationships via `core/memory_projection.py`, JSON sibling `GET /memories/graph` as the permanent contract for a future UI redesign), usage view with UTC day buckets (JSON sibling `GET /usage`, window clamped 1–90 days), settings page (system flags, read-only provider/routing panel, password forms), machines page (`/dashboard/machines`: per-machine online state, platform, auto-discovered capabilities, read-only). The whole surface resolves **session cookies only** (`require_user_session`) — foreign resources are byte-identical to unknown ones. |
| Harness | Machine-harness layer over the Phase 10 agent (`core/harness_*`, `docs/HARNESS_PLAN.md`): typed event bus (`harness_events`/`harness_bus`, workflow-scoped rows persisted to `workflow_events`), unified policy gate (`harness_policy`), dependency-injected loop spine + context hydration/compaction (`harness_runtime`), agent handoff router + parallel supervisor (`harness_router`/`harness_supervisor`), durable suspend/resume approvals (`harness_approvals`, migrations `0011`/`0012`); outbound-only WS relay (`WS /agent/ws`, WS-first with long-poll fallback, per-machine inventory with capabilities) plus read-only inspector stream (`WS /harness/events`). |
| Control plane | Static provider config (packaged `providers.yaml` fixture + `core/provider_catalog.py` operator constants). Routing is **per user** since Phase 9: each account's own BYOK credentials with `auto`/`pinned`/`chain` settings in `user_settings`, managed on the dashboard's Providers page. `GET /api/v1/sessions/{id}/graph` projection. There is no admin API and no shared provider pool. |
| CLI | `setup` (non-interactive since 2026-09-01: zero prompts, secrets auto-generated, provider keys configured later via the dashboard/env, DB URL via `--db-url` — remote-first; scriptable on Windows), `start` (uvicorn + Cloudflare tunnel with an orphan-free lifecycle; opens `/dashboard` in the browser — anonymous sessions land on `/login`), `login` (device-flow pairing, Phase 3; defaults to the hosted service `https://invincible-ai.me`, `--server` for a self-hosted server), `agent` (runs confirmed tool jobs on the user's own machine, Phase 10), `doctor`, `dev-db`, `db upgrade`, `secret rotate` / `secret credential-key`, `oauth list/revoke/test-client`, `api-key create/list/revoke`, `users list/reset-password`. Both `invincible` and `inv`. |
| Packaging/deploy | pyproject (name `invincible-ai`), packaged `providers.yaml` + migrations + Jinja2 templates, Dockerfile, docker-compose app+postgres pair, `Procfile` + `railway.json` (platform start command with `$PORT` and proxy headers). Remote runbook: [DEPLOYMENT.md](DEPLOYMENT.md). |
| Quality gates | pytest + pytest-asyncio against real Postgres; CI runs ruff check + pytest × Python 3.10–3.14 with a postgres:17 service; coverage artifact (~92% at last measurement). |

Honest limitations remaining:

- Password reset is operator-side only (`invincible users
  reset-password`, shipped 2026-09-03): no email-based self-service
  flow — the gateway has no mail infrastructure by design.
- Device pairing stores one pending request per CLI start; there is no
  admin view of device history beyond audit rows.
- The legacy per-session `facts` table was dropped by revision `0013`
  (2026-09-25) after a production audit found it empty; no backfill into
  `memories` was ever performed.
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
in-migration-asserted backfill to a system *local* owner. API keys are
mintable via `invincible api-key create/list/revoke`.
**Historical auth behavior at Phase 1 delivery:** the legacy gateway key
mapped to the local owner, unset-key fail-open behavior was preserved, and
dual-realm resolution was legacy-first and collision-tested. That
compatibility has since been removed: `/v1/*` now accepts only per-user
`inv_` keys and fails closed in all modes.
**Acceptance at Phase 1 delivery:** migration preserved row counts everywhere
(scratch-DB tests both directions, plus downgrade); the existing suite passed
unchanged in behavior; legacy-key vs API-key resolution was unambiguous.

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
rewritten onto scoped `memories` (auto-extracted rows at confidence 0.6,
mined from user messages only since 2026-09-25;
explicit "remember this"/"save this" chat triggers at confidence 1.0,
user-scope, user-messages-only, no explicit/auto double-capture; the
per-session `facts` pipeline retired in Phase 4 and its table dropped by
revision `0013` with **no backfill**); `RetrievalService` (lexical match × recency
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
Projects, sessions, tasks, memory, usage, and settings views on
Jinja2 + HTMX (API-key lifecycle remains on the Phase 3 `/account`
page; the dashboard overview carries the live count). **Status:
Implemented.** Scope landed across six slices
plus a hardening remediation arc: `/dashboard` overview — count cards +
recent sessions on the browser-session realm, vendored HTMX, site nav on
authed pages (PR-A); projection extraction into `core/projection.py`
shared by the graph endpoint, a sessions index, per-session detail
(runs chain, failovers, checkpoints, activity) and a cross-session task
board (PR-B); memory management — browse/filter/paginate, lexical
search, explicit create, audited owner-predicated deletes, with
`INVINCIBLE_MEMORY=0` gating creation only (PR-C); usage aggregation +
cookie-realm usage view over UTC-pinned day buckets (PR-D); settings
page + password set/change via `POST /auth/password`, where the STORED
account state decides set-vs-change (PR-E); per-user
`users.session_version` bumped atomically inside every password write
(migration `0006`) so previously issued browser cookies stop resolving
immediately, plus the UTC-bucket regression pin (remediation).
**Acceptance:** every view requires a live session cookie and is
ownership-predicated — foreign ids render the identical unknown/404
body everywhere (pinned across the dashboard suites); `inv_*` API keys
never authorize the new surface (pinned on the settings password flow),
and no other realm reaches it by construction; changing a password
invalidates every other browser session while the acting client keeps
its login and API keys keep working; memory deletes and both password
actions are audit-written; usage day buckets stay identical under three
different session timezones (UTC regression test).

### Phase 6 — CLI Client Experience
**Status: In progress (partly shipped).** Shipped: hosted-by-default device
pairing (`invincible login`, with `--server` for a self-hosted server) and
the agent's one-command self-pairing (`invincible agent`), so an ordinary
user never touches PostgreSQL/Alembic/tunnel/OAuth details. Client-mode
pass landed 2026-09-25: `--help` leads with login/agent under a hosted
heading and groups the rest as self-host administration (command names
and paths unchanged; every user is their own operator against the
hosted service). Possible follow-up: a remote `status`/`whoami` so a
hosted user can check login + agent state without touching the server.

### Phase 7 — Deployment
**Status: Deployed 2026-09-02 — production operational; Railway ownership
transfer pending.** Neon PostgreSQL (ap-southeast-1, autosuspend + pooled
DSN, role split per the acceptance criteria below, all verified by direct
probe) + the app containerized on Railway
(`invincible-gateway-production.up.railway.app`, `railway.json` start
command + `/health` healthcheck). Fresh-start database: no dev data migrated
(the live dataset was ~2 accounts of rehearsal traffic); first registration
bootstrapped the operator. Full acceptance journey smoke-tested live:
health, register→operator, login, dashboard, Providers page, and a real
chat round-trip through the provider pool. Neon has a two-year term, and
there is no current Azure or AWS migration planned. The current operational
task is to transfer Railway project ownership to a different Railway account
while keeping the application, database, and domain in place. Domain
`invincible-ai.me` is live: Cloudflare CNAME → the Railway host (DNS-only
mode; Railway serves a Let's Encrypt cert from its Singapore edge), verified
end-to-end including a chat round-trip. Historical note: the docs carried
the typo `invinseble-ai.me` for weeks — that domain was never registered;
the real domain is `invincible-ai.me`. Dockerfile note: its default CMD runs
migrations with the app DSN — on Railway the `?sslmode=require` URL param
must stay out of the asyncpg DSN (migrations no-op at head anyway).

Deployment acceptance criteria (explicit; permission model detailed in
[SECURITY.md](SECURITY.md) §8; operational version and go-live checklist in
[DEPLOYMENT.md](DEPLOYMENT.md) §7):

- (a) **Persistent managed Postgres** (Neon or equivalent) with a backup
  story — never a temp-directory or otherwise ephemeral cluster.
- (b) **Non-superuser app role**: runtime connects with
  SELECT/INSERT/UPDATE/DELETE + sequence usage only; migrations run
  under a separate schema-owner role (the role split and reference
  grants ship in `docker-compose.yml` / `docker/db-init/01-roles.sh`).
- (c) **Enforced password auth** (`scram-sha-256`) on every connection —
  `trust` never leaves an isolated dev loopback.
- (d) **Fresh secrets for the target environment** — DB credentials and
  all `INVINCIBLE_*` secrets generated per environment, never carried over
  from dev.

### Phase 8 — Cleanup
The shared gateway-key and anonymous fail-open paths have been removed;
`/v1/*` requires per-user `inv_` keys in hosted and local modes. The legacy
SQLite importer was removed 2026-09-24. The legacy `facts` table was
audited empty on production, backed up, and dropped by revision `0013`
on 2026-09-25. Local mode itself stays.


### Phase 9 — BYOK Provider Connections
Per-user Bring-Your-Own-Key provider connections: encrypted credential
storage, connect/list/test/remove API, per-user router candidate pool,
and a dashboard Providers page. **Status: Complete.** PR-A (storage
& encryption primitive) landed the Fernet credential crypto,
``user_provider_credentials`` schema + migration ``0007``,
``INVINCIBLE_CREDENTIAL_KEY`` settings accessor, CLI
``secret credential-key``, and the startup fail-closed warning. PR-B
landed the connect/list/test/remove API with the SSRF guard and audit
rows; PR-C landed the per-user router candidate pool
(``byok_attempt_source``). PR-D (`27bebf4`) landed the dashboard
Providers UI: catalog connect cards with connected-state flip, custom
provider form, HTMX test/remove with row delete, nav entry, and
realm/fail-closed gates pinned by ``tests/test_dashboard_providers.py``.


### Phase 10 — Local Agent (tool execution on the user's PC)
Move confirmed MCP tool execution off the server host and onto each
user's own machine: a paired local agent (``invincible agent``,
WS-first ``invincible harness connect`` since H1) that holds an
outbound-only relay (``WS /agent/ws`` with long-poll fallback) with its
``inv_`` key, executes confirmed ``execute_bash``/``write_file``/
``read_file`` jobs locally (plus read-only ``code_search``/
``process_list`` and agent-only ``screenshot`` since H6a), and posts
results back through ``POST /agent/result`` (or the WS itself). The server keeps
every decision (denylist, staging, tokens, audit, routing by
``user_id``); the agent only does the work — with a local denylist
re-check (wall 2) and a home-relative read/write sandbox (wall 3,
``invincible/agent/sandbox.py``). Opt-in via ``INVINCIBLE_AGENT_ROUTING``
(default off = server-local execution, unchanged). The OAuth consent
gate relaxes for non-operators **iff** routing is on (approving exposes
only one's own machine — the coupling is pinned by
``tests/test_oauth_consent_relaxation.py`` and documented in
[SECURITY.md §10](SECURITY.md)). One new dependency (`websockets`, agent
relay + server WS routes); migrations `0011`/`0012` (workflow log,
approval columns) landed with the H5 durable-approval slice:
long-poll over plain HTTPS, in-memory registry (restart orphans
in-flight jobs; agents re-register on next poll). Dashboard MCP page
gained a live agent online/offline badge (``GET /agent/status``),
and the dashboard Machines page lists per-machine inventory
(``GET /agent/status`` machines + ``GET /agent/machines`` for the CLI).
Pinned by ``tests/test_agent_registry.py``,
``tests/test_agent_endpoints.py``, ``tests/test_agent_routing.py``,
``tests/test_agent_sandbox.py``, ``tests/test_cli_agent.py``,
``tests/test_agent_ws.py`` (relay + inventory),
``tests/test_dashboard_machines.py``, ``tests/test_cli_harness.py``,
``tests/test_harness_*.py`` (bus/policy/runtime/memory/router/
supervisor/approvals/tools).

---

## Deferred

Design seams exist; implementation deliberately postponed:

- Vector/semantic retrieval (behind `RetrievalService` /
  `EmbeddingProvider`).
- Teams/workspaces/collaboration.
- Predictive quota/context-limit saving (token accounting groundwork
  lands in Phase 4; prediction does not).
- Social/passkey login; Prometheus metrics surface.

---

## Deprecated and removed

Items below are either scheduled for replacement/removal or explicitly marked
as removed.

| Item | Replacement | When |
|---|---|---|
| Owner-secret-only MCP consent (`INVINCIBLE_OWNER_SECRET` as sole identity) | User-bound OAuth subjects | Retired 2026-09-25 (subject mandatory at issuance; legacy rows fail closed) |
| `facts` triple store | `memories` table (scopes/layers/provenance) | Phase 4 request path retired; table dropped by revision `0013` on 2026-09-25 after an empty production audit (no backfill ever performed). |
| Legacy SQLite importer (`db import`) | Direct hosted signup/onboarding | Removed 2026-09-24 |
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
| 13.5 Provider control plane | Partly superseded | Registry, routing modes, runs table, x-invincible-* headers landed; the admin API and the shared provider pool were later removed (`c3e768f`) with the operator role — routing is per-user BYOK now |
| 14 Continuous integration | Done | Matrix 3.10–3.14; Postgres service folded into the test job; coverage artifact |
| 15a/b/c Canonical sessions · ContinuityEngine · Graph API | Done | Landed work previously unrecorded here; recorded now |
| 15 Observability (/metrics etc.) | Not started | Folded into platform backlog (candidate around P5/P7) |
| 16 PostgreSQL storage migration | Done | Slips honored elsewhere: audit_log → Platform P2; provider-health persistence → backlog; TIMESTAMPTZ deferred |
| 2 Zero-clone distribution (PyPI) | In progress | Packaging/metadata/CI landed 2026-09-21 (`docs/RELEASING.md`); only the actual pypi.org upload remains |
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

