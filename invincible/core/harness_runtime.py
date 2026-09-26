# invincible/core/harness_runtime.py
"""Durable agent loop spine (H2) + context hydration (H3) — port of
Hendrixer `harness/runtime.ts` + `harness/memory.ts`.

The spine of the whole harness program:

```python
result = await run_workflow(
    workflow_id, task,
    agent_next=...,    # the LLM decides the next step (injected)
    execute_step=...,  # tools / handoff / approval (injected)
    policy=...,        # harness_policy.before_tool_call (injected)
    checkpoint=...,    # continuity checkpoint (injected, optional)
    bus=...,           # HarnessBus event stream (injected, optional)
)
```

Deliberately dependency-injected: this module never imports the provider
``Router``, the continuity engine, or any store (see AGENTS.md layering —
``compat/`` never imports ``Router``; business logic lives in ``core/``
but stays decoupled). H4 (router/supervisor) and the compat endpoints
supply the callables; unit tests supply fakes.

Semantics (Hendrixer parity):
- ``agent_next(context)`` returns ``{"text": str, "tool_calls": [...]}``.
  Empty ``tool_calls`` means done → ``workflow.completed``.
- Policy denials (``ToolBlocked``) become structured ``tool.failed``
  results the agent can self-correct from — the loop continues, it never
  crashes the workflow (their L3 "structured errors" lesson).
- Unexpected executor exceptions also become ``tool.failed`` results.
- ``max_steps`` overflow → ``workflow.failed`` (their ``MAX_STEPS``).

H3 adds the memory half of their `memory.ts`:
- ``hydrate_context`` assembles what the model sees THIS turn (system
  prompt + pinned task + running summary + injection blocks from
  ``context_builder.assemble`` + recent turns verbatim). History (the
  durable event log) and state (the summary) stay separate — context is
  a runtime decision, not a chat log.
- ``summarize_turns`` / ``compact_turns`` fold old turns into the running
  summary through an injected ``complete`` callable (the caller binds the
  one BYOK ``Router`` — never a second failover loop). The LLM summarizer
  is opt-in via ``INVINCIBLE_HARNESS_SUMMARIZER``; the relay digest in
  ``core/relay.py`` stays the cheap default path.
- Every step emits on the bus (metadata only — the bus contract forbids
  secrets; callers must not put commands/contents in).
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from invincible.core import tool_executor
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType

MAX_STEPS = 30


async def _maybe_await(fn: Callable, *args: Any) -> Any:
    result = fn(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def run_workflow(
    workflow_id: str,
    task: str,
    *,
    agent_next: Callable[[dict], Awaitable[dict]],
    execute_step: Callable[[str, dict], Awaitable[dict]],
    policy: Callable | None = None,
    checkpoint: Callable | None = None,
    bus: HarnessBus | None = None,
    max_steps: int = MAX_STEPS,
) -> str:
    """Run one workflow to completion. Returns the final text ("" on
    step-limit failure). Never raises for tool-level failures; raises
    only when the harness itself is miswired (agent_next/execute_step
    raising non-tool errors is captured per-call, but checkpoint errors
    propagate — a checkpoint that can't persist must not be silent)."""
    if bus is not None:
        bus.emit(
            HarnessEventType.WORKFLOW_STARTED,
            workflow_id=workflow_id, input_preview=task[:200],
        )
    turns: list = []
    step = 0
    while step < max_steps:
        context = {"task": task, "turns": list(turns)}
        turn = await agent_next(context)
        tool_calls = turn.get("tool_calls") or []
        text = turn.get("text", "")
        if not tool_calls:
            if bus is not None:
                bus.emit(
                    HarnessEventType.MODEL_COMPLETED,
                    workflow_id=workflow_id, text_preview=str(text)[:200],
                )
                bus.emit(
                    HarnessEventType.WORKFLOW_COMPLETED,
                    workflow_id=workflow_id, output_preview=str(text)[:200],
                )
            return str(text)
        turn_results: list = []
        for call in tool_calls:
            tool_call_id = str(call.get("tool_call_id", ""))
            tool_name = str(call.get("tool_name", ""))
            tool_input = call.get("input") or {}
            if policy is not None:
                try:
                    await _maybe_await(policy, tool_name, tool_input)
                except tool_executor.ToolBlocked as blocked:
                    if bus is not None:
                        bus.emit(
                            HarnessEventType.TOOL_FAILED,
                            workflow_id=workflow_id,
                            tool_call_id=tool_call_id, name=tool_name,
                            error=f"blocked: {blocked.reason}",
                        )
                    turn_results.append({
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "result": {
                            "status": "blocked", "reason": blocked.reason,
                        },
                    })
                    continue
            if bus is not None:
                bus.emit(
                    HarnessEventType.TOOL_REQUESTED,
                    workflow_id=workflow_id,
                    tool_call_id=tool_call_id, name=tool_name,
                )
            try:
                output = await execute_step(tool_name, tool_input)
            except Exception as exc:
                if bus is not None:
                    bus.emit(
                        HarnessEventType.TOOL_FAILED,
                        workflow_id=workflow_id,
                        tool_call_id=tool_call_id, name=tool_name,
                        error=str(exc)[:200],
                    )
                turn_results.append({
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "result": {"status": "error", "error": str(exc)[:500]},
                })
                continue
            if bus is not None:
                bus.emit(
                    HarnessEventType.TOOL_COMPLETED,
                    workflow_id=workflow_id,
                    tool_call_id=tool_call_id, name=tool_name,
                    status=str((output or {}).get("status", "ok")),
                )
            turn_results.append({
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "result": output,
            })
            if checkpoint is not None:
                await _maybe_await(checkpoint, workflow_id, output)
        turns.append(turn_results)
        step += 1
    if bus is not None:
        bus.emit(
            HarnessEventType.WORKFLOW_FAILED,
            workflow_id=workflow_id,
            error=f"Hit the {max_steps}-step limit without finishing.",
        )
    return ""


# --- context hydration + summarization (H3) -------------------------------

# Token budget, not turn count, drives context bloat (their memory.ts
# lesson). Kept small so compaction engages on short tasks; callers with
# bigger windows pass explicit budgets.
MAX_CONTEXT_TOKENS = 3000
KEEP_CONTEXT_TOKENS = 1500

SUMMARIZER_SYSTEM = (
    "You compress an agent's work log into a short running summary. "
    "Preserve concrete facts: file paths, tool names, item ids, "
    "categories, draft ids, amounts, what failed, and what was already "
    "sent. Be terse."
)


def hydrate_context(
    task: str,
    summary: str = "",
    turns: list | None = None,
    *,
    system_prompt: str = "",
    injections: list | None = None,
) -> list[dict]:
    """Assemble what the model sees THIS turn (Hendrixer `buildContext`
    parity), over the internal message model:

    system prompt → pinned task → summary → injection blocks (the output
    of ``context_builder.assemble``: continuity brief first, memories
    second, already budget-fitted) → recent turns verbatim.

    The task is pinned, never summarized away. The system prompt is
    typically ``harness_router.build_system_prompt(...)`` (base +
    task overlay + environment line, volatile facts last). Pure and
    hermetic.
    """
    context: list[dict] = []
    if system_prompt:
        context.append({"role": "system", "content": system_prompt})
    context.append({"role": "user", "content": task})
    if summary:
        context.append({
            "role": "system",
            "content": f"Summary of earlier work so far:\n{summary}",
        })
    for msg in injections or []:
        context.append(msg)
    for turn in turns or []:
        context.extend(turn)
    return context


def _flat_tokens(turns: list) -> int:
    """Estimated tokens over recent-turn message lists."""
    from invincible.core.trimming import estimate_tokens

    return sum(
        estimate_tokens(m) for turn in turns for m in turn
        if isinstance(m, dict)
    )


def _render_transcript(old_turns: list) -> str:
    """Flatten compacted turns to text for the summarizer (capped)."""
    import json

    lines = []
    for turn in old_turns:
        for m in turn:
            if not isinstance(m, dict):
                continue
            content = m.get("content", "")
            text = (
                content if isinstance(content, str)
                else json.dumps(content, default=str)
            )
            lines.append(f"{m.get('role', '?')}: {text}")
    return "\n".join(lines)[:6000]


async def summarize_turns(
    old_turns: list,
    prior_summary: str,
    *,
    complete: Callable[[str, str], Awaitable[str]],
) -> str:
    """Fold compacted turns into the running summary via one injected LLM
    call. ``complete(system, user)`` is bound by the caller to the one
    BYOK Router — this module never routes providers itself."""
    transcript = _render_transcript(old_turns)
    user = (
        f"Prior summary:\n{prior_summary or '(none)'}\n\n"
        f"Fold in this newer work:\n{transcript}\n\n"
        "Return the updated summary."
    )
    return await complete(SUMMARIZER_SYSTEM, user)


async def compact_turns(
    turns: list,
    summary: str,
    *,
    complete: Callable[[str, str], Awaitable[str]],
    max_tokens: int = MAX_CONTEXT_TOKENS,
    keep_tokens: int = KEEP_CONTEXT_TOKENS,
    bus: HarnessBus | None = None,
    workflow_id: str = "",
) -> tuple[list, str]:
    """Peel oldest turns into the summary until back under budget.

    Returns ``(kept_turns, new_summary)``. Emits ``memory.compacted`` when
    anything was folded. No-op when already under budget (no LLM call).
    """
    if _flat_tokens(turns) <= max_tokens or len(turns) <= 1:
        return turns, summary
    old: list = []
    kept = list(turns)
    while len(kept) > 1 and _flat_tokens(kept) > keep_tokens:
        old.append(kept.pop(0))
    if not old:
        return turns, summary
    new_summary = await summarize_turns(old, summary, complete=complete)
    if bus is not None:
        bus.emit(
            HarnessEventType.MEMORY_COMPACTED,
            workflow_id=workflow_id,
            summarized_turns=len(old),
            context_tokens=_flat_tokens(kept),
            summary=new_summary[:500],
        )
    return kept, new_summary
