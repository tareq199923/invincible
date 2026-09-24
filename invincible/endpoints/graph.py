# invincible/endpoints/graph.py
"""Continuity-graph projection API (Phase 15c).

GET /api/v1/sessions/{session_id}/graph renders the session's canonical
continuity history as nodes + edges + timeline, answering the dashboard's
core question: "which provider/model handled what, why did work move from
A to B, and what state did B inherit?"

The projection body lives in core/projection.py (extracted Phase 5 so the
cookie-realm dashboard renders the identical projection); this module owns
ONLY authz and ownership resolution.

Authz since Phase 2 - pure self-service:

- Every caller authenticates like /v1/* (a per-user ``inv_`` API key)
  and the projection is scoped to that principal's owning session row.
- A session string another principal owns is indistinguishable from one
  that does not exist ("known": false), so enumeration leaks nothing.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from invincible.core.projection import (
    build_session_projection,
    fetch_session_view,
    unknown_session_payload,
)
from invincible.endpoints.auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions")

_DEFAULT_LIMIT = 200


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


@router.get("/{session_id}/graph")
async def session_graph(session_id: str, request: Request,
                        limit: int = _DEFAULT_LIMIT,
                        principal=Depends(require_auth)):
    sessions_store = _require(request, "sessions")
    runs_store = _require(request, "runs")
    engine = _require(request, "continuity")

    limit = max(1, min(limit, 1000))

    # Resolve the owning context: confined to the caller's own ownership
    # triple - a foreign session string is indistinguishable from a
    # nonexistent one.
    found = await sessions_store.lookup(
        session_id,
        user_id=principal.user_id,
        project_id=principal.project_id,
    )
    if found is None:
        # Not this principal's session: stop here. Projecting past a
        # failed ownership check is what leaked one user's runs, task
        # states and checkpoints into another user's response (deep code
        # review 2026-09-24, finding 1) - the projection's store reads
        # fall back to an unscoped string match when session_pk is None.
        return unknown_session_payload(session_id)

    owner = (principal.user_id, principal.project_id)
    session_row, turns = await fetch_session_view(
        sessions_store, session_id, owner=owner)

    return await build_session_projection(
        sessions_store, runs_store, engine,
        session_id=session_id,
        session_row=session_row,
        turns=turns,
        session_pk=found,
        limit=limit,
    )
