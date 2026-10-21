# invincible/core/workflow_store.py
"""Read-only workflow timelines for the dashboard (H6c).

The harness bus writes workflow-scoped events to ``workflow_events``
best-effort. This module is the only read surface: every query filters on
``user_id`` first, so a workflow id is never a capability and a foreign
workflow renders exactly like an unknown one.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from invincible.core.db import workflow_events

_LIST_LIMIT = 50
_EVENT_LIMIT = 200


def _clamp(value: int, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def summarize_event(row: Any) -> str:
    """One safe line for a timeline row.

    Only fields the event contract already calls metadata: agent names,
    step ids, tool names, statuses, counts. Task objectives, findings
    previews, commands, paths, and contents are deliberately not shown.
    """
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    etype = str(row.get("type") or "")
    if etype == "plan.created":
        steps = payload.get("steps") or []
        if not isinstance(steps, list):
            steps = []
        agents = sorted({
            str(step.get("agent"))
            for step in steps
            if isinstance(step, dict) and step.get("agent")
        })
        count = len(steps)
        summary = f"{count} step{'s' if count != 1 else ''}"
        if agents:
            summary += f" - {', '.join(agents)}"
        return summary
    if etype.startswith("subagent."):
        step = payload.get("step_id")
        agent = payload.get("agent")
        status = etype.split(".", 1)[1]
        parts: list[str] = []
        if step:
            parts.append(f"step {step}")
        if agent:
            parts.append(str(agent))
        parts.append(status)
        return " - ".join(parts)
    if etype.startswith("tool."):
        name = payload.get("name") or "tool"
        status = payload.get("status") or etype.split(".", 1)[1]
        return f"{name} - {status}"
    if etype.startswith("approval."):
        action = payload.get("action") or payload.get("name") or "action"
        status = etype.split(".", 1)[1]
        return f"{action} - {status}"
    if etype == "log":
        return str(payload.get("message") or "")[:160]
    if etype == "memory.compacted":
        turns = payload.get("summarized_turns")
        tokens = payload.get("context_tokens")
        parts = []
        if turns is not None:
            parts.append(f"{turns} turns")
        if tokens is not None:
            parts.append(f"{tokens} tokens")
        return " - ".join(parts)
    if etype == "agent.handoff":
        source = payload.get("from_agent") or "?"
        target = payload.get("to_agent") or "?"
        return f"{source} -> {target}"
    if etype.startswith(("workflow.", "model.")):
        return etype.split(".", 1)[1]
    return ""


async def list_workflows(
    engine, *, user_id: int, limit: int = _LIST_LIMIT
) -> list[dict]:
    """Recent workflows for one account, newest activity first."""
    limit = _clamp(limit, default=_LIST_LIMIT, maximum=100)
    async with engine.connect() as conn:
        totals = (await conn.execute(
            select(
                workflow_events.c.workflow_id,
                func.count().label("event_count"),
                func.min(workflow_events.c.created_at).label("started_at"),
                func.max(workflow_events.c.created_at).label("last_at"),
            )
            .where(workflow_events.c.user_id == user_id)
            .group_by(workflow_events.c.workflow_id)
            .order_by(func.max(workflow_events.c.created_at).desc())
            .limit(limit)
        )).mappings().all()
        if not totals:
            return []
        workflow_ids = [row["workflow_id"] for row in totals]
        ranked = (
            select(
                workflow_events.c.workflow_id,
                workflow_events.c.type,
                workflow_events.c.payload,
                workflow_events.c.created_at,
                func.row_number().over(
                    partition_by=workflow_events.c.workflow_id,
                    order_by=(
                        workflow_events.c.created_at.desc(),
                        workflow_events.c.id.desc(),
                    ),
                ).label("rn"),
            )
            .where(
                workflow_events.c.user_id == user_id,
                workflow_events.c.workflow_id.in_(workflow_ids),
            )
            .subquery()
        )
        latest = (await conn.execute(
            select(ranked).where(ranked.c.rn == 1)
        )).mappings().all()
    latest_by = {row["workflow_id"]: row for row in latest}
    result = []
    for row in totals:
        latest_row = latest_by.get(row["workflow_id"])
        result.append({
            "workflow_id": row["workflow_id"],
            "event_count": row["event_count"],
            "started_at": row["started_at"],
            "last_at": row["last_at"],
            "latest_type": latest_row["type"] if latest_row else "",
            "latest_summary": (
                summarize_event(latest_row) if latest_row else ""
            ),
        })
    return result


async def list_workflow_events(
    engine, *, user_id: int, workflow_id: str, limit: int = _EVENT_LIMIT
) -> list[dict]:
    """Newest events for one workflow, returned oldest-first for display.

    The ``user_id`` predicate is mandatory: a workflow id alone is never
    sufficient to read another account's events.
    """
    limit = _clamp(limit, default=_EVENT_LIMIT, maximum=500)
    async with engine.connect() as conn:
        rows = (await conn.execute(
            select(
                workflow_events.c.type,
                workflow_events.c.payload,
                workflow_events.c.created_at,
            )
            .where(
                workflow_events.c.user_id == user_id,
                workflow_events.c.workflow_id == workflow_id,
            )
            .order_by(
                workflow_events.c.created_at.desc(),
                workflow_events.c.id.desc(),
            )
            .limit(limit)
        )).mappings().all()
    ordered = list(reversed(rows))
    return [
        {
            "type": row["type"],
            "payload": row["payload"] or {},
            "created_at": row["created_at"],
            "summary": summarize_event(row),
        }
        for row in ordered
    ]
