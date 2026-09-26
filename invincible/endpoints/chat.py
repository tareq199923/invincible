# invincible/endpoints/chat.py
"""Dashboard webchat: browser-native BYOK chat with live SSE streaming,
including agentic PC control in three modes.

Cookie-realm ONLY (``require_user_session`` - same realm as the rest of
the dashboard; ``inv_`` API keys never authorize this surface, and nothing
here touches ``/v1/*`` or ``/mcp`` auth).

Modes (per stream request, default ``manual``):

- ``plan``   - read-only inspection tools only (offered by construction,
  so the model cannot mutate anything); ends with a plan.
- ``manual`` - all tools; every ``execute_bash``/``write_file`` pauses
  for browser approval (``POST /dashboard/chat/approve``) before running.
- ``auto``   - all tools run immediately, no approvals. Denylists still
  enforced.

Tool execution mirrors ``POST /mcp``: with
``INVINCIBLE_AGENT_ROUTING=1`` confirmed work runs on the caller's paired
machine via the agent registry, otherwise on the server host. The chat
pipeline itself is shared with the API path via ``core/chat_service.py``
(history scoping, memory+continuity injections that are routed but never
persisted, tool-pairing repair, BYOK-only routing through the single
``router._iter_attempts`` loop, persistence + runs rows).
"""
import html
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from invincible.compat.common import upstream_error_detail
from invincible.core.chat_service import (
    ChatError,
    models_from_providers,
    prepare_chat,
    web_sse_event,
)
from invincible.core.principal import Principal
from invincible.core.settings import settings
from invincible.core.webchat_agent import (
    DEFAULT_MODE,
    WEBCHAT_MODES,
    build_agent_executor,
    run_agent_turn,
)
from invincible.endpoints.accounts import (
    _audit,
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
# Marker distinguishing dashboard-created conversations from API-client
# (Claude Code / Codex / ...) sessions sharing the same store. Creation
# (_new_web_session_id) and listing (_sidebar) both go through this so
# the two can never drift apart.
_WEB_SESSION_PREFIX = "web-"


def _new_web_session_id() -> str:
    """Client session id for a browser-started conversation (lazy: no DB
    row until the first message persists via resolve_or_create)."""
    return f"{_WEB_SESSION_PREFIX}{uuid.uuid4().hex}"


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
    """Newest-first sidebar rows, dashboard-created conversations only
    (``web-`` ids): API-client threads (Claude Code / Codex / ...) stay
    out of the webchat sidebar. Each row carries a bounded derived title.
    Direct ``?session=`` links to owned API sessions still resolve (see
    ``chat_page``) - the store itself stays shared."""
    store = _state(request, "sessions")
    rows = await store.list_for_user(
        principal.user_id, limit=_SIDEBAR_LIMIT,
        client_session_id_prefix=_WEB_SESSION_PREFIX)
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
    if not active_id:
        # Fresh account (or all history pruned): mint a lazy id so the
        # composer - with its model/mode pickers - always renders. No DB
        # row is created until the first message persists, exactly like
        # POST /dashboard/chat/new.
        active_id = _new_web_session_id()
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
        side_history=sidebar,
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


@router.get("/dashboard/chat/list")
async def chat_list(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Sidebar history for non-chat pages: the global sidebar lazy-loads
    this (same cookie realm, ownership-predicated via _sidebar)."""
    return {"sessions": await _sidebar(request, principal)}


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
    mode = raw.get("mode") or DEFAULT_MODE
    mode = str(mode).strip().lower() or DEFAULT_MODE
    if mode not in WEBCHAT_MODES:
        return _bad_request(
            f"mode must be one of {', '.join(WEBCHAT_MODES)}.")

    # Capture state now: the generator runs after the response starts.
    store = _state(request, "sessions")
    engine = getattr(request.app.state, "engine", None)
    memory = getattr(request.app.state, "memory", None)
    retrieval = getattr(request.app.state, "retrieval", None)
    continuity = getattr(request.app.state, "continuity", None)
    runs_store = getattr(request.app.state, "runs", None)
    router_obj = getattr(request.app.state, "router", None)
    pending_store = _state(request, "pending_actions")
    waiter = _state(request, "webchat_approvals")
    registry = getattr(request.app.state, "agent_registry", None)
    routing_on = settings.agent_routing()
    executor = build_agent_executor(
        registry, principal.user_id, routing_on)

    async def _audit_tool(action: str, meta: dict | None = None):
        # Metadata only (action + mode) - never commands/paths/contents,
        # which can carry secrets (same discipline as the MCP audit).
        await _audit(
            request, action, actor_user_id=principal.user_id,
            actor_kind="user", resource_type="webchat_tool",
            meta=meta or {},
        )

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
        # Approval tokens this stream is waiting on (disconnect cleanup).
        waited: list[str] = []
        try:
            async for ev_name, ev_data in run_agent_turn(
                prepared, model=model, router=router_obj,
                sessions=store, memory=memory, runs_store=runs_store,
                principal=principal, mode=mode,
                pending_store=pending_store, executor=executor,
                waiter=waiter, audit=_audit_tool,
                retrieval=retrieval, continuity=continuity,
                engine=engine,
            ):
                if ev_name == "approval":
                    waited.append(ev_data["token"])
                if ev_name == "done":
                    # Server-rendered final bubble: escaped once here so
                    # the client inserts it without an XSS surface.
                    answer = ev_data.pop("text", "")
                    ev_data["bubble_html"] = html.escape(
                        answer).replace("\n", "<br>")
                yield web_sse_event(ev_name, ev_data)
        finally:
            for token in waited:
                waiter.discard(token)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/dashboard/chat/approve")
async def chat_approve(
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Resolve one manual-mode tool approval staged by this user's own
    stream (cookie realm only - ``inv_`` keys are rejected like every
    other webchat route).

    Only a real JSON boolean decides: anything else is a 400 (unlike the
    MCP ``confirm_action`` deny-default - a browser misclick should be
    loud, not an accidental execution or silent drop). Unknown, foreign,
    expired, or already-settled tokens share one 404 body
    (anti-enumeration); settling a token pops any orphaned staging row
    so a late approval can never fire.
    """
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return _bad_request("Request body must be JSON.")
    token = str(raw.get("token") or "")
    if not token:
        return _bad_request("token is required.")
    approve = raw.get("approve")
    if not isinstance(approve, bool):
        return _bad_request("approve must be true or false.")
    waiter = _state(request, "webchat_approvals")
    if waiter.resolve(token, principal.user_id, approve):
        await _audit(
            request,
            "webchat.approval.granted" if approve
            else "webchat.approval.denied",
            actor_user_id=principal.user_id,
            actor_kind="user",
            resource_type="webchat_approval",
        )
        return {"ok": True, "approved": approve}
    # No live waiter: pop an orphaned staging row (if ours) so nothing
    # can confirm it later; foreign tokens are untouched and every miss
    # reads identically.
    _state(request, "pending_actions").take(
        token, requester_subject=principal.user_id)
    raise HTTPException(
        status_code=404,
        detail={"error": {"message": "No such approval.",
                          "type": "not_found_error"}},
    )
