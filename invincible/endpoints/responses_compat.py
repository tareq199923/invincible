# invincible/endpoints/responses_compat.py
"""OpenAI Responses API compatibility endpoint (POST /v1/responses).

Translates Responses requests (spoken by Codex CLI and the OpenAI SDK's
Responses surface) into Invincible's internal message model, hands them
to the existing Router, and translates responses back. The Router is
never modified and never becomes aware that the client spoke Responses;
sessions are shared with the other protocol endpoints because all of them
persist the same internal message format.

Unlike the chat-completions/Anthropic endpoints this one does NOT
prepend stored history when routing: Responses clients are stateless
(``store: false``) and resend the full conversation in ``input`` every
turn, so prepending would duplicate it upstream. Persistence instead
dedupes against stored history (``_suffix_after_history``).
"""
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from invincible.compat.common import estimate_token_sum, route_headers
from invincible.compat.responses import (
    build_error,
    build_stream_events,
    internal_to_responses,
    responses_to_internal,
    responses_tools_to_openai,
    translate_tool_choice,
)
from invincible.core.compression import compress_messages, compression_enabled
from invincible.core.context_builder import build_context_messages
from invincible.core.memory import MemoryStore
from invincible.core.principal import Principal
from invincible.core.router import (
    NO_CREDENTIALS_MESSAGE,
    AllProvidersFailedError,
    NoCredentialsConfiguredError,
    UpstreamClientError,
)
from invincible.endpoints.auth import require_auth
from invincible.endpoints.byok import byok_attempt_source
from invincible.models.responses import ResponsesRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def _error_response(status_code: int, message: str) -> JSONResponse:
    status, body = build_error(status_code, message)
    return JSONResponse(content=body, status_code=status)


def _suffix_after_history(history: list, incoming: list) -> list:
    """The turns of ``incoming`` that are new relative to ``history``.

    Responses clients (Codex) resend the full conversation every turn.
    When the stored history is a prefix of the resent conversation, only
    the suffix is new; when it is not (the client compacted/rewound its
    history), nothing can be matched safely and the caller persists only
    the assistant reply rather than duplicating turns.

    System messages are excluded from ``incoming`` before the comparison:
    clients resend ``instructions`` every request but system messages are
    never persisted, so they would break the prefix match.
    """
    incoming = [m for m in incoming if m.get("role") != "system"]
    if len(history) <= len(incoming) and incoming[:len(history)] == history:
        return incoming[len(history):]
    return []


async def _persist(store, session_id, new_messages: list,
                   assistant_message: dict, memory: MemoryStore | None,
                   principal: Principal):
    """Append this request's genuinely-new turns (system role excluded -
    Responses clients resend ``instructions`` every request and system
    messages must never accumulate) plus the assistant reply. Memories
    are extracted from the persisted turns on a best-effort basis.

    ``principal`` is required (multi-tenant audit Step 2): persistence
    must land under the caller's own session, never a fallback owner.
    """
    saved = [m for m in new_messages if m.get("role") != "system"]
    new_turns = saved + [assistant_message]
    if not new_turns:
        return
    try:
        await store.append(
            session_id,
            new_turns,
            user_id=principal.user_id,
            project_id=principal.project_id,
        )
    except Exception:
        logger.exception("Failed to persist session history for %s",
                         session_id)
    if memory is None:
        return
    try:
        await memory.record_memories(
            user_id=principal.user_id,
            client_session_id=session_id,
            messages_list=new_turns,
        )
    except Exception:
        logger.exception("Failed to record memories for %s", session_id)


@router.post("/v1/responses")
async def create_response(
    request: Request,
    body: ResponsesRequest,
    principal: Principal = Depends(require_auth),
):
    session_id = (
        request.headers.get("x-claude-code-session-id")
        or request.headers.get("X-Session-Id")
        or "default"
    )
    store = request.app.state.sessions
    memory = getattr(request.app.state, "memory", None)

    # Owning surrogate session for run records and task reads.
    session_pk = await store.resolve_or_create(
        session_id,
        user_id=principal.user_id,
        project_id=principal.project_id,
    )

    try:
        internal_messages = responses_to_internal(
            body.input, body.instructions)
    except ValueError as e:
        return _error_response(400, str(e))

    # The request already carries the full conversation (stateless
    # client) - stored history is only the persistence dedupe baseline,
    # never routing input.
    history = await store.load(
        session_id,
        user_id=principal.user_id,
        project_id=principal.project_id,
    )
    # Memory + continuity injections share one budget via the
    # ContextBuilder. Injected system messages are routed but never
    # persisted (system role), so they never accumulate.
    injections = await build_context_messages(
        retrieval=getattr(request.app.state, "retrieval", None),
        continuity_engine=getattr(request.app.state, "continuity", None),
        user_id=principal.user_id,
        project_id=principal.project_id,
        session_id=session_id,
        session_pk=session_pk,
        new_messages=internal_messages,
    )
    # Injections sit after the leading system message(s) and before the
    # resent conversation, mirroring the other endpoints' history +
    # injections + new ordering (a memory block arriving after the newest
    # user turn reads as an answer, not context).
    system_prefix = []
    for message in internal_messages:
        if message.get("role") != "system":
            break
        system_prefix.append(message)
    conversation = internal_messages[len(system_prefix):]
    full_messages = system_prefix + injections + conversation
    # Estimate on the compressed messages so reported usage tracks what
    # is actually sent. Per-provider trimming still makes this an upper
    # bound when a small-context provider wins the route.
    if compression_enabled():
        input_tokens = estimate_token_sum(compress_messages(full_messages))
    else:
        input_tokens = estimate_token_sum(full_messages)
    tools = responses_tools_to_openai(body.tools)
    tool_choice = translate_tool_choice(body.tool_choice)

    # BYOK: api_key-realm principals route ONLY through their own
    # connected credentials (never the operator's shared pool);
    # legacy/anonymous keep the operator pool as-is.
    byok = await byok_attempt_source(request, principal)
    if byok is not None and not byok[0]:
        return _error_response(400, NO_CREDENTIALS_MESSAGE)
    byok_kwargs = (
        {} if byok is None
        else {"byok_candidates": byok[0], "byok_key_resolver": byok[1]}
    )

    if body.stream:
        try:
            (first, tail), info = (
                await request.app.state.router.stream_open_detailed(
                    full_messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    model=body.model,
                    session_id=session_id,
                    session_pk=session_pk,
                    **byok_kwargs,
                )
            )
        except NoCredentialsConfiguredError:
            # Defensive: the pre-router check above normally catches this.
            return _error_response(400, NO_CREDENTIALS_MESSAGE)
        except UpstreamClientError as e:
            return _error_response(e.status_code, "Upstream request failed")
        except AllProvidersFailedError:
            return _error_response(
                503, "All providers failed or are in cooldown.")

        runs_store = getattr(request.app.state, "runs", None)
        request_id = info["request_id"]
        new_turns = _suffix_after_history(history, internal_messages)

        async def save_complete(accumulated: dict):
            await _persist(
                store, session_id, new_turns, accumulated, memory,
                principal,
            )
            if runs_store is not None:
                try:
                    await runs_store.attach_output(
                        # chars/4 estimate of what actually streamed,
                        # flagged in the run row's meta (no wire change).
                        request_id=request_id,
                        output_tokens=len(json.dumps(accumulated)) // 4,
                        estimated=True,
                    )
                except Exception as exc:
                    logger.warning("Failed to attach stream usage: %s", exc)

        return StreamingResponse(
            build_stream_events(
                first, tail, body.model, input_tokens,
                on_complete=save_complete,
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
            session_pk=session_pk,
            **byok_kwargs,
        )
    except NoCredentialsConfiguredError:
        return _error_response(400, NO_CREDENTIALS_MESSAGE)
    except UpstreamClientError as e:
        return _error_response(e.status_code, "Upstream request failed")
    except AllProvidersFailedError:
        return _error_response(
            503, "All providers failed or are in cooldown.")
    except Exception:
        logger.exception("Unexpected error during Responses completion")
        return _error_response(500, "Internal server error")

    choices = result.get("choices") or []
    if choices and "message" in choices[0]:
        message = choices[0]["message"]
        assistant_message = {
            "role": "assistant",
            "content": message.get("content") or "",
        }
        if message.get("tool_calls"):
            assistant_message["tool_calls"] = message["tool_calls"]
        await _persist(
            store,
            session_id,
            _suffix_after_history(history, internal_messages),
            assistant_message,
            memory,
            principal,
        )

    responses_response = internal_to_responses(
        result, body.model, input_tokens)
    return JSONResponse(content=responses_response,
                        headers=route_headers(info))
