# invincible/endpoints/anthropic_compat.py
"""Anthropic Messages API compatibility endpoint (POST /v1/messages).

Translates Anthropic requests into Invincible's internal message model,
hands them to the existing Router, and translates responses back to
Anthropic format. The Router is never modified and never becomes aware that
the client spoke Anthropic; sessions are shared with the OpenAI endpoint
because both protocols persist the same internal message format.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from invincible.compat.anthropic import (
    anthropic_to_internal,
    anthropic_tools_to_openai,
    build_error,
    build_stream_events,
    internal_to_anthropic,
    translate_tool_choice,
)
from invincible.compat.common import estimate_token_sum, route_headers
from invincible.core.compression import compress_messages, compression_enabled
from invincible.core.continuity import context_system_message
from invincible.core.memory import memory_system_message, record_turns
from invincible.core.router import AllProvidersFailedError, UpstreamClientError
from invincible.models.anthropic import AnthropicMessagesRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def _error_message(status_code: int, message: str) -> JSONResponse:
    status, body = build_error(status_code, message)
    return JSONResponse(content=body, status_code=status)


def _assistant_message_from_provider(provider_message: dict) -> dict:
    """Normalize a provider assistant message for session persistence.

    Keeps ``tool_calls`` (so a tool turn's history stays structurally valid
    for the OpenAI providers) while dropping provider-only noise fields.
    """
    message = {"role": "assistant", "content": provider_message.get("content") or ""}
    tool_calls = provider_message.get("tool_calls") or []
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


async def _persist(store, session_id, new_messages: list, assistant_message: dict,
                   memory=None):
    """Append this request's new turns to the session under the store's lock.

    ``new_messages`` is the request's own messages (system role already
    excluded here so repeated system prompts never accumulate in history);
    the assistant reply is appended after them. Facts are extracted from
    the persisted turns (Phase 10) on a best-effort basis.
    """
    saved = [m for m in new_messages if m.get("role") != "system"]
    new_turns = saved + [assistant_message]
    try:
        await store.append(session_id, new_turns)
    except Exception:
        logger.exception("Failed to persist session history for %s", session_id)
    try:
        await record_turns(memory, session_id, new_turns)
    except Exception:
        logger.exception("Failed to record session facts for %s", session_id)


@router.post("/v1/messages")
async def anthropic_messages(request: Request, body: AnthropicMessagesRequest):
    session_id = (
        request.headers.get("x-claude-code-session-id")
        or request.headers.get("X-Session-Id")
        or "default"
    )
    store = request.app.state.sessions
    memory = getattr(request.app.state, "memory", None)

    try:
        internal_messages = anthropic_to_internal(body.messages, body.system)
    except ValueError as e:
        return _error_message(400, str(e))

    history = await store.load(session_id)
    # Injected memory/continuity are routed but never persisted (system role).
    memory_msg = await memory_system_message(memory, session_id)
    continuity = getattr(request.app.state, "continuity", None)
    continuity_msg = await context_system_message(continuity, session_id)
    full_messages = (
        history
        + ([memory_msg] if memory_msg else [])
        + ([continuity_msg] if continuity_msg else [])
        + internal_messages
    )
    # Estimate on the compressed messages so reported usage tracks what is
    # actually sent (Phase 9). Per-provider trimming still makes this an
    # upper bound when a small-context provider wins the route — that drift
    # predates compression and is documented in ROADMAP Phase 9.
    if compression_enabled():
        input_tokens = estimate_token_sum(compress_messages(full_messages))
    else:
        input_tokens = estimate_token_sum(full_messages)
    tools = anthropic_tools_to_openai(body.tools)
    tool_choice = translate_tool_choice(body.tool_choice)

    if body.stream:
        try:
            (first, tail), info = await request.app.state.router.stream_open_detailed(
                full_messages,
                tools=tools,
                tool_choice=tool_choice,
                model=body.model,
                session_id=session_id,
            )
        except UpstreamClientError as e:
            return _error_message(e.status_code, "Upstream request failed")
        except AllProvidersFailedError:
            return _error_message(503, "All providers failed or are in cooldown.")

        async def save_complete(accumulated: dict):
            await _persist(store, session_id, internal_messages, accumulated, memory)

        return StreamingResponse(
            build_stream_events(
                first, tail, body.model, input_tokens, on_complete=save_complete
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                **route_headers(info),
            },
        )

    try:
        result, info = await request.app.state.router.route_request_detailed(
            full_messages,
            tools=tools,
            tool_choice=tool_choice,
            model=body.model,
            session_id=session_id,
        )
    except UpstreamClientError as e:
        return _error_message(e.status_code, "Upstream request failed")
    except AllProvidersFailedError:
        return _error_message(503, "All providers failed or are in cooldown.")
    except Exception:
        logger.exception("Unexpected error during Anthropic completion")
        return _error_message(500, "Internal server error")

    choices = result.get("choices") or []
    if choices and "message" in choices[0]:
        await _persist(
            store,
            session_id,
            internal_messages,
            _assistant_message_from_provider(choices[0]["message"]),
            memory,
        )

    anthropic_response = internal_to_anthropic(result, body.model, input_tokens)
    return JSONResponse(content=anthropic_response, headers=route_headers(info))
