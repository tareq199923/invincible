# invincible/endpoints/graph.py
"""Continuity-graph projection API (Phase 15c).

GET /api/v1/sessions/{session_id}/graph renders the session's canonical
continuity history as nodes + edges + timeline, answering the dashboard's
core question: "which provider/model handled what, why did work move from
A to B, and what state did B inherit?"

STRICTLY A PROJECTION: every node is derived from authoritative stores
(SessionStore turns/messages, RunStore attempts, ContinuityEngine states +
checkpoints). The graph owns no state of its own and is never a source of
truth (Rule 7). Reads go through the stores' public APIs only.

Authz since Phase 2 - dual-realm:

- ``INVINCIBLE_ADMIN_KEY`` = operator override: fail-closed, sees any
  session (documented out-of-band operator trust).
- Otherwise a user Principal resolves exactly like /v1/* (legacy key,
  API key, OAuth/MCP token; fail-open local when no gateway key) and the
  projection is scoped to that principal's owning session row. A session
  string another principal owns is indistinguishable from one that does
  not exist ("known": false), so enumeration leaks nothing.
"""
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from invincible.endpoints.admin_api import require_admin
from invincible.endpoints.auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions")

_DEFAULT_LIMIT = 200


async def require_graph_access(request: Request):
    """Operator override first; otherwise an authenticated user Principal."""
    try:
        await require_admin(request)
        return "admin"
    except HTTPException:
        return await require_auth(request)


def _require(request: Request, attr: str):
    value = getattr(request.app.state, attr, None)
    if value is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": f"{attr} not initialized on this server",
                    "type": "config_error",
                }
            },
        )
    return value


async def _session_and_turns(store, session_id: str,
                             *, owner: tuple[int, int] | None):
    """(session_row|None, [turn dicts]) via SessionStore's public reads.

    ``owner`` scopes every read; ``None`` means the session does not exist
    for the caller (indistinguishable from a foreign one)."""
    if owner is None:
        return None, []
    uid, pid = owner
    session_row = await store.session_meta(
        session_id, user_id=uid, project_id=pid)
    if session_row is None:
        return None, []
    turns = await store.turn_overview(
        session_id, user_id=uid, project_id=pid)
    return session_row, turns


@router.get("/{session_id}/graph")
async def session_graph(session_id: str, request: Request,
                        limit: int = _DEFAULT_LIMIT,
                        access=Depends(require_graph_access)):
    sessions_store = _require(request, "sessions")
    runs_store = _require(request, "runs")
    engine = _require(request, "continuity")

    limit = max(1, min(limit, 1000))

    # Resolve the owning context: operator override looks across all
    # owners (documented operator trust); a user principal is confined to
    # its own ownership triple.
    if access == "admin":
        owner = await sessions_store.owner_context(session_id)
    else:
        principal = access
        found = await sessions_store.lookup(
            session_id,
            user_id=principal.user_id,
            project_id=principal.project_id,
        )
        owner = (
            (principal.user_id, principal.project_id)
            if found is not None
            else None
        )
    session_pk = (
        await sessions_store.lookup(session_id, user_id=owner[0],
                                    project_id=owner[1])
        if owner is not None
        else None
    )

    session_meta, turns = await _session_and_turns(
        sessions_store, session_id, owner=owner)
    known = bool(session_meta)

    nodes: list[dict] = []
    edges: list[dict] = []
    state_node_ids: set[str] = set()

    if known:
        nodes.append({
            "id": "session", "kind": "session", "label": session_id,
            "created_at": session_meta.get("created_at"),
            "updated_at": session_meta.get("updated_at"),
        })

    run_rows = list(reversed(await runs_store.recent(
        session_id=session_id, limit=limit, session_pk=session_pk)))
    prev_run_id = None
    for run in run_rows:
        node_id = f"run:{run['id']}"
        nodes.append({
            "id": node_id,
            "kind": "run",
            "label": (
                f"attempt #{run['attempt_index']} → "
                f"{run['provider_name']}/{run['model_id']} "
                f"[{run['outcome']}]"
            ),
            "provider_name": run["provider_name"],
            "model_id": run["model_id"],
            "outcome": run["outcome"],
            "error_class": run.get("error_class"),
            "attempt_index": run["attempt_index"],
            "request_id": run["request_id"],
            "started_at": run["started_at"],
            "finished_at": run.get("finished_at"),
            "ts": run.get("finished_at") or run["started_at"],
        })
        edges.append({"source": "session", "target": node_id,
                      "kind": "attempted_for"})
        if prev_run_id is not None:
            previous = next(n for n in nodes if n["id"] == prev_run_id)
            same_request = previous["request_id"] == run["request_id"]
            edges.append({
                "source": prev_run_id,
                "target": node_id,
                "kind": "failover_from" if same_request else "followed_by",
            })
        prev_run_id = node_id

    task_keys = await engine.active_task_keys(session_id, limit=10,
                                              session_pk=session_pk)
    latest_states = {}
    for task_key in task_keys:
        head = await engine.get_state(session_id, task_key,
                                      session_pk=session_pk)
        if head is None:
            continue
        latest_states[task_key] = head
        history = await engine.history(session_id, task_key, limit=limit,
                                       session_pk=session_pk)
        prev_state_id = None
        for state in reversed(history):  # oldest first
            node_id = f"state:{task_key}:v{state['version']}"
            state_node_ids.add(node_id)
            nodes.append({
                "id": node_id,
                "kind": "task_state",
                "label": f"{task_key} v{state['version']} "
                         f"({state['status']}) by {state['updated_by']}",
                "task_key": task_key,
                "version": state["version"],
                "status": state["status"],
                "payload": state["payload"],
                "updated_by": state["updated_by"],
                "ts": state["updated_at"],
            })
            if prev_state_id is not None:
                edges.append({"source": prev_state_id, "target": node_id,
                              "kind": "supersedes"})
            else:
                edges.append({"source": "session", "target": node_id,
                              "kind": "canonical_for"})
            prev_state_id = node_id

    for cp in await engine.checkpoints(session_id, limit=limit,
                                       session_pk=session_pk):
        node_id = f"checkpoint:{cp['id']}"
        nodes.append({
            "id": node_id,
            "kind": "checkpoint",
            "label": (
                f"checkpoint #{cp['id']} ({cp['task_key']} @ v"
                f"{cp['state_version']}): {cp['note']}"
            ),
            "task_key": cp["task_key"],
            "state_version": cp["state_version"],
            "note": cp["note"],
            "ts": cp["created_at"],
        })
        target = f"state:{cp['task_key']}:v{cp['state_version']}"
        if target in state_node_ids:
            edges.append({"source": node_id, "target": target,
                          "kind": "pins"})

    for turn in turns:
        node_id = f"turn:{turn['seq']}"
        nodes.append({
            "id": node_id,
            "kind": "turn",
            "label": (
                f"turn {turn['seq']} "
                f"({turn['message_count']} msg): "
                f"{turn['first_payload_snippet'][:80]}"
            ),
            "message_count": turn["message_count"],
            "first_payload_snippet": turn["first_payload_snippet"],
        })
        edges.append({"source": "session", "target": node_id,
                      "kind": "contains"})

    interruption = await engine.interruption_note(session_id,
                                                  session_pk=session_pk)

    timeline_ids = [
        n["id"] for n in sorted(
            (n for n in nodes if n.get("ts") is not None),
            key=lambda n: (n["ts"], n["id"]),
        )
    ]
    providers_used = sorted({n["provider_name"] for n in nodes
                             if n["kind"] == "run"})
    summary = {
        "providers_used": providers_used,
        "attempts": len(run_rows),
        "failovers": sum(1 for n in nodes
                         if n["kind"] == "run" and n["outcome"] != "ok"),
        "tasks": {
            key: {
                "version": st["version"],
                "status": st["status"],
                "payload": st["payload"],
            }
            for key, st in latest_states.items()
        },
        "interruption_note": interruption,
        "turns": len(turns),
    }

    return {
        "session_id": session_id,
        "known": known,
        "generated_at": time.time(),
        "nodes": nodes,
        "edges": edges,
        "timeline": timeline_ids,
        "summary": summary,
    }
