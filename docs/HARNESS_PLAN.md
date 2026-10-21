# Harness Plan — outbound-only harness behavior (additive, non-breaking)

> **Last verified: 2026-10-21** on branch `main`
> (migration head `0017`; F05 merged as `011ec2c`).
>
> Verification method: import graph, route table, settings constants,
> schema metadata, and the tool registry — compared against this file,
> not against the prose of earlier revisions.
>
> Status vocabulary used below:
>
> - **live** — a running request path reaches it.
> - **dormant** — module, tests, and docs exist, but nothing in the app
>   calls it. Dormant is not shipped; it is a library awaiting a caller.
> - **partial** — one half is live, the other is not.
> - **missing** — promised here, not implemented.
> - **removed** — deliberately deleted.

## 1. Status at a glance

| Phase | Artifact | Tests | Status | Evidence |
|---|---|---|---|---|
| H0 events/bus | `core/harness_events.py`, `core/harness_bus.py`; wired in `main.py` lifespan | `test_harness_bus.py` | **live** | `tool.requested/completed/failed` and `approval.resolved` emit from `endpoints/mcp.py`; `approval.requested` emits when an action is staged |
| H1 WS relay | `WS /agent/ws`, `WS /harness/events` | `test_agent_ws.py` | **live** | 4401 auth close, 5 sockets/user, 20 s heartbeat, WS-first dispatch with poll fallback |
| H2 policy gate | `core/harness_policy.py::before_tool_call` | `test_harness_policy.py` | **live** | Called from `endpoints/mcp.py` and `core/tool_dispatch.py` |
| H2 loop spine | `core/harness_runtime.py::run_workflow` | `test_harness_runtime.py` | **dormant** | No production importer; the module calls itself the "unwired spine (Step 7)" and points at the `harness_router` header |
| H3 hydration | `hydrate_context`, `compact_turns`, `INVINCIBLE_HARNESS_SUMMARIZER` | `test_harness_runtime.py` | **dormant** | Only tests call them |
| H4 router/handoff | `core/harness_router.py::handle_tool_call` | `test_harness_router.py` | **dormant** | No production caller; module header says "NOT wired into `POST /mcp`" |
| H4 supervisor | — deleted in Step 5 | — | **removed** | Replaced by the read-only delegation in Steps 8/8b |
| H5 approvals | `core/harness_approvals.py`, migrations `0011`/`0012` | `test_harness_approvals.py` | **dormant** | `harness_approvals` is imported by nothing; the 10-minute fast path is live |
| H5 durable log | `workflow_events`, `core/workflow_store.py` | `test_dashboard_workflows.py` | **live** | Fan-out events persist with `user_id`; `/dashboard/workflows` reads owner-scoped timelines; rows without `user_id` stay hidden |
| H6a parity tools | `code_search`, `process_list`, `screenshot` | tool suites | **live** | Registry-derived `/mcp` descriptors |
| H6b tunneling | `core/harness_tunnel.py`, `endpoints/tunnel.py` | `test_tunnel_registry.py`, `test_tunnel_route.py` | **live** | 2 h TTL, 256 KiB/s, 100 MiB, 3 per user |
| H6c CLI | `invincible harness setup/connect/status/service install` | `test_cli_harness.py` | **live** | writes a systemd unit / launchd plist on POSIX, prints a `schtasks` command on Windows; enabling stays manual |
| H6c Machines | `GET /dashboard/machines` | `test_dashboard_machines.py` | **live** | Per-account, read-only |
| H6c Workflows | `GET /dashboard/workflows`, `core/workflow_store.py` | `test_dashboard_workflows.py` | **live** | Owner-scoped list + detail; migration `0017`; foreign/unknown ids share one 404 |
| Step 8 explore | `core/harness_explore.py` | `test_harness_explore.py` | **live** | 4 consecutive reads, 8 iterations, 300-word budget |
| Step 8b fan-out | `core/harness_fanout.py` | `test_harness_fanout.py` | **live** | 6 reads across 2 planes, width 4, 150 words per step |

Two facts this table exists to keep honest:

1. The loop spine, hydration, the handoff router, and the durable
   approval store are **dormant**. Earlier revisions called them
   "shipped"; they have tests and docs but no live caller.
2. `workflow_events` is written and read: fan-out events carry a
   `workflow_id` and `user_id`, and `/dashboard/workflows` renders only
   the signed-in account's timelines. The page is metadata-only.

## 2. Goal

Build an **outbound-only harness** (outbound-only machine agent, relay,
MCP tools, screenshots/tunneling, auto-discovery, memory) with **event/bus
`harness-engineering` code structure** (`events` + `bus.emit()`,
WebSocket streams, `tools / policy / memory / router / supervisor /
approvals`, `runtime.runWorkflow` spine) **without breaking** Invincible's
existing structure, auth realms, wire protocols, or conventions.

## 3. Non-negotiable guards (from AGENTS.md)

1. Schema truth in `invincible/core/db.py` metadata. Migrations under
   `invincible/migrations/versions/`, run ONLY via `invincible db upgrade`.
   Never auto-run at startup. Verify against `create_all`. Head in this
   tree: `0017` (F05 stage 2 merged as `011ec2c`; F29 adds the workflow
   ownership column).
2. All in-app env reads via `invincible/core/settings.py` (live-read
   accessors). `cli.py` is the only exemption.
3. Exactly one provider failover loop: `core/router.py::_iter_attempts`.
   `route_request` / `stream_open` are thin wrappers. The
   agent-orchestration router is `harness_router.py` and does not
   duplicate provider failover.
4. Layering: `compat/` never imports Router. Handlers thin
   (`endpoints/`), business logic in `core/`, stores are thin SQLAlchemy
   async Core repos.
5. Timestamps = epoch floats. JSONB columns bind objects natively, never
   `json.dumps` before insert.
6. Auth realms stay separate:
   - `/v1/*` = per-user `inv_` API keys only, fail closed.
   - Dashboard `/auth/*`, `/api-keys`, consent = session-cookie realm,
     fail closed without `INVINCIBLE_OWNER_SECRET`.
   - `/mcp` = OAuth 2.1 + PKCE bearer tokens (hashed at rest, revocable).
   - `/agent/*` = `inv_` key narrow dep `require_agent_auth` (NOT
     `require_auth`), per-user isolation.
   Do not merge realms.
7. Secrets never logged. DSNs masked with `_mask_url`. Provider creds by
   env-var NAME, resolved at request time.
8. Wire shapes of `/v1/chat/completions`, `/v1/messages`, `/v1/responses`,
   `/mcp` (SSE order, tool-call round-trips, error mapping) are tested
   behavior. Change only deliberately with test updates.
9. Deployment: public deploys MUST set `INVINCIBLE_AGENT_ROUTING=1`,
   single instance (cooldowns, `PendingActionStore`, `AgentRegistry`
   in-process), Postgres CRUD-only role + separate schema-owner for
   migrations.
10. Every change: tests + `ruff check .` clean + full suite green.
    Security-adjacent touches update `docs/SECURITY.md` in the same PR.
    Docs follow implementation.

## 4. Current state (verified)

| Area | File | State |
|---|---|---|
| App wiring | `invincible/main.py` | lifespan constructs the `Router`, stores, and `HarnessBus` (engine attached) |
| Provider routing | `core/router.py`, `core/selection.py`, `core/provider_health.py` | single `_iter_attempts` loop, tier order, 429/5xx cooldown, 401/403 disable |
| MCP server | `endpoints/mcp.py` | `POST /mcp` JSON-RPC 2.0; 34 descriptors from `harness_tools.mcp_descriptors()`; OAuth bearer only |
| Tool execution | `core/tool_executor.py` | denylists; staged actions with `secrets.token_urlsafe(16)`; 600 s TTL; optional persisted tokens |
| Agent transport | `invincible/agent/runner.py`, `invincible/agent/sandbox.py`, `endpoints/agents.py`, `core/agent_registry.py` | long-poll hold 25 s; `WS /agent/ws`; WS-first dispatch; home sandbox |
| Memory/continuity | `core/memory.py`, `core/retrieval.py`, `core/context_builder.py`, `core/relay.py`, `core/continuity.py` | scoped `memories`; lexical retrieval + pg_trgm typo fallback (default on); unified 3000-token injection budget; relay digest; versioned task state |
| Settings | `core/settings.py` | `AGENT_ONLINE_TTL_SECONDS=60`, `AGENT_POLL_HOLD_SECONDS=25`, `AGENT_JOB_GRACE_SECONDS=10`; all toggles live-read |
| Dependencies | `pyproject.toml` | `websockets` is a core dependency (imported lazily by the agent); no `langchain` |
| Events | `core/harness_events.py` | 17 event types |
| Durable log | `core/db.py`, `core/workflow_store.py` | `workflow_events(id, workflow_id, user_id FK, type, payload JSONB, created_at)`; index `(user_id, created_at)`; no `seq` column |

## 5. Design decisions

1. **WS library: `websockets`, WS-first with poll fallback.** It is a
   core dependency, not an agent-only extra; the agent imports it lazily
   so a missing install degrades to polling. `AgentRegistry` keeps
   at most 5 sockets per user and at most 5 held polls.
2. **Bus: in-memory truth, Postgres append.** Bounded history (500
   events) powers the inspector replay. Events carrying a `workflow_id`
   are appended to `workflow_events` best-effort; ambient events stay in
   memory.
3. **LangChain: no dependency, and no compatibility claim until it is
   true.** `HarnessTool` currently exposes `name`, `description_mcp`,
   `description_webchat`, `properties`, `required`, `read_only`,
   `needs_approval`, `mcp`, `webchat`, `webchat_modes`, `agent_job`,
   `router_agents`, `data_plane`, `server_executable`. It has no
   `args_schema` or `func`, so `StructuredTool` adapter compatibility
   would require an explicit adapter, not a field rename.
4. **Tunneling: shipped as H6b (F03).** `expose_port` is staged and
   confirmed through `PendingActionStore`; `/t/{token}/` is a capability
   URL with per-user tokens, caps, audit, expiry, and identical 404s.

## 6. Phase history

### H0 — Events + bus

`harness_events` defines the typed event vocabulary (17 names).
`harness_bus` keeps a bounded in-memory history, fans out to
subscribers, and appends workflow-scoped events to Postgres. The MCP
dispatch emits `tool.requested` before execution and
`tool.completed`/`tool.failed` after. Staged actions now emit
`approval.requested`; `confirm_action` emits `approval.resolved`.
Metadata only: no commands, paths, contents, or tokens.

### H1 — WS relay + inspector stream

`WS /agent/ws` accepts an `inv_` key in the `Authorization` header or
`?api_key=` and closes with 4401 on failure. `AgentRegistry.dispatch`
pushes a job to exactly one attached socket and removes it from the poll
queue, so it cannot execute twice; without a socket the job waits for
the next poll. The agent sends results back over the same socket. The
read-only `WS /harness/events` stream authenticates with the account
session cookie, replays the bounded history, then follows new events.

### H2 — Runtime spine + policy gate

The policy gate is live: `before_tool_call` is the single pre-execution
entry point for machine-plane tools, called from the MCP dispatcher and
`tool_dispatch`. The runtime spine (`run_workflow`) is dormant; it was
built and tested, but no production path calls it. Do not describe it as
live.

### H3 — Hydrator + optional summarizer

`hydrate_context`, `summarize_turns`, `compact_turns`, and
`INVINCIBLE_HARNESS_SUMMARIZER` exist and are tested, but only tests call
them. They stay dormant until the spine has a caller.

### H4 — Agent handoff + supervisor

`harness_router` defines `triage` and `operator` agents plus a typed
`handoff` interception. The module is deliberately not wired into
`POST /mcp`; `handle_tool_call` has no production caller. The supervisor
was deleted in Step 5. The live delegation path is Steps 8/8b.

### Step 8 — explore sub-agent

`run_explore` is called from `webchat_agent.run_agent_turn` after four
consecutive machine reads, once per turn. It runs a read-only loop with
its own cap (8 iterations) and injects a 300-word summary as one system
message. Failure falls back to the main loop doing the work itself.

### Step 8b — read-only fan-out

`run_fanout` is called from the same turn loop when at least six reads
span at least two planes. One planner call produces a validated,
width-capped (4) plan; `scout` (machine reads) and `librarian` (data-plane
reads) run in parallel; findings are merged into one system message. The
main loop synthesizes; there is no second synthesize call. Mutating
tools are unoffered and refused.

### H5 — Durable approvals + durable log

Migrations `0011` and `0012` add `workflow_events` and the
`suspended_workflow_id`/deadline columns. `harness_approvals` implements
`suspend`/`resolve` with structural separation from the fast path. The
module is dormant: no production path constructs `ApprovalStore`.
`workflow_events` receives fan-out events; since F29 the Workflows page
reads them back as owner-scoped timelines.

### H6a — parity tools, safe set

`code_search`, `process_list`, and `screenshot` are live in the registry.
The agent hello reports `ripgrep`, `docker`, and `chrome`; it does not
report `gpu` (no portable probe).

### H6b — Tunneling (shipped, F03)

`expose_port` is staged and confirmed; on confirm the agent dials
`localhost:PORT` over `WS /agent/ws` and the server exposes
`/t/{token}/...` with per-user token auth, byte-rate and lifetime caps,
audit rows, auto-expiry, and hard-close orphans. `tunnel_close` is
single-call, owner-bound, and idempotent.

### H6c — CLI + dashboard harness UX

`invincible harness setup/connect/status` and `service install` are live;
`status` uses `/agent/whoami` and `/agent/machines`. `service install`
writes a systemd unit or launchd plist on POSIX, prints a `schtasks`
command on Windows; enabling the service remains manual. The Machines
page is live. The Workflows page is live: owner-scoped, read-only, and
metadata-only, backed by migration `0017`.

## 7. What remains

1. **Decide the caller for the dormant spine, router, and approvals.**
   Either wire them into the H-later assistant (with tests proving the
   live path) or delete them. Do not advertise them as shipped in the
   meantime.
2. **`gpu` capability.** Removed from the plan; no portable probe.
3. **WS transport docs.** `docs/MCP_PROTOCOL.md` §8 now documents the
   relay, the inspector stream, the heartbeat, the result frame, and the
   poll fallback.
4. **`approval.requested`.** Now emitted at staging (metadata only).
5. **Service install.** Documented as advisory; it does not enable the
   service and does not install one on Windows.

## 8. Acceptance

Current, honest acceptance:

- `pip install invincible-ai; invincible harness setup; invincible harness
  connect` → Cursor/Claude at `/mcp` gets memory tools immediately and
  machine tools once an agent is online; zero inbound ports on the PC.
- WS kill → the agent falls back to long-poll with identical job and
  result shapes; server restart → agents re-register (in-flight jobs are
  documented orphans; durable resume is dormant).
- User A jobs never reach user B machines or sockets (pinned tests).
- `/dashboard/workflows` shows only the signed-in account's durable
  timelines; a foreign or unknown workflow id is a 404; the page renders
  metadata summaries only, never task text, tool output, or file contents.
- `ruff check . && pytest` green; docs updated in the same PR as code.

Dormant modules must not be listed as acceptance evidence until they have
a live caller.

## 9. Risks

- Extra WS dependency — core dep, lazily imported by the agent, pinned and
  matrix-tested.
- `expose_port` public URL — mitigated by per-user tokens, caps, audit,
  expiry; deferred until H6b for that reason.
- `screenshot` needs Chrome on the agent — degrades to `unavailable`,
  never fails the workflow.
- Durable `emit` doubles writes — H5 only, best-effort with memory as the
  source of truth on failure (same posture as `PendingActionStore`).
- Dormant modules drift: they are tested but not exercised by the product,
  so a passing suite is not evidence that a user path works.
