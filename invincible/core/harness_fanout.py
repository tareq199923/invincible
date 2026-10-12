# invincible/core/harness_fanout.py
"""Read-only parallel fan-out (F04 reframe): a generalized explore sub-agent.

The single-explorer case (``harness_explore.run_explore``) covers a
machine-read spiral with one bounded context. This module covers the
remaining gap: a multi-PLANE investigation (machine reads + data-plane
reads) spread over many sequential rounds, where no single 4-consecutive
spiral trips the explorer and the within-round read batch
(``webchat_agent`` ``parallel_reads``) cannot help across rounds.

Shape (deliberately explore-like, not v1-supervisor-like):

- The main loop (``webchat_agent.run_agent_turn``) is the ONLY caller
  and the synthesizer: one planner call produces a validated plan, up
  to ``FANOUT_MAX_WIDTH`` bounded sub-agent loops run it in parallel
  via ``asyncio.gather``, and their findings merge into ONE system
  message the main loop continues with. There is no ``synthesize``
  provider call — an empty plan is a no-op costing exactly one call.
- Read-only by construction: the only specialists are ``scout``
  (machine reads, the explore set) and ``librarian`` (data-plane
  reads). Mutating, data-write, and agent-only tools are absent from
  the offered schemas AND refused at execution, so a hallucinating
  model can never act through fan-out.
- Same failover loop: every provider call goes through
  ``router.route_request_detailed`` with the caller's BYOK kwargs — a
  second attempt loop is forbidden.
- Per-step timeout is iteration caps, not a wall clock: each step runs
  at most ``FANOUT_MAX_ITERATIONS`` provider calls, each individually
  bounded by the router's own timeout resolution. A wall-clock cancel
  mid-dispatch risks orphaned agent jobs, so (like the main loop and
  the explorer) there is none.
- The plan is transient per turn: validated steps live in turn-local
  memory only. Durability rides the existing bus log (``PLAN_CREATED``
  with steps metadata → ``workflow_events``); nothing is written to
  ``task_states`` and no migration exists for this.

Layering: pure ``core/`` business logic — the router, executor, and
data stores are passed in (never imported as instances); only router
exception types are imported, like ``webchat_agent`` does.
"""
import asyncio
import json
import logging

from invincible.core import harness_tools, tool_dispatch
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.harness_router import Agent
from invincible.core.router import UpstreamClientError

logger = logging.getLogger("invincible.webchat.fanout")

# Fan-out width: matches the main loop's READ_BATCH_CONCURRENCY and
# keeps headroom under MAX_POLLS_PER_USER. Wider fans scale provider
# spend linearly with no evidence of benefit.
FANOUT_MAX_WIDTH = 4
# Per-step provider calls: a step answers one bounded objective, it
# never works a task to completion (compare EXPLORE_MAX_ITERATIONS).
FANOUT_MAX_ITERATIONS = 4
# Same stuck-loop trip point as the main loop (independent counter
# per step, same call_key semantics).
FANOUT_REPEAT_LIMIT = 3
# Hard word budget per step's findings (code-enforced truncation,
# not prompt obedience). Four steps merge into at most ~600 words.
FANOUT_FINDINGS_BUDGET = 150
# Trigger bar (see run_agent_turn): this many dispatched reads across
# at least FANOUT_TRIGGER_PLANES planes fires delegation. Below every
# lean task's profile, far below the drowning profile.
FANOUT_TRIGGER_READS = 6
FANOUT_TRIGGER_PLANES = 2

# Mechanical, code-shape instruction (not prompt doctrine): the JSON
# contract the caller parses. Prompt text lives in exactly one place
# (webchat_agent.MODE_SYSTEM_PROMPTS); this stays here because it
# defines the plan shape, like EXPLORE_SYSTEM_PROMPT defines the
# explorer's summary contract.
FANOUT_PLAN_SYSTEM = (
    "Decompose an investigation into independent read-only sub-tasks, "
    "one per area the task actually raises. Only include relevant "
    "areas. Reply with a JSON object only, no other text: "
    "'{\"steps\": [{\"id\": \"<short id>\", \"agent\": \"<name>\", "
    "\"objective\": \"<what this investigator should find out>\"}]}'. "
    "Every step needs a non-empty id, an agent from the valid list, "
    "and a concrete objective; the plan may be empty when there is "
    "nothing worth splitting. Investigators can only read — never "
    "plan a step that changes anything."
)

# Per-step mechanical shape: role line + bounded objective +
# findings-only reply contract. Sub-agents get the objective, not the
# full harness prompt — the main loop owns synthesis.
_FANOUT_STEP_ROLES = {
    "scout": (
        "You are a read-only machine investigator sub-agent. "
        "Inspect files, directories, code, git history, and processes; "
        "you cannot change anything."
    ),
    "librarian": (
        "You are a read-only context investigator sub-agent. "
        "Look up memories, projects, and task state; "
        "you cannot change anything."
    ),
}
_FANOUT_STEP_TAIL = (
    "Reply with your findings only - concise, with file:line evidence "
    "where relevant. Do not synthesize the whole task; "
    "the main agent merges findings."
)


def _step_prompt(agent_name: str, objective: str) -> str:
    role = _FANOUT_STEP_ROLES.get(
        agent_name, f"You are the {agent_name} specialist sub-agent.")
    return (
        f"{role} Objective: {objective.strip() or '(none given)'} "
        f"{_FANOUT_STEP_TAIL}"
    )


def _truncate_words(text: str, budget: int) -> str:
    words = str(text or "").split()
    if len(words) <= budget:
        return " ".join(words)
    return " ".join(words[:budget]) + "…"


SCOUT_AGENT = Agent(
    name="scout",
    tools=tuple(harness_tools.explore_names()),
)

LIBRARIAN_AGENT = Agent(
    name="librarian",
    tools=tuple(harness_tools.data_read_names()),
)

FANOUT_AGENTS: dict[str, Agent] = {
    SCOUT_AGENT.name: SCOUT_AGENT,
    LIBRARIAN_AGENT.name: LIBRARIAN_AGENT,
}

# Plane tags for the trigger: which specialist family a dispatched
# read belongs to. Agent-only reads (web_fetch/screenshot) count as
# breadth (they grow context) but no specialist owns them, so they
# tag a third plane rather than either step set.
_PLANE_BY_TOOL: dict[str, str] = {
    **{name: "machine" for name in SCOUT_AGENT.tools},
    **{name: "data" for name in LIBRARIAN_AGENT.tools},
    "web_fetch": "agent",
    "screenshot": "agent",
}


def plane_of(tool_name: str) -> str | None:
    """Plane tag for one dispatched tool (None when not a read)."""
    return _PLANE_BY_TOOL.get(tool_name)


def librarian_schemas() -> list[dict]:
    """Webchat-form function schemas for the librarian set, in webchat
    order. Same derivation as ``explore_schemas`` (descriptions
    stripped, ``required`` always emitted); fresh deep copies. Kept
    here — not in the golden-pinned registry — because only fan-out
    offers exactly this subset."""
    by_name = {s["function"]["name"]: s for s in harness_tools.webchat_schemas()}
    return [by_name[name] for name in LIBRARIAN_AGENT.tools]


def validate_plan(plan, agents: dict[str, Agent] | None = None) -> list[dict]:
    """Structural check on a planner-produced plan: keep steps with a
    non-empty id, a KNOWN agent, and a non-empty objective; drop the
    rest. Unknown-agent steps are dropped (the planner hallucinated a
    specialist), never executed."""
    agents = FANOUT_AGENTS if agents is None else agents
    if not isinstance(plan, dict):
        return []
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return []
    valid: list[dict] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = step.get("id")
        agent = step.get("agent")
        objective = step.get("objective")
        if (
            isinstance(sid, str) and sid
            and isinstance(agent, str) and agent in agents
            and isinstance(objective, str) and objective
        ):
            valid.append(
                {"id": sid, "agent": agent, "objective": objective})
    return valid


def parse_plan_text(text: str, agents: dict[str, Agent] | None = None) -> list[dict]:
    """Parse one planner reply into validated steps. Fenced or bare
    JSON both accepted; anything unparseable is an empty plan (the
    caller then no-ops), never an exception."""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        plan = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):
        return []
    return validate_plan(plan, agents)


async def make_plan(
    task: str,
    *,
    agents: dict[str, Agent] | None = None,
    complete,
) -> list[dict]:
    """Ask the planner for a plan, then validate it. ``complete(system,
    user)`` returns the raw reply text (the caller binds one provider
    call with no tools)."""
    agents = FANOUT_AGENTS if agents is None else agents
    raw = await complete(
        FANOUT_PLAN_SYSTEM,
        f"Task:\n{task}\n\nValid agents: {', '.join(sorted(agents))}.",
    )
    return parse_plan_text(raw, agents)


async def run_step(
    step: dict,
    *,
    router,
    model: str | None,
    session_id: str | None,
    session_pk,
    byok_kwargs: dict,
    executor,
    data_ctx: dict | None = None,
) -> dict:
    """Run one validated plan step in its own bounded context. Returns
    ``{"id": str, "agent": str, "summary": str, "calls": int}``
    (calls = provider calls made, for the turn's accounting).

    Router errors (no credentials / upstream client error / all
    providers failed) propagate: the caller maps them or falls back to
    no enrichment. Tool-level failures (blocked, malformed, unknown,
    refused non-read) become error results inside the step loop,
    exactly like the main loop.
    """
    agent_name = step["agent"]
    objective = step["objective"]
    if agent_name == "librarian":
        tools = librarian_schemas()
        read_names = set(LIBRARIAN_AGENT.tools)
        machine = False
    else:
        tools = harness_tools.explore_schemas()
        read_names = set(SCOUT_AGENT.tools)
        machine = True
    ctx = data_ctx or {}
    full = [
        {"role": "user", "content": objective},
        {"role": "system", "content": _step_prompt(agent_name, objective)},
    ]
    calls = 0
    iteration = 0
    repeat_key: str | None = None
    repeat_count = 0
    repeat_tripped = False

    while True:
        capped = iteration > FANOUT_MAX_ITERATIONS
        current_tools = tools if not capped and not repeat_tripped else None
        if capped or repeat_tripped:
            if capped:
                note = ("Search budget exhausted - report what you found, "
                        "no more tool calls.")
            else:
                note = ("Same tool call repeated - report what you found, "
                        "no more tool calls.")
            full = full + [{"role": "system", "content": note}]
        # Failover across providers happens INSIDE this call (the single
        # router loop); errors propagate to the caller for fallback.
        result, _info = await router.route_request_detailed(
            full, tools=current_tools, model=model,
            session_id=session_id, session_pk=session_pk,
            **byok_kwargs,
        )
        calls += 1
        choices = result.get("choices") or []
        message = choices[0].get("message") if choices else None
        if not isinstance(message, dict):
            raise UpstreamClientError(503, {"error": {
                "message": "gateway error", "type": "gateway_error"}})
        text = message.get("content")
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return {
                "id": step["id"], "agent": agent_name,
                "summary": _truncate_words(text or "", FANOUT_FINDINGS_BUDGET),
                "calls": calls,
            }
        full.append(message)
        for call in tool_calls:
            call_id = call.get("id") or f"fanout_{step['id']}_{iteration}"
            fname = ((call.get("function") or {}).get("name")) or ""
            raw_args = ((call.get("function") or {}).get("arguments")) or "{}"
            try:
                fargs = json.loads(raw_args) if isinstance(
                    raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError, ValueError):
                fargs = None
            if not isinstance(fargs, dict):
                result_doc = {
                    "status": "error",
                    "error": (
                        f"Tool arguments for '{fname}' were not valid "
                        f"JSON.{harness_tools.expected_args_hint(fname)}"
                    ) if fname else "Tool arguments were not valid JSON.",
                }
            elif fname not in read_names:
                # Read-only enforcement: anything outside this
                # specialist's subset is absent from the schemas AND
                # refused here, so a model that hallucinates a
                # mutating/data-write/agent-only tool can never
                # execute it through fan-out.
                valid = ", ".join(sorted(read_names))
                result_doc = {
                    "status": "error",
                    "error": f"Unknown tool: {fname}. "
                             f"Valid tools: {valid}.",
                }
            elif machine:
                result_doc = await tool_dispatch.run_read(
                    executor, fname, fargs,
                    agent_routed=executor is not None)
            else:
                result_doc, _ok = await tool_dispatch.run_data_tool(
                    fname, fargs,
                    principal=ctx.get("principal"),
                    mode=str(ctx.get("mode") or "manual"),
                    memory=ctx.get("memory"),
                    retrieval=ctx.get("retrieval"),
                    continuity=ctx.get("continuity"),
                    sessions=ctx.get("sessions"),
                    engine=ctx.get("engine"),
                )
            key = harness_tools.call_key(fname, fargs)
            if key == repeat_key:
                repeat_count += 1
            else:
                repeat_key, repeat_count = key, 1
            if repeat_count >= FANOUT_REPEAT_LIMIT:
                repeat_tripped = True
            full.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result_doc, ensure_ascii=False),
            })
        iteration += 1


async def run_fanout(
    task: str,
    *,
    router,
    model: str | None,
    session_id: str | None,
    session_pk,
    byok_kwargs: dict,
    executor,
    data_ctx: dict | None = None,
    bus: HarnessBus | None = None,
    workflow_id: str = "",
) -> dict:
    """Plan → parallel bounded steps → merged findings. Returns
    ``{"findings": str, "calls": int, "steps": [...]}`` (calls counts
    the planner call plus every step's provider calls, for the turn's
    accounting; steps is the validated plan, possibly empty).

    An empty/invalid plan is a no-op returning empty findings (one
    planner call spent, nothing else). A step failure is recorded
    (``subagent.failed``) and the rest still merge — degraded, never
    crashed. Router errors propagate for the caller to fall back on.

    Bus scope is deliberately narrow: ``PLAN_CREATED`` plus per-step
    ``SUBAGENT_STARTED/COMPLETED/FAILED`` (and a ``LOG`` on width cap)
    only. No ``WORKFLOW_*`` or ``MODEL_*`` events — fan-out runs
    INSIDE a turn, it is not a workflow, and the dashboard timeline
    must not gain a nested workflow per delegation.
    """
    async def _complete(system: str, user: str) -> str:
        result, _info = await router.route_request_detailed(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            tools=None, model=model,
            session_id=session_id, session_pk=session_pk,
            **byok_kwargs,
        )
        choices = result.get("choices") or []
        message = choices[0].get("message") if choices else None
        if not isinstance(message, dict):
            raise UpstreamClientError(503, {"error": {
                "message": "gateway error", "type": "gateway_error"}})
        return str(message.get("content") or "")

    steps = await make_plan(task, complete=_complete)
    calls = 1  # the planner call above
    if len(steps) > FANOUT_MAX_WIDTH:
        dropped = len(steps) - FANOUT_MAX_WIDTH
        steps = steps[:FANOUT_MAX_WIDTH]
        if bus is not None:
            bus.emit(
                HarnessEventType.LOG,
                workflow_id=workflow_id, level="info",
                message=f"fan-out width capped: {dropped} step(s) dropped",
            )
    if bus is not None:
        bus.emit(
            HarnessEventType.PLAN_CREATED,
            workflow_id=workflow_id, steps=steps,
        )
    if not steps:
        return {"findings": "", "calls": calls, "steps": []}

    async def _one(step: dict) -> dict | None:
        if bus is not None:
            bus.emit(
                HarnessEventType.SUBAGENT_STARTED,
                workflow_id=workflow_id, step_id=step["id"],
                agent=step["agent"], objective=step["objective"],
            )
        try:
            out = await run_step(
                step, router=router, model=model, session_id=session_id,
                session_pk=session_pk, byok_kwargs=byok_kwargs,
                executor=executor, data_ctx=data_ctx,
            )
        except Exception as exc:
            if bus is not None:
                bus.emit(
                    HarnessEventType.SUBAGENT_FAILED,
                    workflow_id=workflow_id, step_id=step["id"],
                    agent=step["agent"], error=str(exc)[:200],
                )
            return None
        if bus is not None:
            bus.emit(
                HarnessEventType.SUBAGENT_COMPLETED,
                workflow_id=workflow_id, step_id=step["id"],
                agent=step["agent"],
                findings_preview=str(out["summary"])[:200],
            )
        return out

    settled = await asyncio.gather(*(_one(s) for s in steps))
    merged: list[str] = []
    for step, out in zip(steps, settled, strict=True):
        if out is None:
            continue
        calls += out["calls"]
        if out["summary"]:
            merged.append(
                f"[{step['id']} via {out['agent']}] {out['summary']}")
    findings = "\n".join(merged)
    return {"findings": findings, "calls": calls, "steps": steps}


__all__ = [
    "FANOUT_AGENTS",
    "FANOUT_FINDINGS_BUDGET",
    "FANOUT_MAX_ITERATIONS",
    "FANOUT_MAX_WIDTH",
    "FANOUT_PLAN_SYSTEM",
    "FANOUT_REPEAT_LIMIT",
    "FANOUT_TRIGGER_PLANES",
    "FANOUT_TRIGGER_READS",
    "LIBRARIAN_AGENT",
    "SCOUT_AGENT",
    "librarian_schemas",
    "make_plan",
    "parse_plan_text",
    "plane_of",
    "run_fanout",
    "run_step",
    "validate_plan",
]
