# invincible/endpoints/graph.py
"""Continuity-graph projection API (Phase 15c).

GET /api/v1/sessions/{session_id}/graph renders the session's canonical
continuity history as nodes + edges + timeline, answering the dashboard's
core question: "which provider/model handled what, why did work move from
A to B, and what state did B inherit?"

The projection body lives in core/projection.py (extracted Phase 5 so the
cookie-realm dashboard renders the identical projection); this module owns
ONLY authz and ownership resolution.

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

from fastapi import APIRouter, Depends, HTTPException, Request

from invincible.core.projection import (
    build_session_projection,
    fetch_session_view,
)
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

    session_row, turns = await fetch_session_view(
        sessions_store, session_id, owner=owner)

    return await build_session_projection(
        sessions_store, runs_store, engine,
        session_id=session_id,
        session_row=session_row,
        turns=turns,
        session_pk=session_pk,
        limit=limit,
    )
