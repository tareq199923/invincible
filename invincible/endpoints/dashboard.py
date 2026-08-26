# invincible/endpoints/dashboard.py
"""Phase 5 dashboard surface: server-rendered pages under the
browser-session realm (require_user_session - session cookies only;
this realm never authorizes /v1/* chat or /mcp).

PR-5A shipped the overview; PR-5B adds the session list, the per-session
continuity detail (rendered from the SAME core/projection.py payload the
graph API returns), and the cross-session task list.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from invincible.core.accounts import ProjectService, UserService
from invincible.core.identity import ApiKeyStore
from invincible.core.principal import Principal
from invincible.core.projection import (
    build_session_projection,
    fetch_session_view,
)
from invincible.endpoints.accounts import require_user_session

logger = logging.getLogger("invincible.dashboard")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _engine(request: Request):
    return request.app.state.engine


def _page(template_name: str, request: Request, **context):
    return templates.TemplateResponse(request, template_name, context)


def _missing_page(request: Request, title: str, message: str,
                  status_code: int = 404):
    """Generic result page; the SAME body renders for unknown AND foreign
    resources so existence never leaks across users."""
    return templates.TemplateResponse(
        request, "device_result.html",
        {"title": title, "message": message},
        status_code=status_code,
    )


def _state(request: Request, attr: str):
    value = getattr(request.app.state, attr, None)
    if value is None:
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": f"{attr} not initialized on "
                                          f"this server",
                              "type": "config_error"}},
        )
    return value


async def _email(engine, principal: Principal) -> str:
    user = await UserService(engine).get(principal.user_id)
    return user["email"] if user else "unknown"


@router.get("/dashboard")
async def overview_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    sessions_store = _state(request, "sessions")
    engine = _engine(request)
    email = await _email(engine, principal)
    projects = await ProjectService(engine).list(principal.user_id)
    keys = await ApiKeyStore(engine).list(principal.user_id)
    recent_sessions = await sessions_store.list_for_user(
        principal.user_id, limit=10)
    task_heads = await _state(request, "continuity").list_for_user(
        principal.user_id)
    return _page(
        "dashboard.html", request,
        user_email=email,
        counts={
            "projects": len(projects),
            "sessions":
                await sessions_store.count_for_user(principal.user_id),
            "api_keys": sum(1 for k in keys if k["revoked_at"] is None),
            "tasks": len(task_heads),
        },
        recent_sessions=recent_sessions,
    )


# ---------------------------------------------------------------------------
# Sessions


@router.get("/dashboard/sessions")
async def sessions_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    sessions_store = _state(request, "sessions")
    rows = await sessions_store.list_for_user(principal.user_id, limit=100)
    return _page(
        "sessions.html", request,
        user_email=await _email(_engine(request), principal),
        sessions_rows=rows,
    )


@router.get("/dashboard/sessions/{session_pk}")
async def session_detail_page(
    session_pk: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    sessions_store = _state(request, "sessions")
    runs_store = _state(request, "runs")
    continuity = _state(request, "continuity")

    # Ownership predicate first: a surrogate id another user owns is
    # indistinguishable from one that does not exist.
    owned = await sessions_store.lookup_by_pk(
        session_pk, user_id=principal.user_id,
        project_id=principal.project_id)
    if owned is None:
        return _missing_page(request, "Unknown session",
                             "This session does not exist or is not yours.")
    client_session_id, owner = owned

    session_row, turns = await fetch_session_view(
        sessions_store, client_session_id,
        owner=(principal.user_id, principal.project_id))
    projection = await build_session_projection(
        sessions_store, runs_store, continuity,
        session_id=client_session_id,
        session_row=session_row,
        turns=turns,
        session_pk=session_pk,
        limit=200,
    )

    labels = {n["id"]: n["label"] for n in projection["nodes"]}
    failovers = [
        {"from": labels[e["source"]], "to": labels[e["target"]]}
        for e in projection["edges"] if e["kind"] == "failover_from"
    ]
    checkpoints = [n for n in projection["nodes"]
                   if n["kind"] == "checkpoint"]
    # Normalize for the template: turn nodes carry no ts key at all.
    activity = sorted(
        ({"label": n["label"], "ts": n.get("ts"), "kind": n["kind"]}
         for n in projection["nodes"] if n["id"] != "session"),
        key=lambda n: (n["ts"] is None, -(n["ts"] or 0)),
    )
    return _page(
        "session_detail.html", request,
        user_email=await _email(_engine(request), principal),
        session_pk=session_pk,
        client_session_id=client_session_id,
        created_at=session_row["created_at"],
        updated_at=session_row["updated_at"],
        summary=projection["summary"],
        tasks=projection["summary"]["tasks"],
        checkpoints=checkpoints,
        failovers=failovers,
        activity=activity,
    )


# ---------------------------------------------------------------------------
# Tasks (cross-session)


@router.get("/dashboard/tasks")
async def tasks_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    continuity = _state(request, "continuity")
    heads = await continuity.list_for_user(principal.user_id)
    return _page(
        "tasks.html", request,
        user_email=await _email(_engine(request), principal),
        task_heads=heads,
    )
