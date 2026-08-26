# invincible/endpoints/dashboard.py
"""Phase 5 dashboard surface: server-rendered overview pages under the
browser-session realm (require_user_session - session cookies only;
this realm never authorizes /v1/* chat or /mcp).

PR-5A ships the overview page: count cards backed by existing store
reads plus a recent-sessions table. Task/memory/usage views attach to
this router in their own slices.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from invincible.core.accounts import ProjectService, UserService
from invincible.core.identity import ApiKeyStore
from invincible.core.principal import Principal
from invincible.endpoints.accounts import require_user_session

logger = logging.getLogger("invincible.dashboard")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _engine(request: Request):
    return request.app.state.engine


def _page(template_name: str, request: Request, **context):
    return templates.TemplateResponse(request, template_name, context)


@router.get("/dashboard")
async def overview_page(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    sessions_store = getattr(request.app.state, "sessions", None)
    if sessions_store is None:
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "Session storage unavailable.",
                              "type": "config_error"}},
        )
    engine = _engine(request)
    user = await UserService(engine).get(principal.user_id)
    email = user["email"] if user else "unknown"
    projects = await ProjectService(engine).list(principal.user_id)
    keys = await ApiKeyStore(engine).list(principal.user_id)
    recent_sessions = await sessions_store.list_for_user(
        principal.user_id, limit=10)
    return _page(
        "dashboard.html", request,
        user_email=email,
        counts={
            "projects": len(projects),
            "sessions":
                await sessions_store.count_for_user(principal.user_id),
            "api_keys": sum(1 for k in keys if k["revoked_at"] is None),
        },
        recent_sessions=recent_sessions,
    )
