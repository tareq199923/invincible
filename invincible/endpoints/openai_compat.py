# invincible/endpoints/openai_compat.py
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from invincible.compat.common import route_headers
from invincible.core.continuity import context_system_message
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


def _delta_tool_calls(chunk: dict) -> list:
    """The ``delta.tool_calls`` entries carried by one OpenAI stream chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return []
    return (choices[0].get("delta") or {}).get("tool_calls") or []


def _accumulate_tool_call(states: dict, tool_call: dict) -> None:
    """Merge one streamed ``tool_calls`` fragment into ``states``.

    Fragments arrive keyed by upstream ``index``: the first carries the id
    and function name, later ones append argument pieces. Mirrors the
    Anthropic stream state machine so persisted history matches what the
    client actually received.
    """
    index = tool_call.get("index", 0)
    function = tool_call.get("function") or {}
    state = states.get(index)
    if state is None:
        state = {
            "id": tool_call.get("id"),
            "name": function.get("name"),
            "arguments": "",
        }
        states[index] = state
    arguments = function.get("arguments")
    if arguments:
        state["arguments"] += arguments


def _stream_assistant_message(content: str, states: dict) -> dict:
    """Assemble the assistant turn to persist for a finished stream.

    Same shape a non-streaming upstream would have returned (content is
    None when the reply was tool calls only), so history stays consistent
    with the protocol whether the provider streamed or not.
    """
    tool_calls = [
        {
            "id": state["id"] or f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {
                "name": state["name"] or "",
                "arguments": state["arguments"] or "{}",
            },
        }
        for _, state in sorted(states.items())
    ]
    message = {"role": "assistant", "content": content or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


async def _persist_new_turns(new_turns, store, session_id, memory):
    try:
        await store.append(session_id, new_turns)
    except Exception:
        logger.exception("Failed to persist session history for %s", session_id)
    try:
        await record_turns(memory, session_id, new_turns)
    except Exception:
        logger.exception("Failed to record session facts for %s", session_id)


async def _stream_body(first, tail, store, session_id, to_persist, memory=None):
    content = ""
    tool_states = {}

    def new_turns():
        return to_persist + [_stream_assistant_message(content, tool_states)]

    try:
        if first is not None:
            content = _append_content(content, first)
            for tool_call in _delta_tool_calls(first):
                _accumulate_tool_call(tool_states, tool_call)
            yield _sse_event(first)
        async for chunk in tail:
            content = _append_content(content, chunk)
            for tool_call in _delta_tool_calls(chunk):
                _accumulate_tool_call(tool_states, tool_call)
            yield _sse_event(chunk)
    except Exception as e:
        logger.warning("Stream terminated after an upstream error: %s", e)
        yield _sse_event({"error": {"message": "stream terminated",
                                    "type": "stream_error"}})
        # Persist what accumulated before the failure so history matches
        # what the client saw (mirrors the Anthropic path's on_complete).
        await _persist_new_turns(new_turns(), store, session_id, memory)
        return
    await _persist_new_turns(new_turns(), store, session_id, memory)
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
    # Injected after history as system messages: routed but never persisted
    # (system role is excluded below), so they never accumulate.
    memory_msg = await memory_system_message(memory, session_id)
    continuity = getattr(request.app.state, "continuity", None)
    continuity_msg = await context_system_message(continuity, session_id)
    full_messages = (
        history
        + ([memory_msg] if memory_msg else [])
        + ([continuity_msg] if continuity_msg else [])
        + body.messages
    )
    # Clients resend the system prompt on every request; persisting it would
    # accumulate duplicates that trimming never removes (system messages are
    # always kept). Route with it, but only persist the new turns.
    to_persist = [m for m in body.messages if m.get("role") != "system"]

    if body.stream:
        try:
            (first, tail), info = await request.app.state.router.stream_open_detailed(
                full_messages, model=body.model, session_id=session_id
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
                **route_headers(info),
            },
        )

    try:
        result, info = await request.app.state.router.route_request_detailed(
            full_messages, model=body.model, session_id=session_id
        )
        choices = result.get("choices") or []
        if choices and "message" in choices[0]:
            new_turns = to_persist + [choices[0]["message"]]
            await store.append(session_id, new_turns)
            try:
                await record_turns(memory, session_id, new_turns)
            except Exception:
                logger.exception("Failed to record session facts for %s", session_id)
        return JSONResponse(content=result, headers=route_headers(info))
    except UpstreamClientError as e:
        return JSONResponse(
            content=e.body,
            status_code=e.status_code
        )
    except Exception:
        # Never leak internal exception text (SQL/DSN details) to clients;
        # the full traceback is already in the server log.
        logger.exception("chat completion failed")
        return JSONResponse(
            content={"error": {"message": "gateway error", "type": "gateway_error"}},
            status_code=503,
        )
