# Harness Plan — outbound-only harness behavior (additive, non-breaking)

> Status 2026-09-25: H0 (events/bus), H1 (WS relay), H2 (policy+spine),
> H3 (hydration), H4 (router/supervisor), H5 (durable approvals, revs
> 0011/0012), H6a (search/process/screenshot), H6c (machine inventory,
> CLI, dashboard) shipped. H6b (tunneling) deliberately deferred.

## 1. Goal

Build an **outbound-only harness** (outbound-only machine agent, relay, MCP
tools, screenshots/tunneling, auto-discovery, memory) with **event/bus
`harness-engineering` code structure** (`events` + `bus.emit()`, WebSocket
streams, `tools / policy / memory / router / supervisor / approvals`,
`runtime.runWorkflow` spine) **without breaking** Invincible's existing
structure, auth realms, wire protocols, or conventions.

### Non-negotiable guards (from AGENTS.md)

1. Schema truth in `invincible/core/db.py` metadata. Migrations under
   `invincible/migrations/versions/`, run ONLY via `invincible db upgrade`.
  Never auto-run at startup. Verify against `create_all`. Current head: `0015`.
2. All in-app env reads via `invincible/core/settings.py` (live-read
   accessors). `cli.py` is the only exemption.
3. Exactly one provider failover loop: `core/router.py::_iter_attempts`.
   `route_request` / `stream_open` are thin wrappers. New agent-orchestration
   router MUST have a different name (`harness_router.py`).
4. Layering: `compat/` never imports Router. Handlers thin (`endpoints/`),
   business logic in `core/`, stores are thin SQLAlchemy async Core repos.
5. Timestamps = epoch floats. JSONB columns bind objects natively, never
   `json.dumps` before insert.
6. Auth realms stay separate:
   - `/v1/*` = per-user `inv_` API keys only, fail closed.
   - Dashboard `/auth/*`, `/api-keys`, consent = session-cookie realm, fail
     closed without `INVINCIBLE_OWNER_SECRET`.
   - `/mcp` = OAuth 2.1 + PKCE bearer tokens (hashed at rest, revocable).
   - `/agent/*` = `inv_` key narrow dep `require_agent_auth` (NOT
     `require_auth`), per-user isolation.
   Do not merge realms.
7. Secrets never logged. DSNs masked with `_mask_url`. Provider creds by
   env-var NAME, resolved at request time.
8. Wire shapes of `/v1/chat/completions`, `/v1/messages`, `/v1/responses`,
   `/mcp` (SSE order, tool-call round-trips, error mapping) are tested
   behavior. Change only deliberately with test updates.
9. Deployment: public deploys MUST set `INVINCIBLE_AGENT_ROUTING=1`, single
   instance (cooldowns, `PendingActionStore`, `AgentRegistry` in-process),
   Postgres CRUD-only role + separate schema-owner for migrations.
10. Every change: tests + `ruff check .` clean + full suite green.
    Security-adjacent touches update `docs/SECURITY.md` same PR. Docs follow
    implementation.

## 2. Starting point (verified)

| Area | File | State |
|---|---|---|
| App wiring | `invincible/main.py:72-145` lifespan | engine + `Router()` bare (BYOK-only), `OAuthStore`, `MemoryStore`, `RetrievalService`, `RunStore`, `ContinuityEngine`, `PendingActionStore`, `SessionStore`, `ApiKeyStore`, `AuditLog`, `AgentRegistry` |
| Provider routing | `core/router.py`, `core/selection.py`, `core/provider_health.py` | single `_iter_attempts` loop, tier order, 429/5xx cooldown 30s→300s, 401/403 disable, per-provider trim |
| MCP server | `endpoints/mcp.py:1-948` | `POST /mcp` JSON-RPC 2.0, `TOOLS` list (12 tools: `read_file/execute_bash/write_file/confirm_action`, `task_state_*`, `memory_*`, `project_*`), OAuth `require_mcp_auth`, per-user `owner_subject` binding |
| Tool execution | `core/tool_executor.py:1-632` | denylist + path denylist, `PendingActionStore` (10-min TTL, single-use `secrets.token_urlsafe(16)`), `confirm_action` with optional `executor` callback (Phase 10 hook) |
| Agent transport | `endpoints/agents.py`, `core/agent_registry.py`, `agent/runner.py`, `agent/sandbox.py` | long-poll `POST /agent/poll` (hold 25s) + `POST /agent/result`, `GET /agent/status` (session realm) + `GET /agent/machines` / `GET /agent/whoami` (inv_ key realm); in-memory per-`user_id` queues/futures, `MAX_POLLS_PER_USER=5`; runner re-checks denylist (Wall 2) + home sandbox (Wall 3), byte-identical result shapes |
| Memory/continuity | `core/memory.py`, `core/retrieval.py`, `core/context_builder.py`, `core/relay.py`, `core/continuity.py` | scoped `memories`, lexical FTS×recency×kind×confidence, unified 1200-token budget, relay digest, versioned `task_states` + reactive failover checkpoints |
| Settings | `core/settings.py` | `AGENT_ONLINE_TTL=60`, `POLL_HOLD=25`, `JOB_GRACE=10`, all toggles live-read |
| Deps | `pyproject.toml` | `fastapi, uvicorn, httpx, sqlalchemy[asyncio], asyncpg, alembic, argon2-cffi, cryptography, jinja2, click, PyYAML, python-dotenv`. No `websockets`, no `langchain`. Python 3.10–3.14 |

## 3. Target mapping (new files only)

| Source pattern | Invincible new module | Notes |
|---|---|---|
| `shared/events.ts` (`EventType` enum + `AgentEvent`) | `invincible/core/harness_events.py` | `HarnessEventType(str, Enum)` + TypedDicts. Same 15 event names. No DB |
| `harness/bus.ts` (`emit`, `subscribe`, `history`) | `invincible/core/harness_bus.py` | in-memory first (H0), Postgres append added H5. Also forwards `approval.*`/`tool.*` metadata to existing `AuditLog` |
| `harness/runtime.ts` (`runWorkflow` spine) | `invincible/core/harness_runtime.py` | `run_workflow(workflow_id, input)` calling existing `session_store`, `context_builder`, `router`, `harness_policy`, `harness_bus`, `tool_executor`/`agent_registry`, `continuity` |
| `harness/tools.ts` (safe vs dangerous) | `invincible/core/harness_tools.py` | SHIPPED (tool-registry step): `HarnessTool` frozen-dataclass registry — the single source of truth for tool name/descriptions/schemas/classification. `/mcp` descriptors, webchat schemas + sets + mode subsets, router agent tuples, and the docs plane split all derive from it (byte-identical, golden-pinned); execution bodies, policy branches, and audit names stay hand-written. Field-compatible with `langchain_core.tools.StructuredTool`, no import |
| `harness/sandbox.ts` (`runInSandbox`) | keep `agent/sandbox.py` + `core/tool_executor.py` | already the boundary. Add `harness_policy.before_tool_call()` orchestrator, no new patterns yet |
| `harness/memory.ts` (`buildContext`, `summarize`) | wrapper in `harness_runtime.hydrate_context()` over `core/context_builder.assemble()` | relay stays cheap path; opt-in LLM summarizer behind flag |
| `harness/agents.ts` + `harness/router.ts` | `invincible/core/harness_router.py` | `Agent{name,system_prompt,tools_subset}`, deterministic→LLM→human routing. NOT `core/router.py` |
| `harness/supervisor.ts` | `invincible/core/harness_supervisor.py` | `make_plan → asyncio.gather(dispatch) → synthesize`, `PlanCreated/Subagent*` emits, partial-failure degrade |
| `harness/db.ts` + DBOS durable + `ApprovalStore` | `invincible/core/harness_approvals.py` + migrations `0011`, `0012` | `suspend()/resume()` on top of `ContinuityEngine` + `pending_actions` extension |
| `server/index.ts` (Express+`ws`) | `WS /agent/ws` in `endpoints/agents.py` + `WS /harness/events` read-only inspector stream | FastAPI/Starlette WS, same `inv_` / cookie realms respectively |
| `web/` inspector | dashboard `Machines` + `Workflows` pages (H6) | render `harness_bus` stream + `projection.py` |

## 4. Locked decisions

1. **WS library: `websockets` (agent-only dep), WS-first with poll fallback.**
   `AgentRegistry` gains `attach_ws/detach_ws/dispatch_via_ws`; `dispatch()`
   tries WS, falls back to long-poll queue. `MAX_WS_PER_USER=5` mirrors polls.
   Runner (`agent/runner.py`) tries WS, falls back to `run_agent()` loop.
   Rationale: one well-tested dep isolated to `agent/`, keeps offline behavior
   identical.
2. **Durable bus: in-memory in H0, Postgres in H5.** H0 `harness_bus` is pure
   in-process (fast, no migration). H5 adds `workflow_events` table append
   alongside broadcast (needed for crash-resume + days-long approvals).
3. **LangChain: adapter-compatible interface, NOT a dependency.**
   `HarnessTool` fields match `StructuredTool`
   (`name/description/args_schema/func`) so
   `StructuredTool.from_function()` works downstream, but `core/` never imports
   `langchain`. Optional extra `harness-langchain` only if ever needed.
   Rationale: avoids heavy tree + 3.10–3.14 risk + Phase 10 no-new-deps rule.
4. **Tunneling: deferred to H6b.** H6a ships `code_search`, `process_list`,
   `screenshot` first. `expose_port` tunneling ships alone with its own
   threat-model + caps + audit. Rationale: tunneling needs a new multiplex
   route + public URL surface; isolate its risk.

## 5. Phases (each green: `ruff check . && pytest`)

### H0 — Events + bus (no migration, no protocol change)

- Add `core/harness_events.py`, `core/harness_bus.py`; wire
  `app.state.harness_bus` in `main.py` lifespan + dispose.
- Emit from `endpoints/mcp.py::_dispatch` (`tool.requested/completed/failed`)
  and `tool_executor.confirm_action` (`approval.requested/resolved` metadata
  only).
- Tests: `tests/test_harness_bus.py` (order, fan-out, history cap,
  no-secret leakage). Docs: none (internal only).

### H1 — WS relay + inspector stream

- `core/settings.py`: `harness_ws_enabled()` (default on when dep present),
  `harness_ws_heartbeat_seconds` (default 20).
- `core/agent_registry.py`: WS attach/detach, `dispatch_via_ws`, `online()`
  counts both transports.
- `endpoints/agents.py`: `websocket_endpoint /agent/ws` (`inv_` via header or
  `?api_key=`, close 4401 on fail); `websocket_endpoint /harness/events`
  (cookie realm, replay `history()` then live).
- `agent/runner.py`: `run_agent_ws()` + fallback; hello frame
  `{machine_id, machine_name, platform, capabilities}` persisted in
  `~/.invincible/config.json`.
- Tests: `tests/test_agent_ws.py` (auth, dispatch-over-WS, fallback,
  cross-user isolation, cap 429/4401). `docs/MCP_PROTOCOL.md` WS note (after
  code lands).

### H2 — Runtime spine + policy gate (L1+L3)

- Add `core/harness_runtime.py::run_workflow`,
  `core/harness_policy.py::before_tool_call()` (calls existing denylists +
  sandbox checks, no new patterns).
- Route `endpoints/mcp.py` machine-plane calls through `policy.check` before
  staging; keep `pending_confirmation` shapes byte-identical.
- Tests: policy unit (block reasons), round-trip shapes. `docs/SECURITY.md`
  policy section same PR.

### H3 — Hydrator + optional summarizer (L4)

- `harness_runtime.hydrate_context()` wraps `context_builder.assemble()`
  (continuity-first, unified budget). Add `INVINCIBLE_HARNESS_SUMMARIZER`
  (default `0`/off) LLM summarizer via existing BYOK `Router` (no second
  failover loop).
- Tests: budget pin (existing pattern), summarizer-off default,
  summarizer-on uses mocked transport (`httpx.MockTransport`).

### H4 — Agent handoff + supervisor (L5+L6)

- Add `core/harness_router.py` (`triage` without unconfirmed-bash vs
  `specialist` with it), `handoff` interception in `_dispatch` (typed
  `{to, reason}`, keeps conversation).
- ~~Added `core/harness_supervisor.py`~~ — removed in Step 5 (was never
  wired into a live caller; revisit in Step 8).
- Tests: `tests/test_harness_router.py` (handoff switch, fan-out
  isolation per `user_id`, partial failure). `tests/test_harness_supervisor.py`
  deleted with its module in Step 5.

### H4 follow-up — task-aware system prompts (merged in Step 5)

> Step 5 merged the two prompt bodies: the single source of truth is
> `webchat_agent.MODE_SYSTEM_PROMPTS` + `environment_note`. The duplicate
> `BASE_PROMPT` + read/do/plan overlays, `classify_task`,
> `render_env_block`, and `build_system_prompt` were deleted from
> `harness_router.py`; `harness_supervisor.py` (+ `PLAN_SYSTEM`,
> `build_subagent_prompt`) was deleted unwired — revisit in Step 8.

- `harness_router.py` (before Step 5): shared `BASE_PROMPT` plus
  read/do/plan overlays, `classify_task()` sniffing, `build_system_prompt()`
  assembling base → role+overlay → environment line. The static agent
  prompts were the per-agent defaults (triage→read, operator→do); tool
  tuples (unchanged, still registry-derived) are all that remains.
- `harness_supervisor.py` (before Step 5): `PLAN_SYSTEM` demanded a
  JSON-only plan; `build_subagent_prompt(agent, objective)` rendered the
  minimal per-step prompt so fan-out stayed cheap. `harness_runtime.py`
  summarizer keeps file paths, tool names, and failures (still terse).
- Not wired into a live caller yet: `run_workflow`'s `agent_next` contract
  is still `{task, turns}`, and the router stays off `/mcp` by design —
  binding happens with the H-later assistant.
- Tests: handoff cases, unknown-agent degrade, tool-membership pins.
  Classify/overlay/env-block cases deleted with the duplicate in Step 5.

### H5 — Durable approvals + durable log (L2+L7)

- `core/db.py` metadata + migrations: `0011_workflow_events` (`id,
  workflow_id, seq, type, payload JSONB, created_at float`),
  `0012_approval_suspend` (`suspended_workflow_id, deadline` on
  `pending_actions`). `invincible db upgrade` only; scratch-DB tests per
  `tests/test_cli_db.py` pattern.
- Add `core/harness_approvals.py::suspend/resume`;
  `harness_bus.emit` dual-writes (PG + broadcast). Keep 10-min
  `confirm_action` fast path; add days-long `approval_request/resolve` slow
  path surfaced on dashboard.
- Tests: crash-resume (kill mid-tool, resume no-duplicate), suspend days +
  resume, idempotency keys. `docs/SECURITY.md` + `docs/DEPLOYMENT.md` same PR.

### H6a — parity tools, safe set

- `code_search` (rg → walk fallback, respects `check_agent_read`),
  `process_list` (stdlib `ps`/`tasklist`, read-only no-confirm), `screenshot`
  (agent Chrome headless, base64 capped, `unavailable` when no Chrome). Each:
  `TOOLS` descriptor + `_dispatch` branch + `_agent_executor` path +
  `runner.execute_job` handler + sandbox/policy check + `_audit_action`.
- Agent hello `capabilities{ripgrep,docker,chrome,gpu}` (cheap
  `shutil.which`).
- Tests per tool (shapes, caps, blocks, offline/timeout).
  `docs/MCP_PROTOCOL.md` entries after landing.

### H6b — Tunneling (deferred, isolated)

- `expose_port{port}` staged + confirmed; agent multiplexes TCP over
  `WS /agent/ws`; server exposes `/{tunnel_token}/...` reverse route with
  per-user token auth, byte-per-second caps, audit rows, auto-expiry.
- Tests: auth, caps, expiry, cross-user denial. `docs/SECURITY.md` threat
  model + `docs/DEPLOYMENT.md` posture same PR.

### H6c — CLI + dashboard harness UX

- `cli.py`: `harness setup` (pair + print MCP config), `harness connect`
  (WS loop), `harness service install` (systemd/Windows service),
  `harness status` (account + machines + capabilities: `GET /agent/whoami`
  for account identity, `GET /agent/machines` for inventory).
- Dashboard: `Machines` page (per-machine online, capabilities, enable
  toggles in `user_settings`) + `Workflows` timeline (from `workflow_events`
  via `projection.py`). Cookie realm only.
- Tests: CLI pairing/status hermetic, dashboard realm/ownership pins.

## 6. File-by-file touch list (Step 5 deleted the supervisor + duplicate prompts)

- ADD: `core/harness_events.py`, `core/harness_bus.py`,
  `core/harness_runtime.py`, `core/harness_tools.py`,
  `core/harness_policy.py`, `core/harness_router.py`,
  `core/harness_approvals.py`,
  `tests/test_harness_*.py`, migrations `0011`, `0012`.
- DELETE (Step 5): `core/harness_supervisor.py` (+ its test) was never
  wired into a live caller — revisit in Step 8. The duplicate prompt
  assembly in `harness_router.py` (`BASE_PROMPT`, overlays,
  `classify_task`, `render_env_block`, `build_system_prompt`) was
  deleted; the live prompt is `webchat_agent.MODE_SYSTEM_PROMPTS`.
- EDIT: `main.py` (lifespan wiring only), `endpoints/agents.py` (add WS
  routes), `core/agent_registry.py` (add WS methods), `agent/runner.py` (add
  WS loop + hello), `endpoints/mcp.py` (add policy call + tools + handoff
  intercept), `core/settings.py` (add accessors), `core/db.py` (add tables H5
  only), `cli.py` (add `harness` group H6c), dashboard templates (H6c).
- NEVER: rename `core/router.py`, duplicate `_iter_attempts`, merge auth
  realms, auto-run migrations, pre-dump JSONB, log secrets.

## 7. Acceptance

- `pip install invincible-ai; invincible harness setup; invincible harness
  connect` → Cursor/Claude at `/mcp` gets memory tools immediately, machine
  tools once agent online; zero inbound ports on PC.
- WS kill → fallback poll works; server restart → agents re-register,
  in-flight documented-orphan (H0-H4) then durable-resume (H5).
- User A jobs never reach user B machines/sockets (pinned tests).
- `ruff check . && pytest` green every phase; `docs/*` updated in same PR as
  code (never ahead).

## 8. Risks

- Extra WS dep (`websockets`) — isolated to `agent/`, pinned, matrix-tested.
- `expose_port` public URL — mitigated by per-user tokens, caps, audit,
  expiry; hence deferred.
- `screenshot` needs Chrome on agent only — degrades to `unavailable`, never
  fails workflow.
- Durable `emit` doubles writes — H5 only, async fire-and-forget with memory
  as truth on failure (existing `PendingActionStore` pattern).
