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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
from invincible.endpoints.dashboard import _email, _state, templates

logger = logging.getLogger("invincible.webchat")

router = APIRouter()

# Sidebar cap and display/validation bounds for web-supplied fields. The
# sidebar's titles come from ONE bounded query (SessionStore.sidebar_rows),
# not a load() per listed session.
_SIDEBAR_LIMIT = 30
_TITLE_CHARS = 60
# Custom (renamed) sidebar names are the user's own label, so they get more
# room than the derived first-message snippet - still bounded, because the
# value round-trips through the sidebar on every page render.
_RENAME_CHARS = 100
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


def _bound_title(text: str) -> str:
    """Collapse whitespace and elide a sidebar label to ``_TITLE_CHARS``.
    Used on the bounded first-user-message snippet the store returns."""
    text = " ".join(text.split())
    if len(text) > _TITLE_CHARS:
        return text[:_TITLE_CHARS].rstrip() + "…"
    return text


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

    One query: ``sidebar_rows`` derives the title server-side from the first
    user message's ``content`` (bounded substring) so no full payload leaves
    Postgres - the old shape issued one ``load()`` per listed session.

    A custom (renamed) title wins over the derived one; ``pinned`` rows
    arrive first (the store orders them that way). ``id`` is the surrogate
    pk the sidebar's rename/pin/delete actions address.

    Direct ``?session=`` links to owned API sessions still resolve (see
    ``chat_page``) - the store itself stays shared."""
    store = _state(request, "sessions")
    rows = await store.sidebar_rows(
        principal.user_id,
        limit=_SIDEBAR_LIMIT, client_session_id_prefix=_WEB_SESSION_PREFIX,
        title_chars=_TITLE_CHARS)
    sidebar = []
    for row in rows:
        client_id = row["client_session_id"]
        custom = row.get("title")
        snippet = row["first_user_content"]
        # The label without any custom name: the first user message (or the
        # client id when the conversation has no text yet). Kept alongside
        # the effective title so clearing a rename needs no round trip.
        derived = (client_id if not isinstance(snippet, str)
                   or not snippet.strip() else _bound_title(snippet))
        sidebar.append({
            "id": row["id"],
            "client_session_id": client_id,
            "title": custom if isinstance(custom, str) and custom.strip()
                     else derived,
            "derived": derived,
            "pinned": bool(row.get("pinned")),
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
    """Sidebar history for non-chat pages.

    ``{"sessions": [...]}`` by default. The sidebar's lazy loader asks for
    ``Accept: text/html`` and receives the rendered ``_chat_rows`` fragment
    instead, which keeps the row markup defined exactly once (in the Jinja
    partial) while both shapes read the same ``_sidebar`` rows.
    """
    rows = await _sidebar(request, principal)
    if "text/html" in request.headers.get("accept", ""):
        # Not a full page: a bare fragment for `list.innerHTML = ...`.
        return templates.TemplateResponse(
            request, "_chat_rows.html",
            {"rows": rows, "active_session": ""},
        )
    return {"sessions": rows}


def _no_such_chat() -> HTTPException:
    """Foreign and unknown session pks are indistinguishable (the dashboard's
    anti-enumeration convention: identical 404 body either way)."""
    return HTTPException(
        status_code=404,
        detail={"error": {"message": "No such chat.",
                          "type": "not_found_error"}},
    )


@router.patch("/dashboard/chat/sessions/{session_pk}")
async def update_chat_session(
    session_pk: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Sidebar management for ONE owned conversation: rename and/or pin.

    Both fields are optional and independent, so the sidebar menu's Rename
    and Pin actions share this route. ``{"title": ""}`` clears a custom
    name (the label falls back to the first user message). Ownership is
    predicated in the store, so a foreign pk is a 404 with no side effect.
    """
    try:
        raw = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return _bad_request("Request body must be JSON.")
    fields = {k: raw.get(k) for k in ("title", "pinned") if k in raw}
    if not fields:
        return _bad_request("Send title and/or pinned.")
    store = _state(request, "sessions")
    applied: dict = {}
    if "title" in fields:
        value = fields["title"]
        if value is not None and not isinstance(value, str):
            return _bad_request("title must be a string.")
        clean = " ".join((value or "").split())
        if len(clean) > _RENAME_CHARS:
            return _bad_request(f"title must be at most {_RENAME_CHARS} "
                                f"characters.")
        stored = clean or None
        if not await store.rename_session(
            session_pk, user_id=principal.user_id,
            project_id=principal.project_id, title=stored,
        ):
            raise _no_such_chat()
        await _audit(request, "session.renamed",
                     actor_user_id=principal.user_id,
                     resource_type="session", resource_id=str(session_pk),
                     meta={"cleared": stored is None,
                           "chars": len(clean)})
        applied["title"] = stored
    if "pinned" in fields:
        if not isinstance(fields["pinned"], bool):
            return _bad_request("pinned must be a boolean.")
        stored_pin = await store.set_pinned(
            session_pk, user_id=principal.user_id,
            project_id=principal.project_id, pinned=fields["pinned"])
        if stored_pin is None:
            raise _no_such_chat()
        await _audit(request, "session.pinned",
                     actor_user_id=principal.user_id,
                     resource_type="session", resource_id=str(session_pk),
                     meta={"pinned": stored_pin})
        applied["pinned"] = stored_pin
    return {"id": session_pk, **applied}


@router.delete("/dashboard/chat/sessions/{session_pk}")
async def delete_chat_session(
    session_pk: int,
    request: Request,
    principal: Principal = Depends(require_user_session),
):
    """Delete ONE owned conversation (turns, messages, checkpoints, task
    states and runs - see ``SessionStore.delete_session`` for the cascade).
    Foreign/unknown pks raise before anything is touched."""
    deleted = await _state(request, "sessions").delete_session(
        session_pk, user_id=principal.user_id,
        project_id=principal.project_id)
    if not deleted:
        raise _no_such_chat()
    await _audit(request, "session.deleted", actor_user_id=principal.user_id,
                 resource_type="session", resource_id=str(session_pk))
    if request.headers.get("HX-Request") == "true":
        # Empty 204: htmx never swaps 204s, so confirm.js drops the sidebar
        # row in place (modal + toast + empty state included).
        return Response(status_code=204)
    return {"deleted": True}


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
