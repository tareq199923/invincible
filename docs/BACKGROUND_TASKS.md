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
| 4 | Mutating tools + durable approvals | **Planned** (F35) |
| 5 | Restart-safe task records (decision pending) | **Planned** |

Steps 1-3 are packaged together in the 0.6.2 release (migration
`0018`).

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

## Step 4 — mutating tools + durable approvals (planned)

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
| Slow | `pending_actions` row with `suspended_workflow_id` + `deadline` | up to 24h | A human answers later, possibly after a restart |

`core/harness_approvals.py` (H5) implements the slow path — and today it
has **no production caller**. Step 4 is that caller. It is not a new
approval system: it reuses `harness_approvals`, `pending_actions`, and
`tool_executor` as they are.

### Flow

```text
run_agent_turn(mode="manual")
  -> model calls a mutating tool
  -> tool_executor.prepare_staged_action(name, args)     # denylist FIRST
  -> ApprovalStore.suspend(workflow_id, action_type, args, owner_subject)
       -> a pending_actions row + approval.requested event
  -> the runner WAITS (task status: waiting for approval)
  -> owner approves from the dashboard
       -> ApprovalStore.resolve(token, approve=True)  -> staged record
       -> tool_executor.execute_staged(record, executor) -> real result
       -> approval.resolved event; the turn continues
```

### Decisions

**D1 — Modes.** `plan` stays the default and stays read-only. Step 4 adds
`manual` (mutating tools, approval-gated). `auto` (unattended mutating
execution) is **out of scope** and is not part of Step 4 or Step 5:
background execution without a human in the loop is a different security
posture and needs its own threat model.

**D2 — One execution path.** The execution half of
`tool_executor.confirm_action` is extracted into a reusable
`execute_staged(record, executor)`; the fast path and the slow path both
call it. The denylist check is extracted the same way into
`prepare_staged_action(name, args)` (validate + deny, no store), so the
slow path cannot stage anything the fast path would have blocked.

**D3 — Agent routing is required for mutating background tasks.** On a
hosted deployment, a confirmed background action executes on the **server
host** unless `INVINCIBLE_AGENT_ROUTING=1` routes it to the owner's
paired machine. Background tasks therefore **refuse** mutating tools when
agent routing is off (same "unavailable" answer the computer-use tools
already give), and the dashboard says why. A background task must never
be a way to run commands on the shared host.

**D4 — Ownership of the pending row.** `pending_actions` currently
carries the owner inside the JSONB `args` (`_OWNER_SUBJECT_KEY`), which
is fine for a single row but not for listing "my pending approvals".
Migration `0019` adds a nullable indexed `owner_user_id` column; the
slow-path `suspend` writes it, and the listing endpoint filters on it.

**D5 — Deny is a tool result, not a task failure.** A denial feeds
`{"status": "declined"}` back to the model exactly like the fast path,
so the model can adapt or explain. Only a timeout or a cancellation
fails the workflow.

**D6 — Bounded wait.** A background approval has its own TTL (default
**1 hour**, shorter than the 24h slow-path default because a suspended
task holds a slot). On expiry the runner deletes the row and fails the
workflow with a distinct "approval timed out" message.

**D7 — Cancelling cleans up.** Cancelling a suspended task deletes its
pending rows before the workflow fails, so no approval can later be
approved against a dead task.

**D8 — The cap.** `MAX_RUNNING_PER_USER = 2` counts every live task, **including a
suspended task waiting for approval**. A suspended task keeps its slot
until it is approved, rejected, cancelled, or its approval expires, so
there is no separate suspended allowance and no new registry state.
Answering or cancelling a waiting task frees its slot, so a user cannot
deadlock their own queue. Decision (project owner, 2026-11-18): a
separate allowance (`MAX_SUSPENDED_PER_USER`) was considered and
deliberately not adopted; revisit it only if waiting on approval proves
too blocking in practice.

**D9 — Events and audit.** Reuse the existing contract:
`approval.requested` and `approval.resolved` (both already emitted on
the webchat path) plus `workflow.failed` for a timeout. Audit rows are
`background_task.approval_requested` / `background_task.approval_resolved`
with metadata only: workflow id, tool name, decision — never the command,
path, or args.

**D10 — Single instance.** The wait is an in-process `asyncio.Event`
resolved by the dashboard endpoint (the server is single instance by
design). The **persisted row** is the durable record; a restart does not
resume the task (that is Step 5's decision).

### Surfaces (session realm only)

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
| A | `prepare_staged_action` + `execute_staged` extraction; `suspend` writes `owner_user_id` (migration `0019`) | unit pins; existing webchat/MCP behavior byte-identical |
| B | Runner: `manual` mode suspends on a mutating call and waits; approve resumes, deny feeds back, timeout fails | `tests/test_background_tasks.py` |
| C | Endpoints + dashboard list/approve/deny; owner scoping; identical 404s | `tests/test_background_tasks_ui.py` |
| D | Audit + events + `docs/SECURITY.md` + `docs/MCP_PROTOCOL.md` + roadmap | full suite green |

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
