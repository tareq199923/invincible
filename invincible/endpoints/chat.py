# invincible/endpoints/chat.py
"""Dashboard webchat: browser-native BYOK chat with live SSE streaming.

Cookie-realm ONLY (``require_user_session`` - same realm as the rest of
the dashboard; ``inv_`` API keys never authorize this surface, and nothing
here touches ``/v1/*`` or ``/mcp`` auth). Text-only v1: no MCP tool
execution, no tool-call round-trips - the browser sends one user turn and
receives text deltas.

The whole pipeline is shared with the API path via
``core/chat_service.py`` (history scoping, memory+continuity injections
that are routed but never persisted, tool-pairing repair, BYOK-only
routing through the single ``router._iter_attempts`` loop, persistence +
runs rows), so history written here reads back identically on ``/v1/*``
and vice versa.
"""
import html
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from invincible.compat.common import upstream_error_detail
from invincible.core.chat_service import (
    ChatError,
    _persist_new_turns,
    chunk_text,
    models_from_providers,
    open_stream,
    prepare_chat,
    web_sse_event,
)
from invincible.core.principal import Principal
from invincible.endpoints.accounts import (
    _page,
    _wants_html,
    require_user_session,
)
from invincible.endpoints.byok import byok_attempt_source
from invincible.endpoints.dashboard import _email, _state

logger = logging.getLogger("invincible.webchat")

router = APIRouter()

# Sidebar cap (bounded per-page DB work: one load per listed session for
# title derivation) and display/validation bounds for web-supplied fields.
_SIDEBAR_LIMIT = 30
_TITLE_CHARS = 60
_MAX_ID_CHARS = 200
_MAX_MODEL_CHARS = 200


def _new_web_session_id() -> str:
    """Client session id for a browser-started conversation (lazy: no DB
    row until the first message persists via resolve_or_create)."""
    return f"web-{uuid.uuid4().hex}"


def _session_title(history: list, fallback: str) -> str:
    """Sidebar label: the session's first user message, bounded; the raw
    client id when the session has no user text yet (or is foreign)."""
    for message in history:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            text = " ".join(content.split())
            if len(text) > _TITLE_CHARS:
                return text[:_TITLE_CHARS].rstrip() + "…"
            return text
    return fallback


def _renderable_turns(history: list) -> list[dict]:
    """History subset the text-only template renders: user/assistant turns
    with non-empty string content (system injections are never persisted;
    tool payloads from API-created turns are skipped in v1)."""
    turns = []
    for message in history:
        if not isinstance(message, dict):
            continue
        if message.get("role") not in ("user", "assistant"):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        turns.append({"role": message["role"], "text": content})
    return turns


def _error_message(body: object) -> str:
    """Client-safe error text with the same semantics as the API errors
    (provider message passthrough where the API passes through, generic
    otherwise - never tracebacks, DSNs, or keys)."""
    return upstream_error_detail(body) or "gateway error"


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error"}},
        status_code=400,
    )


async def _sidebar(request: Request, principal: Principal) -> list[dict]:
    """Newest-first sidebar rows (all user sessions: web+API continuity
    is a feature), each with a bounded derived title."""
    store = _state(request, "sessions")
    rows = await store.list_for_user(
        principal.user_id, limit=_SIDEBAR_LIMIT)
    sidebar = []
    for row in rows:
        client_id = row["client_session_id"]
        history = await store.load(
            client_id,
            user_id=principal.user_id,
            project_id=principal.project_id,
        )
        sidebar.append({
            "client_session_id": client_id,
            "title": _session_title(history, client_id),
        })
    return sidebar


async def _model_ids(request: Request, principal: Principal) -> list[str]:
    byok = await byok_attempt_source(request, principal)
    providers = [] if byok is None else byok[0]
    return [m["id"] for m in models_from_providers(providers)]


@router.get("/dashboard/chat")
async def chat_page(
    request: Request,
    session: str | None = None,
    principal: Principal = Depends(require_user_session),
):
    store = _state(request, "sessions")
    sidebar = await _sidebar(request, principal)
    active_id = (session or "").strip()
    if not active_id and sidebar:
        # Default to the most recent conversation; a fresh ?session=new-id
        # from /dashboard/chat/new simply renders empty (lazy creation).
        active_id = sidebar[0]["client_session_id"]
    history: list = []
    if active_id:
        # Unknown AND foreign ids load identically empty (anti-enumeration:
        # load is ownership-predicated, so there is nothing to distinguish).
        history = await store.load(
            active_id,
            user_id=principal.user_id,
            project_id=principal.project_id,
        )
    models = await _model_ids(request, principal)
    return _page(
        "chat.html", request,
        user_email=await _email(request.app.state.engine, principal),
        sessions=sidebar,
        active_session=active_id,
        history=_renderable_turns(history),
        models=models,
        has_provider=bool(models),
    )


@router.post("/dashboard/chat/new")
async def new_chat(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    session_id = _new_web_session_id()
    if _wants_html(request):
        # Sidebar form post: bounce back into the fresh conversation.
        return RedirectResponse(
            f"/dashboard/chat?session={session_id}", status_code=303)
    return JSONResponse({"session_id": session_id})


@router.get("/dashboard/chat/models")
async def chat_models(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    # Empty list = no connected credentials; the template renders the
    # no-credentials empty state linking /dashboard/providers.
    return {"models": await _model_ids(request, principal)}


@router.post("/dashboard/chat/stream")
async def chat_stream(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return _bad_request("Request body must be JSON.")
    text = str(raw.get("message") or "").strip()
    if not text:
        return _bad_request("Message is required.")
    session_id = str(raw.get("session_id") or "").strip()
    if not session_id:
        return _bad_request("session_id is required.")
    if len(session_id) > _MAX_ID_CHARS:
        return _bad_request("session_id must be at most "
                            f"{_MAX_ID_CHARS} characters.")
    model = raw.get("model")
    if model is not None:
        model = str(model).strip() or None
    if model is not None and len(model) > _MAX_MODEL_CHARS:
        return _bad_request("model must be at most "
                            f"{_MAX_MODEL_CHARS} characters.")

    # Capture state now: the generator runs after the response starts.
    store = _state(request, "sessions")
    memory = getattr(request.app.state, "memory", None)
    retrieval = getattr(request.app.state, "retrieval", None)
    continuity = getattr(request.app.state, "continuity", None)
    runs_store = getattr(request.app.state, "runs", None)
    router_obj = getattr(request.app.state, "router", None)

    async def _events():
        if router_obj is None:
            yield web_sse_event("error", {"message": "Router not initialized"})
            return
        byok = await byok_attempt_source(request, principal, model=model)
        try:
            prepared = await prepare_chat(
                [{"role": "user", "content": text}],
                principal=principal,
                session_id=session_id,
                model=model,
                sessions=store,
                retrieval=retrieval,
                continuity=continuity,
                byok=byok,
            )
        except ChatError as e:
            yield web_sse_event("error", {"message": _error_message(e.body)})
            return
        try:
            (first, tail), info = await open_stream(
                prepared, model=model, router=router_obj)
        except ChatError as e:
            yield web_sse_event("error", {"message": _error_message(e.body)})
            return
        content = ""
        try:
            if first is not None:
                piece = chunk_text(first)
                content += piece
                if piece:
                    yield web_sse_event("token", {"text": piece})
            async for chunk in tail:
                piece = chunk_text(chunk)
                content += piece
                if piece:
                    yield web_sse_event("token", {"text": piece})
        except Exception as exc:
            logger.warning("Webchat stream terminated: %s", exc)
            # Persist what accumulated before the failure so history
            # matches what the browser rendered (mirrors _stream_body).
            await _persist_new_turns(
                prepared.to_persist,
                {"role": "assistant", "content": content or None},
                store, session_id, memory, principal,
                runs_store=runs_store, request_id=info["request_id"],
                max_turns=prepared.max_turns,
            )
            yield web_sse_event("error", {"message": "stream terminated"})
            return
        assistant_message = {"role": "assistant", "content": content or None}
        await _persist_new_turns(
            prepared.to_persist, assistant_message,
            store, session_id, memory, principal,
            runs_store=runs_store, request_id=info["request_id"],
            max_turns=prepared.max_turns,
        )
        # Server-rendered final bubble: escaped once here so the client
        # inserts it without an XSS surface.
        bubble_html = html.escape(content).replace("\n", "<br>")
        yield web_sse_event("done", {
            "bubble_html": bubble_html,
            "provider": info["provider_name"],
            "model": info["model_id"],
            "attempts": info["attempts"],
        })

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
