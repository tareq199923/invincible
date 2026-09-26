# invincible/endpoints/openai_compat.py
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from invincible.compat.common import route_headers
from invincible.core.chat_service import (
    ChatError,
    _stream_body,
    models_from_providers,
    open_stream,
    prepare_chat,
    run_nonstreaming,
)
from invincible.core.principal import Principal
from invincible.endpoints.auth import require_auth
from invincible.endpoints.byok import byok_attempt_source

# Re-exported for backward compatibility (imported from here before W1).
__all__ = ["models_from_providers", "router"]


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    stream: bool | None = None
    model: str | None = None

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request,
                      principal: Principal = Depends(require_auth)):
    router = getattr(request.app.state, "router", None)
    if router is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": "Router not initialized",
                    "type": "config_error",
                }
            },
        )
    # LOW-3: list the caller's EFFECTIVE pool, mirroring chat routing -
    # every principal is a BYOK (api_key) principal now, so the list is
    # always that user's connected credentials (an empty list when they
    # have none, matching the chat 400).
    byok = await byok_attempt_source(request, principal)
    providers = router.providers if byok is None else byok[0]
    return {"object": "list", "data": models_from_providers(providers)}

@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatRequest,
    principal: Principal = Depends(require_auth),
):
    session_id = (
        request.headers.get("x-claude-code-session-id")
        or request.headers.get("X-Session-Id")
        or "default"
    )
    store = request.app.state.sessions
    memory = getattr(request.app.state, "memory", None)

    # Phase 9 BYOK: every /v1/* principal routes ONLY through its own
    # connected credentials - there is no shared pool (the product
    # decision pins this). Loaded before the injections so the user's
    # per-user overrides (Phase 1) can gate memory/continuity for this
    # request.
    byok = await byok_attempt_source(request, principal, model=body.model)
    try:
        prepared = await prepare_chat(
            body.messages,
            principal=principal,
            session_id=session_id,
            model=body.model,
            sessions=store,
            retrieval=getattr(request.app.state, "retrieval", None),
            continuity=getattr(request.app.state, "continuity", None),
            byok=byok,
        )
    except ChatError as e:
        return JSONResponse(content=e.body, status_code=e.status_code)

    if body.stream:
        try:
            (first, tail), info = await open_stream(
                prepared, model=body.model,
                router=request.app.state.router,
            )
        except ChatError as e:
            return JSONResponse(content=e.body, status_code=e.status_code)
        return StreamingResponse(
            _stream_body(first, tail, store, session_id, prepared.to_persist,
                         memory,
                         principal=principal,
                         runs_store=getattr(request.app.state, "runs", None),
                         request_id=info["request_id"],
                         max_turns=prepared.max_turns),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                **route_headers(info),
            },
        )

    try:
        result, info = await run_nonstreaming(
            prepared,
            principal=principal,
            model=body.model,
            sessions=store,
            memory=memory,
            router=request.app.state.router,
        )
        return JSONResponse(content=result, headers=route_headers(info))
    except ChatError as e:
        return JSONResponse(content=e.body, status_code=e.status_code)
