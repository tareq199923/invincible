# invincible/core/todos.py
"""Model-owned step lists on the continuity store (Step 4: todo tool).

One ``todo`` tool, four actions (add/list/complete/clear), zero new
tables: items live as the payload of the reserved
``task_key="todos"`` chain in the existing continuity store, so
per-session/per-user isolation (via ``session_pk``) and restart
survival come free — the same guarantees ``task_state_*`` already
has. ``AGENTS.md`` convention 1 is satisfied with no migration:
no metadata change, no Alembic revision.

Payload shape (bound by ``ContinuityEngine.MAX_PAYLOAD_CHARS``):
``{"items": [{"id": "3", "text": "...", "done": false}], "next_id": 4}``.
Ids are per-list monotonic strings; ``clear`` empties the items but
keeps ``next_id`` so ids are never reused within a session.

Layering: pure ``core/`` business logic — both ``endpoints/mcp.py``
and ``core/webchat_agent.py`` call :func:`run_todo` so the two
surfaces can never drift. Returns ``(result, ok)`` like
``webchat_agent._run_data_tool``.
"""
from __future__ import annotations

from invincible.core.continuity import ContinuityConflictError

TODO_TASK_KEY = "todos"
TODO_ACTIONS = ("add", "list", "complete", "clear")
TODO_MAX_ITEMS = 20
TODO_MAX_TEXT_CHARS = 200


def blank_payload() -> dict:
    """Fresh empty todo payload (ids start at "1", never reused)."""
    return {"items": [], "next_id": 1}


def normalize(payload: object) -> dict:
    """Coerce a stored ``todos`` payload to the canonical shape.

    Foreign writers share the chain (same ``task_key`` namespace), so
    anything that is not ``{"items": [...], "next_id": N}`` degrades to
    a blank list rather than raising — a corrupt todo list must never
    break the tool. Item entries missing ``id``/``text`` are dropped;
    ``done`` defaults to False.
    """
    next_id = 1
    items: list[dict] = []
    if isinstance(payload, dict):
        raw_next = payload.get("next_id")
        if isinstance(raw_next, int) and raw_next >= 1:
            next_id = raw_next
        raw_items = payload.get("items")
        if isinstance(raw_items, list):
            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                item_id = entry.get("id")
                text = entry.get("text")
                if item_id is None or not isinstance(text, str):
                    continue
                items.append({
                    "id": str(item_id),
                    "text": text,
                    "done": entry.get("done") is True,
                })
    return {"items": items, "next_id": next_id}


async def run_todo(
    continuity,
    *,
    session_id: str,
    session_pk,
    action: str,
    text: str = "",
    todo_id: str = "",
    actor: str = "todo",
) -> tuple[dict, bool]:
    """Execute one todo action against the continuity store.

    ``session_pk=None`` with ``action="list"`` returns the empty list
    (scoped caller with no owner sees nothing — mirrors
    ``task_state_get``); with any other action it fails closed rather
    than writing unscoped. Returns ``(result, ok)``; every failure is
    a plain ``{"status": "error", "error": ...}`` dict, never an
    exception. Callers append ``expected_args_hint("todo")`` to the
    error text for the schema-echo convention.
    """
    if continuity is None:
        return {
            "status": "error",
            "error": "Continuity engine not initialized on this server.",
        }, False
    if action not in TODO_ACTIONS:
        return {
            "status": "error",
            "error": (
                f"unknown todo action: {action!r}. "
                f"Valid actions: {', '.join(TODO_ACTIONS)}."
            ),
        }, False
    if action == "list" and session_pk is None:
        return {
            "items": [],
            "count": 0,
            "version": 0,
            "note": "no todos tracked in this session",
        }, True
    if session_pk is None:
        return {
            "status": "error",
            "error": "Could not resolve the session for this subject.",
        }, False

    try:
        state = await continuity.get_state(
            session_id, TODO_TASK_KEY, session_pk=session_pk)
    except ValueError as e:
        return {"status": "error", "error": str(e)}, False
    current = normalize(state["payload"] if state else None)
    version = state["version"] if state else 0

    if action == "list":
        return {
            "items": current["items"],
            "count": len(current["items"]),
            "version": version,
        }, True

    if action == "clear":
        try:
            head = await continuity.set_state(
                session_id,
                {"items": [], "next_id": current["next_id"]},
                actor=actor,
                task_key=TODO_TASK_KEY,
                session_pk=session_pk,
            )
        except (ContinuityConflictError, ValueError) as e:
            return {"status": "error", "error": str(e)}, False
        return {"cleared": True, "count": 0, "version": head["version"]}, True

    if action == "add":
        step = (text or "").strip()
        if not step:
            return {
                "status": "error",
                "error": "todo add requires non-empty 'text'.",
            }, False
        if len(step) > TODO_MAX_TEXT_CHARS:
            return {
                "status": "error",
                "error": (
                    f"todo text must be at most {TODO_MAX_TEXT_CHARS} "
                    f"characters (got {len(step)})."
                ),
            }, False
        if len(current["items"]) >= TODO_MAX_ITEMS:
            return {
                "status": "error",
                "error": (
                    f"todo list is full ({TODO_MAX_ITEMS} items); "
                    "complete or clear items first."
                ),
            }, False
        item = {
            "id": str(current["next_id"]), "text": step, "done": False}
        payload = {
            "items": [*current["items"], item],
            "next_id": current["next_id"] + 1,
        }
        try:
            head = await continuity.set_state(
                session_id, payload, actor=actor,
                task_key=TODO_TASK_KEY, session_pk=session_pk)
        except (ContinuityConflictError, ValueError) as e:
            return {"status": "error", "error": str(e)}, False
        return {
            "added": item,
            "items": payload["items"],
            "count": len(payload["items"]),
            "version": head["version"],
        }, True

    # action == "complete"
    want = (todo_id or "").strip()
    if not want:
        return {
            "status": "error",
            "error": "todo complete requires 'id'.",
        }, False
    target = next(
        (i for i in current["items"] if i["id"] == want), None)
    if target is None:
        return {
            "status": "error",
            "error": f"unknown todo id: {want!r}.",
        }, False
    items = [
        {**i, "done": True} if i["id"] == want else i
        for i in current["items"]
    ]
    try:
        head = await continuity.set_state(
            session_id,
            {"items": items, "next_id": current["next_id"]},
            actor=actor, task_key=TODO_TASK_KEY, session_pk=session_pk)
    except (ContinuityConflictError, ValueError) as e:
        return {"status": "error", "error": str(e)}, False
    return {
        "completed": {**target, "done": True},
        "items": items,
        "count": len(items),
        "version": head["version"],
    }, True
