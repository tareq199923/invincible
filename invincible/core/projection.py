# invincible/core/projection.py
"""Continuity-graph projection (Phase 15c shape; extracted from
endpoints/graph.py in Phase 5 so the dashboard can render the same
projection server-side).

STRICTLY A PROJECTION: every node is derived from authoritative stores
(SessionStore turns/messages, RunStore attempts, ContinuityEngine states
+ checkpoints). The projection owns no state of its own and is never a
source of truth. Reads go through the stores' public APIs only; the
CALLER owns authz - it resolves the owning context (operator override or
the principal's own ownership triple) and passes it in.
"""
import time


async def fetch_session_view(store, session_id: str,
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


async def build_session_projection(
    sessions_store,
    runs_store,
    continuity,
    *,
    session_id: str,
    session_row: dict | None,
    turns: list,
    session_pk: int | None,
    limit: int,
) -> dict:
    """Assemble the nodes/edges/timeline/summary payload for one session.

    ``session_row``/``turns`` come from :func:`fetch_session_view`;
    ``session_pk`` scopes attempt/state/checkpoint reads (None only when
    the caller could not resolve an owning row)."""
    known = bool(session_row)

    nodes: list[dict] = []
    edges: list[dict] = []
    state_node_ids: set[str] = set()

    if known:
        nodes.append({
            "id": "session", "kind": "session", "label": session_id,
            "created_at": session_row.get("created_at"),
            "updated_at": session_row.get("updated_at"),
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

    task_keys = await continuity.active_task_keys(session_id, limit=10,
                                                  session_pk=session_pk)
    latest_states = {}
    for task_key in task_keys:
        head = await continuity.get_state(session_id, task_key,
                                          session_pk=session_pk)
        if head is None:
            continue
        latest_states[task_key] = head
        history = await continuity.history(session_id, task_key, limit=limit,
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

    for cp in await continuity.checkpoints(session_id, limit=limit,
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

    interruption = await continuity.interruption_note(session_id,
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
