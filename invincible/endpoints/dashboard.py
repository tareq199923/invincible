# invincible/endpoints/dashboard.py
"""Phase 5 dashboard surface: server-rendered pages plus a small
cookie-realm management API under the browser-session realm
(require_user_session - session cookies only; this realm never
authorizes /v1/* chat or /mcp).

PR-5A shipped the overview; PR-5B added session/task views over the
shared projection; PR-5C adds memory management: browse/search/add
(explicit-layer) and audited ownership-predicated deletes. The
INVINCIBLE_MEMORY kill-switch gates only CREATION - existing rows stay
browsable/deletable so the toggle never traps data.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from invincible.core.accounts import ProjectService, UserService
from invincible.core.identity import ApiKeyStore
from invincible.core.principal import Principal
from invincible.core.projection import (
    build_session_projection,
    fetch_session_view,
)
from invincible.core.settings import settings
from invincible.endpoints.accounts import (
    _audit,
    _page,
    _payload,
    _wants_html,
    require_user_session,
)

logger = logging.getLogger("invincible.dashboard")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_MEMORY_PAGE_SIZE = 20
_MEMORY_MAX_CHARS = 2000
_MEMORY_KINDS = ("note", "fact", "preference", "decision", "task")
_MEMORY_LAYERS = ("explicit", "auto")


def _engine(request: Request):
    return request.app.state.engine


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


def _checked_layer(layer: str | None) -> str | None:
    if layer is not None and layer not in _MEMORY_LAYERS:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "layer must be 'explicit' or "
                                         "'auto'.",
                              "type": "invalid_request_error"}},
        )
    return layer


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
    memories_total = await _state(request, "memory").count_for_user(
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
            "memories": memories_total,
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


# ---------------------------------------------------------------------------
# Memory management (Phase 5 PR-5C)


@router.get("/dashboard/memory")
async def memory_page(
    request: Request,
    q: str = "",
    layer: str | None = None,
    kind: str | None = None,
    offset: int = 0,
    principal: Principal = Depends(require_user_session),
):
    store = _state(request, "memory")
    layer = _checked_layer(layer)
    query = q.strip()
    if query:
        rows = await store.search_for_user(
            principal.user_id, query, layer=layer, kind=kind,
            limit=_MEMORY_PAGE_SIZE)
        total = len(rows)  # search results are a single ranked page
        offset = 0
    else:
        total = await store.count_for_user(
            principal.user_id, layer=layer, kind=kind)
        rows = await store.list_for_user(
            principal.user_id, layer=layer, kind=kind,
            limit=_MEMORY_PAGE_SIZE, offset=max(0, offset))
    return _page(
        "memory.html", request,
        user_email=await _email(_engine(request), principal),
        memories=rows,
        total=total,
        q=query,
        layer=layer or "",
        kind=kind or "",
        kinds=_MEMORY_KINDS,
        page_size=_MEMORY_PAGE_SIZE,
        offset=offset,
    )


@router.get("/memories")
async def list_memories(
    request: Request,
    q: str = "",
    layer: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
    principal: Principal = Depends(require_user_session),
):
    store = _state(request, "memory")
    layer = _checked_layer(layer)
    query = q.strip()
    if query:
        # Lexical search: single ranked page, no offset pagination.
        rows = await store.search_for_user(
            principal.user_id, query, layer=layer, kind=kind, limit=limit)
        total = len(rows)
    else:
        rows = await store.list_for_user(
            principal.user_id, layer=layer, kind=kind, limit=limit,
            offset=offset)
        total = await store.count_for_user(
            principal.user_id, layer=layer, kind=kind)
    return {"memories": rows, "total": total}


@router.post("/memories")
async def create_memory(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    # The kill-switch gates CREATION only (browse/delete stay available
    # so toggling off never traps already-saved data).
    if not settings.memory_enabled():
        return JSONResponse(
            {"error": {"code": "memory_disabled",
                       "message": "INVINCIBLE_MEMORY is off; saving new "
                                  "memories is disabled."}},
            status_code=503,
        )
    body = await _payload(request)
    content = str(body.get("content", "")).strip()
    kind = str(body.get("kind") or "note")
    if not content:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "Memory content is required.",
                              "type": "invalid_request_error"}},
        )
    if len(content) > _MEMORY_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": f"Memory content must be at most "
                                         f"{_MEMORY_MAX_CHARS} characters.",
                              "type": "invalid_request_error"}},
        )
    if kind not in _MEMORY_KINDS:
        kind = "note"
    made_id = await _state(request, "memory").save_memory(
        user_id=principal.user_id, content=content, layer="explicit",
        kind=kind, confidence=1.0)
    await _audit(request, "memory.created", actor_user_id=principal.user_id,
                 resource_type="memory", resource_id=str(made_id))
    if _wants_html(request):
        return RedirectResponse("/dashboard/memory", status_code=303)
    return JSONResponse({"id": made_id, "kind": kind}, status_code=201)


@router.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    deleted = await _state(request, "memory").delete(
        memory_id, user_id=principal.user_id)
    if not deleted:
        # Foreign and unknown ids are indistinguishable (anti-enumeration).
        raise HTTPException(
            status_code=404,
            detail={"error": {"message": "No such memory.",
                              "type": "not_found_error"}},
        )
    await _audit(request, "memory.deleted", actor_user_id=principal.user_id,
                 resource_type="memory", resource_id=str(memory_id))
    if request.headers.get("HX-Request") == "true":
        # HTMX row removal: empty 204 lets hx-swap="delete" drop the row.
        return Response(status_code=204)
    return {"deleted": True}
