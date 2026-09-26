# invincible/core/harness_supervisor.py
"""Hierarchical supervision (H4) — port of Hendrixer
`harness/supervisor.ts`.

Unlike a handoff (lateral, supervisor-less), the supervisor keeps control
the whole time: PLAN → dispatch sub-agents in parallel → fan in →
synthesize. The plan is a first-class artifact (emitted, inspectable,
survives via the bus history); partial failure degrades (a failed
sub-agent is recorded, the rest still synthesize) instead of crashing.

Dependency-injected like the rest of the harness: ``complete`` plans,
``investigate`` runs one sub-agent step, ``synthesize`` merges findings.
The caller binds them (H-later: plan/synthesize via the BYOK Router,
investigate via ``AgentRegistry.dispatch`` per owning user — isolation
stays structural). Unit tests supply fakes. This module never imports
the provider Router or any store.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.harness_router import AGENTS, Agent

PLAN_SYSTEM = (
    "Decompose a task into independent sub-tasks - one per area the task "
    "actually raises. Only include relevant areas. Reply with a JSON "
    "object only, no other text: "
    "'{\"steps\": [{\"id\": \"<short id>\", \"agent\": \"<name>\", "
    "\"objective\": \"<what this investigator should find out>\"}]}'. "
    "Every step needs a non-empty id, an agent from the valid list, and "
    "a concrete objective; the plan may be empty when there is nothing "
    "worth splitting."
)


def build_subagent_prompt(agent_name: str, objective: str) -> str:
    """Minimal per-step prompt for one fanned-out sub-agent (openclaw
    ``promptMode=minimal`` parity): one role line + the bounded
    objective + a findings-only reply contract.

    Sub-agents get the objective, not the full harness prompt - the
    supervisor owns synthesis, so findings stay small and cheap.
    Unknown agent names degrade to a generic specialist line instead
    of raising. Pure and hermetic."""
    if agent_name == "triage":
        role = (
            "You are the triage investigator sub-agent. "
            "Investigate with read-only tools; you cannot run commands "
            "or write files."
        )
    elif agent_name == "operator":
        role = (
            "You are the machine operator sub-agent. Inspect, then act "
            "through the normal staged tool flow, then verify."
        )
    else:
        role = f"You are the {agent_name} specialist sub-agent."
    return (
        f"{role} Objective: {objective.strip() or '(none given)'} "
        "Reply with your findings only - concise, with file:line "
        "evidence where relevant. Do not synthesize the whole task; "
        "the supervisor merges findings."
    )


def validate_plan(plan: Any, agents: dict[str, Agent]) -> list[dict]:
    """Structural check on a planner-produced plan: keep steps with a
    non-empty id, a KNOWN agent, and a non-empty objective; drop the rest.
    Unknown-agent steps are dropped (the planner hallucinated a
    specialist), never executed — same fail-safe as their zod enum."""
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


async def make_plan(
    task: str,
    *,
    agents: dict[str, Agent] = AGENTS,
    complete: Callable[[str, str], Awaitable[Any]],
) -> list[dict]:
    """Ask the planner for a plan, then validate it. ``complete(system,
    user)`` returns the raw plan object (parsed JSON from the caller's
    structured-output call)."""
    raw = await complete(
        PLAN_SYSTEM,
        f"Task:\n{task}\n\nValid agents: {', '.join(sorted(agents))}.",
    )
    return validate_plan(raw, agents)


async def run_supervisor(
    task: str,
    *,
    agents: dict[str, Agent] = AGENTS,
    investigate: Callable[[str, str], Awaitable[str]],
    synthesize: Callable[[str, list], Awaitable[str]],
    complete: Callable[[str, str], Awaitable[Any]],
    bus: HarnessBus | None = None,
    workflow_id: str = "",
) -> str:
    """Plan → parallel sub-agents → fan in → synthesize. Returns the final
    reply. A sub-agent failure is recorded (``subagent.failed``) and the
    rest still synthesize — degraded, never crashed."""
    if bus is not None:
        bus.emit(
            HarnessEventType.WORKFLOW_STARTED,
            workflow_id=workflow_id, input_preview=task[:200],
        )
    steps = await make_plan(task, agents=agents, complete=complete)
    if bus is not None:
        bus.emit(
            HarnessEventType.PLAN_CREATED,
            workflow_id=workflow_id, steps=steps,
        )

    async def _one(step: dict) -> dict | None:
        if bus is not None:
            bus.emit(
                HarnessEventType.SUBAGENT_STARTED,
                workflow_id=workflow_id, step_id=step["id"],
                agent=step["agent"], objective=step["objective"],
            )
        try:
            findings = await investigate(step["agent"], step["objective"])
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
                agent=step["agent"], findings_preview=str(findings)[:200],
            )
        return {"agent": step["agent"], "findings": findings}

    settled = await asyncio.gather(*(_one(s) for s in steps))
    findings = [r for r in settled if r is not None]
    reply = await synthesize(task, findings)
    if bus is not None:
        bus.emit(
            HarnessEventType.MODEL_COMPLETED,
            workflow_id=workflow_id, text_preview=str(reply)[:200],
        )
        bus.emit(
            HarnessEventType.WORKFLOW_COMPLETED,
            workflow_id=workflow_id, output_preview=str(reply)[:200],
        )
    return reply
