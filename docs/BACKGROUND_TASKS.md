# Background tasks — design and status

Background tasks let a logged-in user start an **agent turn that runs on
the server with no browser attached**. This document is the status and
design record for the feature: what ships, what the next steps are, and
the decisions each step makes.

Status legend: **Shipped** (code + tests + docs landed) · **Planned**
(designed here, not started).

| Step | Scope | State |
|---|---|---|
| 1 | Plan-mode task runner (server-side, no browser) | **Shipped** (F32) |
| 2 | Dashboard UI: start, status, result page, Stop | **Shipped** (F33) |
| 3 | Durable per-tool-call checkpoints (progress tracking) | **Shipped** (F34) |
| 4 | Mutating tools + durable approvals | **In progress** (F35 — A shipped, B implemented) |
| 5 | Restart-safe task records (decision pending) | **Planned** |

Steps 1-3 are packaged together in the 0.6.2 release (migration
`0018`).

Step 4 is landing in slices. Slice A (the durable groundwork) is
shipped; Slice B (the runner's suspend/approve/deny/timeout path) is
implemented in this change. The dashboard surfaces (Slice C) and the
final audit/docs pass (Slice D) are still planned.

---

## How it works today (Steps 1-3)

```text
POST /dashboard/background-tasks   (cookie realm)
  -> validate task/model/mode, resolve BYOK, enforce the per-user cap
  -> registry.start(user_id, run_background_task(...))   # in-process
       -> chat_service.prepare_chat(...)                # one chat session
       -> webchat_agent.run_agent_turn(mode="plan")      # SAME loop as webchat
            -> core/router.py::_iter_attempts           # THE failover loop
       -> harness_bus: workflow.started / completed / failed
       -> the answer lands in the chat session whose id IS the workflow id
```

Rules that must not drift:

- **One failover loop.** Background tasks call `prepare_chat` +
  `run_agent_turn`; they never re-implement attempt sequencing.
- **Plan mode is read-only by construction.** The model is offered read
  tools only. A mutating call is denied immediately
  (`"approval not supported yet"`) and the staged token is popped.
- **Metadata only.** Events carry previews (200 chars) and tool names.
  Audit rows carry the workflow id and mode — never task text, commands,
  paths, or results.
- **Owner-scoped.** A foreign workflow id is indistinguishable from an
  unknown one (identical 404 body).
- **Single instance.** The registry, the running cap, and the in-process
  task handles live in the worker (see `docs/DEPLOYMENT.md`).

### Step 3 — durable checkpoints

`background_task_checkpoints` (migration `0018`) records one row per
**successful** tool call: tool name, call id, SHA-256 of the args, result
size. Never args, paths, commands, or result content.

This is **progress tracking, not resume**. Nothing replays a tool call,
and a lost checkpoint never fails a task (the checkpointer is
best-effort). Step 5 is where "what happens after a restart" is decided.

---

## Step 4 — mutating tools + durable approvals (Slices A/B)

### Goal

A background task may call a **mutating** tool (`execute_bash`,
`write_file`, `edit_file`, and the agent-routed computer-use tools). The
action is **staged, not executed**: the task suspends until the owner
approves or denies it from the dashboard.

### Why the slow approval path exists

Two approval paths already exist in the codebase and are deliberately
separate:

| Path | Store | Lifetime | User |
|---|---|---|---|
| Fast | `PendingActionStore` (in-memory unless `INVINCIBLE_PERSIST_PENDING_ACTIONS=1`) | 10 minutes | A browser holds the request open (webchat manual mode, `/mcp`) |
| Slow | `pending_actions` row with `suspended_workflow_id` + `deadline` | up to 24h (Step 4 passes 1h) | A human answers later, possibly after a restart |

`core/harness_approvals.py` (H5) implements the slow path — until Step 4
it had no production caller. Slice B wired the first one: a `manual`
background task suspends on that path instead of the browser fast path.
It is not a new approval system: it reuses `harness_approvals`,
`pending_actions`, and `tool_executor` as they are.

### Flow

```text
run_agent_turn(mode="manual", durable=DurableApprovalAdapter)
  -> model calls a mutating tool
  -> tool_executor.staged_call_args(name, args)          # schema scrub
  -> tool_executor.prepare_staged_action(name, args)     # denylist FIRST
  -> ApprovalStore.suspend(workflow_id, action_type, args,
                           owner_subject, ttl_seconds=3600)
       -> one pending_actions row + approval.requested event
       -> ApprovalWaiter.register(token) BEFORE the runner yields
  -> the runner WAITS on the in-process rendezvous
  -> owner approves from the dashboard (Slice C)
       -> ApprovalStore.resolve(token, approve=True) atomically claims
          the row (DELETE ... RETURNING) -> staged record
       -> tool_executor.execute_staged(record, executor,
                                       require_executor=True)
       -> approval.resolved event; the turn continues
  -> every exit (approve, deny, timeout, cancel, error) purges any
     remaining row for this workflow
```

### Decisions

**D1 — Modes.** `plan` stays the default and stays read-only. Step 4 adds
`manual` (mutating tools, approval-gated). `auto` (unattended mutating
execution) is **out of scope** and is not part of Step 4 or Step 5:
background execution without a human in the loop is a different security
posture and needs its own threat model. Core enforces the set:
`_execute_task` raises `BackgroundTaskError` for any mode other than
`plan` or `manual`, before preparation runs (the endpoint also rejects
it).

**D2 — One execution path.** `execute_staged(record, executor, *,
require_executor=False)` is the single execution half (extracted from
`confirm_action` in `b883560`; Slice A added `require_executor`). Slice B
extracted the validation half into `prepare_staged_action(name, args)`
(validate + deny, no store) and the wire response into
`staged_response(record, token)`. Both fast-path writers (webchat's
`tool_dispatch.stage_mutating` and the `/mcp` staging helpers) and the
slow path call them; the slow path passes `require_executor=True`.
`stage_mutating` and the slow path also scrub model args through
`staged_call_args(name, args)` before validating: `execute_bash` keeps
only `command`, because its 30s command timeout is a server default,
never a model-supplied value (the `/mcp` handler never passes one). The
agent loop sees a narrow `DurableApprovalSource` seam (`stage` / `wait` /
`resolve`) and never talks to `pending_actions` directly.

**D3 — Agent routing is required for mutating background tasks.** On a
hosted deployment, a confirmed background action executes on the **server
host** unless `INVINCIBLE_AGENT_ROUTING=1` routes it to the owner's
paired machine. Background tasks therefore **refuse** mutating tools when
agent routing is off: the slow path calls
`execute_staged(..., require_executor=True)`, which answers `unavailable`
and runs nothing when no executor exists (`build_agent_executor` returns
`None` when routing is off). A background task must never be a way to
run commands on the shared host.

**D4 — Ownership of the pending row.** `pending_actions` carries the
owner inside the JSONB `args` (`_OWNER_SUBJECT_KEY`), stripped on
resolve. That stayed as-is: neither slice needed a schema change, so
there is **no migration `0019`**. Owner-scoped listing and purge filter
on the same key. A dedicated indexed `owner_user_id` column is not part
of Step 4.

**D5 — Deny is a tool result, not a task failure.** A denial feeds
`{"status": "declined"}` back to the model exactly like the fast path,
so the model can adapt or explain. Only a timeout or a cancellation
fails the workflow.

**D6 — Bounded wait.** A background approval has its own TTL,
`BACKGROUND_APPROVAL_TTL_SECONDS = 3600`, passed explicitly to
`ApprovalStore.suspend` (the store's default stays 24h because a
suspended task holds a slot). On expiry the waiter answers `None`, the
runner deletes the row, and the workflow fails with the distinct
`"approval timed out"` message.

**D7 — Cancelling cleans up.** Cancelling a suspended task deletes its
pending rows before the workflow fails, so no approval can later be
approved against a dead task. The same `finally` purge runs on every
other exit: approve, deny, timeout, error, and normal completion.

**D8 — The cap.** `MAX_RUNNING_PER_USER = 2` counts every live task, **including a
suspended task waiting for approval**. A suspended task keeps its slot
until it is approved, rejected, cancelled, or its approval expires, so
there is no separate suspended allowance and no new registry state.
Answering or cancelling a waiting task frees its slot, so a user cannot
deadlock their own queue. Decision (project owner, 2026-11-18): a
separate allowance (`MAX_SUSPENDED_PER_USER`) was considered and
deliberately not adopted; revisit it only if waiting on approval proves
too blocking in practice.

**D9 — Events and audit.** Slice B emits `approval.requested` and
`approval.resolved` on the existing workflow bus, metadata only (tool
name and decision — never the command, path, args, or content). A
timeout emits `workflow.failed`. The blocked-tool path audits
`background_task.tool.blocked` with the tool name and call id only.
The remaining audit rows
(`background_task.approval_requested` / `_resolved`) belong to Slice D.

**D10 — Single instance.** The wait is an in-process future in
`ApprovalWaiter` (the same rendezvous webchat uses), resolved by the
dashboard endpoint; the server is single instance by design. The
**persisted row** is the durable record; a restart does not resume the
task (that is Step 5's decision) and startup purges orphaned suspended
rows.

### Surfaces (session realm only — Slice C, not shipped)

None of these routes exist yet; Slice B is runner-only.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/dashboard/background-tasks/{workflow_id}/approvals` | Owner-scoped list of pending approvals (metadata only) |
| `POST` | `/dashboard/background-tasks/{workflow_id}/approvals/{token}` | `{"approve": true\|false}` → 202; identical 404 for foreign / unknown / already-resolved |
| `GET` | `/dashboard/approvals` | The same list across the owner's workflows (the "waiting for you" page) |

The `inv_` API key realm never reaches these routes, and a foreign
workflow id returns the same 404 body as an unknown one.

### Slices

| Slice | Work | Gate |
|---|---|---|
| A | Durable groundwork: atomic single-use `ApprovalStore.resolve`, owner-scoped listing/purge, startup purge, `execute_staged(require_executor=True)` | **Shipped** (`d9852ec`); unit pins |
| B | Shared validation extraction (`staged_call_args` / `prepare_staged_action` / `staged_response`) + runner `manual` lifecycle: suspend, wait, approve, deny, timeout, cancel, purge; metadata-only events | **Implemented** (this change); `tests/test_background_tasks.py` |
| C | Endpoints + dashboard list/approve/deny; owner scoping; identical 404s | Planned; `tests/test_background_tasks_ui.py` |
| D | Audit + `docs/SECURITY.md` + `docs/MCP_PROTOCOL.md` + roadmap; final docs pass | Planned; full suite green |

---

## Step 5 — restart-safe task records (decision pending)

Step 3 records what a task *did*; it does not record that a task
*exists*. Today a background task lives only in the worker: after a
restart, running tasks vanish and the Workflows page only shows the
persisted events.

Two candidate scopes:

**Option A (recommended) — durable task records, no replay.** A
`background_tasks` table (workflow id, owner, status, mode, session id,
timestamps; the task text stays in the chat session) makes the task
list honest across restarts. On startup, tasks still marked `running`
are marked `interrupted`, the owner sees them with a **Retry** button
that starts a *new* task, and nothing is ever replayed automatically.
Conservative: no tool call is ever executed twice, and the model is
never re-prompted with a half-finished turn.

**Option B — real resume.** Reconstruct the run from the session and the
checkpoints and continue it. Larger promise: it must decide what to do
with a tool call that was *started* but not recorded (checkpoints are
written on success only), and it must survive the 30-minute wall clock
and the provider failover semantics.

Recommendation: **Option A**. It makes the state honest and keeps the
"never execute twice" invariant. Option B can follow as its own feature
if the product wants it.

### Prerequisites either way

- A durable task record is the precondition for both options.
- The task text itself stays in the chat session (it is already
  persisted there); the task row stays metadata-only, matching the
  project-wide secrets discipline.

---

## Out of scope

- `auto` mode for background tasks (see D1).
- Multi-instance / distributed task execution (the platform is
  single-instance by design).
- Email or webhook notifications for a pending approval (no mail
  infrastructure exists; the dashboard is the surface).
- Replaying tool calls from checkpoints.
