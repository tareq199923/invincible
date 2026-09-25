# invincible/core/harness_events.py
"""Harness event contract (H0) — port of Hendrixer `shared/events.ts`.

Every step the harness takes becomes an event on the bus. The Inspector UI
(and later the dashboard Workflows page) only ever renders this stream — it
never talks to the model or tools directly.

Event names match Hendrixer 1:1 so lesson notes map directly. Payloads are
METADATA ONLY (tool names, ids, statuses) — never commands, paths, file
contents, or secrets (see docs/SECURITY.md secrets discipline).
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, TypedDict


class HarnessEventType(str, Enum):
    """Every event the harness can emit (Hendrixer `EventType` parity)."""

    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    MODEL_DELTA = "model.delta"
    MODEL_COMPLETED = "model.completed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    MEMORY_COMPACTED = "memory.compacted"
    AGENT_HANDOFF = "agent.handoff"
    PLAN_CREATED = "plan.created"
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_COMPLETED = "subagent.completed"
    SUBAGENT_FAILED = "subagent.failed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    LOG = "log"


class HarnessEvent(TypedDict, total=False):
    """One stamped event. `type` + `ts` + `id` always present; the rest is
    per-type metadata (workflow_id, tool names, statuses — never secrets)."""

    type: str
    id: str
    ts: float
    workflow_id: str
    # tool events
    tool_call_id: str
    name: str
    status: str
    # orchestration
    from_agent: str
    to_agent: str
    reason: str
    # memory
    summarized_turns: int
    context_tokens: int
    # approval
    approved: bool
    # generic
    level: str
    message: str
    user_id: int
    extra: dict[str, Any]


def new_event(type: HarnessEventType | str, **fields: Any) -> HarnessEvent:
    """Stamp an event with id + wall-clock ts (wall clock: events cross
    process boundaries via WS/persistence, same reason PendingActionStore
    uses time.time())."""
    event: HarnessEvent = {
        "type": str(type.value if isinstance(type, HarnessEventType) else type),
        "id": uuid.uuid4().hex,
        "ts": time.time(),
    }
    event.update(fields)  # type: ignore[typeddict-unknown-key]
    return event
