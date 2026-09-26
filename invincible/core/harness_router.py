# invincible/core/harness_router.py
"""Agent routing + typed handoffs (H4) — port of Hendrixer
`harness/agents.ts` + the handoff interception in `harness/runtime.ts`.

An agent is data, not machinery: a name, a system prompt, and the subset
of tools it may use. The runtime (``harness_runtime.run_workflow``) runs
ANY agent through the same loop — adding a specialist never adds a loop.

Deliberate scope guard: this module is NOT wired into ``POST /mcp`` in
H4, so the twelve-tool contract in ``docs/MCP_PROTOCOL.md`` stays intact
(protocol compatibility is tested behavior). ``run_workflow`` callers and
the H-later assistant use ``handle_tool_call`` to intercept ``handoff``;
MCP exposure of handoff arrives with that assistant, not here.

The two built-ins mirror their triage/billing split, adapted to this
machine-plane: ``triage`` investigates (reads, memory, task state) but
cannot change anything; ``operator`` owns the privileged verbs.

Prompts are task-aware (MVP): one shared ``BASE_PROMPT`` (identity +
safety + loop discipline, in opencode's concise style) plus a per-task
overlay — read / do / plan — chosen by ``classify_task`` and assembled
by ``build_system_prompt``. The static ``Agent.system_prompt`` values
are the each-agent defaults (triage→read, operator→do); callers that
know the live facts pass ``build_system_prompt(agent, task,
model=..., cwd=..., date_str=...)`` into
``harness_runtime.hydrate_context(system_prompt=...)`` so the model
also sees the environment line (openclaw's volatile-last section).
Section order is stable on purpose: identity → role+overlay → env.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType

HANDOFF_TOOL = "handoff"

BASE_PROMPT = (
    "You are an Invincible harness agent running on the user's own "
    "machine. Be least-privilege: inspect before changing anything, "
    "and never send machine data off the machine. Work in a tool loop "
    "until done or blocked, then reply briefly with what you did plus "
    "file:line evidence. Keep replies concise - a few lines unless "
    "asked for detail."
)

_TRIAGE_ROLE = "You are the triage investigator."
_OPERATOR_ROLE = "You are the machine operator specialist."

READ_OVERLAY = (
    "Investigate with read_file, memory_search/list, and task_state_get. "
    "You cannot run commands or write files - those tools are absent, "
    "not merely forbidden. If the task needs a machine change, hand off "
    "with handoff({\"to\": \"operator\", \"reason\": \"...\"}) and stop; "
    "do not draft the change yourself."
)

DO_OVERLAY = (
    "Inspect, then act, then verify. Running commands and writing files "
    "IS your job. Each execute_bash/write_file call is staged for human "
    "approval and the policy gate still blocks destructive patterns: "
    "call the tool, briefly say what will happen, and wait for the "
    "result. Risk is enforced at runtime by those gates - do not "
    "pre-refuse requested work and do not seek extra permission beyond "
    "the staged flow. When work arrives via handoff, do it: read state, "
    "run the command or write the file, verify afterwards, then "
    "summarize. Never stop after only acknowledging."
)

PLAN_OVERLAY = (
    "Produce a concrete step-by-step plan grounded in what you read, "
    "then stop. Use read-only tools only. Never claim an action was "
    "taken; end with the plan."
)

TASK_KINDS = ("read", "do", "plan")

_PLAN_WORDS = (
    "plan", "outline", "steps", "step-by-step", "design",
    "proposal", "roadmap", "strategy",
)
_DO_WORDS = (
    "run", "execute", "fix", "write", "create", "delete", "remove",
    "update", "change", "install", "deploy", "restart", "patch",
    "implement", "apply", "move", "rename", "build",
)
_PLAN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _PLAN_WORDS) + r")\b",
    re.IGNORECASE,
)
_DO_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _DO_WORDS) + r")\b",
    re.IGNORECASE,
)


def classify_task(task: str) -> str:
    """Sniff the task kind: ``plan`` wins over ``do``, else ``read``.

    Keyword-only on purpose (cheap, deterministic, no LLM call): a
    task mentioning planning words wants a plan even when it also
    names a change ("plan the fix"); a task naming a change wants
    action; everything else is investigation. Pure and hermetic.
    """
    if _PLAN_RE.search(task or ""):
        return "plan"
    if _DO_RE.search(task or ""):
        return "do"
    return "read"


def render_env_block(
    *, model: str = "", cwd: str = "", date_str: str = ""
) -> str:
    """One volatile-last environment line (openclaw temporal-context
    parity): model, working directory, local date. Empty facts render
    as ``unknown`` (the server never guesses another machine's cwd);
    an empty ``date_str`` falls back to today. Pure and hermetic."""
    day = date_str.strip() or datetime.date.today().isoformat()
    return (
        f"Environment: model={model.strip() or 'unknown'} | "
        f"cwd={cwd.strip() or 'unknown'} | date={day}."
    )


def build_system_prompt(
    agent_name: str,
    task: str,
    *,
    model: str = "",
    cwd: str = "",
    date_str: str = "",
) -> str:
    """Assemble the per-turn system prompt: base + role/overlay + env.

    The overlay follows the TASK kind (read/do/plan), the role line
    follows the agent; unknown agent names degrade to a generic
    specialist line instead of raising. Section order is fixed
    (identity → role+overlay → env) so prompt-cache prefixes stay
    stable across turns. Pure and hermetic.
    """
    kind = classify_task(task)
    overlay = {
        "read": READ_OVERLAY, "do": DO_OVERLAY, "plan": PLAN_OVERLAY,
    }[kind]
    if agent_name == "triage":
        role = _TRIAGE_ROLE
    elif agent_name == "operator":
        role = _OPERATOR_ROLE
    else:
        role = f"You are the {agent_name} specialist."
    return (
        f"{BASE_PROMPT} {role} {overlay} "
        f"{render_env_block(model=model, cwd=cwd, date_str=date_str)}"
    )


@dataclass(frozen=True)
class Agent:
    """One specialist: prompt + allowed tool subset."""

    name: str
    system_prompt: str
    tools: tuple[str, ...] = field(default_factory=tuple)

    def allows(self, tool_name: str) -> bool:
        return tool_name in self.tools


TRIAGE_AGENT = Agent(
    name="triage",
    system_prompt=f"{BASE_PROMPT} {_TRIAGE_ROLE} {READ_OVERLAY}",
    tools=(
        "read_file",
        "task_state_get",
        "memory_search",
        "memory_list",
        HANDOFF_TOOL,
    ),
)

OPERATOR_AGENT = Agent(
    name="operator",
    system_prompt=f"{BASE_PROMPT} {_OPERATOR_ROLE} {DO_OVERLAY}",
    tools=(
        "read_file",
        "execute_bash",
        "write_file",
        "task_state_set",
        "task_state_get",
        "checkpoint_create",
    ),
)

AGENTS: dict[str, Agent] = {
    TRIAGE_AGENT.name: TRIAGE_AGENT,
    OPERATOR_AGENT.name: OPERATOR_AGENT,
}


class UnknownAgent(Exception):
    """Handoff target names no registered agent."""


def resolve(agents: dict[str, Agent], name: str) -> Agent:
    """Look up an agent by name (KeyError-free: raises UnknownAgent
    listing the valid names)."""
    try:
        return agents[name]
    except KeyError:
        valid = ", ".join(sorted(agents))
        raise UnknownAgent(
            f"Unknown agent: {name!r}. Valid agents: {valid}."
        ) from None


def handle_tool_call(
    agents: dict[str, Agent],
    current: Agent,
    tool_name: str,
    args: dict | None,
    *,
    bus: HarnessBus | None = None,
    workflow_id: str = "",
) -> tuple[Agent, dict | None]:
    """Intercept the handoff tool; pass everything else through.

    Returns ``(agent, result)``: for non-handoff calls the same agent and
    None (the caller proceeds to normal execution). For ``handoff`` the
    new agent and a structured tool-result dict to feed back to the model
    — the harness switches the running agent laterally, keeping the
    conversation, exactly like their runtime.
    """
    if tool_name != HANDOFF_TOOL:
        return current, None
    argv = args or {}
    to = str(argv.get("to", ""))
    reason = str(argv.get("reason", ""))
    try:
        new_agent = resolve(agents, to)
    except UnknownAgent as exc:
        if bus is not None:
            bus.emit(
                HarnessEventType.AGENT_HANDOFF,
                workflow_id=workflow_id, from_agent=current.name,
                to_agent=to, reason=f"rejected: {exc}",
            )
        return current, {"ok": False, "message": str(exc)}
    if bus is not None:
        bus.emit(
            HarnessEventType.AGENT_HANDOFF,
            workflow_id=workflow_id, from_agent=current.name,
            to_agent=to, reason=reason,
        )
    return new_agent, {
        "ok": True,
        "message": (
            f"You are now the {to} specialist. Take over and FINISH the "
            "task by calling the tools you need - do the work, verify "
            "the result, then summarize briefly. Don't just acknowledge "
            "the handoff."
        ),
    }
