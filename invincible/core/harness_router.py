# invincible/core/harness_router.py
"""Agent routing + typed handoffs (H4).

An agent is data, not machinery: a name and the subset of tools it may
use. The runtime (``harness_runtime.run_workflow``) runs ANY agent
through the same loop — adding a specialist never adds a loop.

Deliberate scope guard: this module is NOT wired into ``POST /mcp``
(protocol compatibility is tested behavior). ``run_workflow`` callers
and the H-later assistant use ``handle_tool_call`` to intercept
``handoff``; MCP exposure of handoff arrives with that assistant, not
here.

The two built-ins mirror their triage/billing split, adapted to this
machine-plane: ``triage`` investigates (reads, memory, task state) but
cannot change anything; ``operator`` owns the privileged verbs.

Prompts live in exactly one place (Step 5 merge):
``webchat_agent.MODE_SYSTEM_PROMPTS`` + ``environment_note``. The former
BASE_PROMPT + read/do/plan overlays, ``classify_task``,
``render_env_block``, and ``build_system_prompt`` were deleted here as
semantically duplicate — the live permission-mode axis (plan/manual/auto)
never mapped 1:1 onto the old intent-kind axis (read/do/plan), and the old
DO_OVERLAY ("do not seek extra permission") contradicted manual mode's
approval gate. A caller that knows the live facts passes the composed
live prompt into ``harness_runtime.hydrate_context(system_prompt=...)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from invincible.core import harness_tools
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType

HANDOFF_TOOL = "handoff"


@dataclass(frozen=True)
class Agent:
    """One specialist: name + allowed tool subset."""

    name: str
    tools: tuple[str, ...] = field(default_factory=tuple)

    def allows(self, tool_name: str) -> bool:
        return tool_name in self.tools


TRIAGE_AGENT = Agent(
    name="triage",
    tools=harness_tools.router_tools("triage"),
)

OPERATOR_AGENT = Agent(
    name="operator",
    tools=harness_tools.router_tools("operator"),
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
