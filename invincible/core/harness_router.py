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
"""
from __future__ import annotations

from dataclasses import dataclass, field

from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType

HANDOFF_TOOL = "handoff"


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
    system_prompt=(
        "You are a triage agent. Investigate with read_file, memory "
        "search/list, and task_state_get. You are NOT allowed to run "
        "commands or write files. If the task needs a machine change "
        "(run a command, write a file), hand off to the operator "
        "specialist with handoff({ to: \"operator\", reason }). Do not "
        "draft or describe the change yourself — let the operator take "
        "over. Handle what you can, then briefly summarize what you did."
    ),
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
    system_prompt=(
        "You are the machine operator specialist. Running commands and "
        "writing files IS your job — but every execute_bash/write_file "
        "is staged for human approval first, and the policy gate still "
        "blocks destructive patterns outright. When work arrives via "
        "handoff, ALWAYS do it: read the state, run the command or write "
        "the file through the normal staged flow, then briefly summarize "
        "what you did. Do not stop after only acknowledging — act."
    ),
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
            "task by calling the tools you need — do the work, don't just "
            "acknowledge the handoff."
        ),
    }
