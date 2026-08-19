# invincible/endpoints/openai_compat.py
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from invincible.core.memory import memory_system_message, record_turns
from invincible.core.router import AllProvidersFailedError, UpstreamClientError

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    stream: bool | None = None
    model: str | None = None

router = APIRouter()


def _sse_event(data) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _append_content(content: str, chunk: dict) -> str:
    for choice in chunk.get("choices") or []:
        piece = (choice.get("delta") or {}).get("content")
        if piece:
            content += piece
    return content


async def _stream_body(first, tail, store, session_id, to_persist, memory=None):
    content = ""
    try:
        if first is not None:
            content = _append_content(content, first)
            yield _sse_event(first)
        async for chunk in tail:
            content = _append_content(content, chunk)
            yield _sse_event(chunk)
    except Exception as e:
        logger.warning("Stream terminated after an upstream error: %s", e)
        yield _sse_event({"error": {"message": str(e), "type": "stream_error"}})
        return
    new_turns = to_persist + [{"role": "assistant", "content": content}]
    try:
        await store.append(session_id, new_turns)
    except Exception:
        logger.exception("Failed to persist session history for %s", session_id)
    try:
        await record_turns(memory, session_id, new_turns)
    except Exception:
        logger.exception("Failed to record session facts for %s", session_id)
    yield "data: [DONE]\n\n"


def models_from_providers(providers: list) -> list[dict]:
    """Map the router's loaded providers to OpenAI /v1/models entries.

    The router validates providers at startup, so every entry normally has
    a ``model_id``; the isinstance/get guard is cheap defense in depth.
    Runtime provider order is preserved. Aliases are listed after the real
    model ids so clients can discover and request them.
    """
    entries = [
        {"id": p["model_id"], "object": "model", "owned_by": "invincible"}
        for p in providers
        if isinstance(p, dict) and p.get("model_id")
    ]
    for p in providers:
        for alias in p.get("aliases") or []:
            entries.append({"id": alias, "object": "model", "owned_by": "invincible"})
    return entries


@router.get("/v1/models")
async def list_models(request: Request):
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
    return {"object": "list", "data": models_from_providers(router.providers)}

@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatRequest):
    session_id = (
        request.headers.get("x-claude-code-session-id")
        or request.headers.get("X-Session-Id")
        or "default"
    )
    store = request.app.state.sessions
    memory = getattr(request.app.state, "memory", None)

    history = await store.load(session_id)
    # Injected after history as a system message: routed but never
    # persisted (system role is excluded below), so it never accumulates.
    memory_msg = await memory_system_message(memory, session_id)
    full_messages = (
        history + ([memory_msg] if memory_msg else []) + body.messages
    )
    # Clients resend the system prompt on every request; persisting it would
    # accumulate duplicates that trimming never removes (system messages are
    # always kept). Route with it, but only persist the new turns.
    to_persist = [m for m in body.messages if m.get("role") != "system"]

    if body.stream:
        try:
            first, tail = await request.app.state.router.stream_open(
                full_messages, model=body.model
            )
        except UpstreamClientError as e:
            return JSONResponse(content=e.body, status_code=e.status_code)
        except AllProvidersFailedError as e:
            return JSONResponse(
                content={"error": {"message": str(e), "type": "gateway_error"}},
                status_code=503,
            )
        return StreamingResponse(
            _stream_body(first, tail, store, session_id, to_persist, memory),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        result = await request.app.state.router.route_request(
            full_messages, model=body.model
        )
        choices = result.get("choices") or []
        if choices and "message" in choices[0]:
            new_turns = to_persist + [choices[0]["message"]]
            await store.append(session_id, new_turns)
            try:
                await record_turns(memory, session_id, new_turns)
            except Exception:
                logger.exception("Failed to record session facts for %s", session_id)
        return JSONResponse(content=result)
    except UpstreamClientError as e:
        return JSONResponse(
            content=e.body,
            status_code=e.status_code
        )
    except Exception as e:
        return JSONResponse(
            content={"error": {"message": str(e), "type": "gateway_error"}},
            status_code=503
        )
