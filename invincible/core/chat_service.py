# invincible/core/chat_service.py
"""Shared chat pipeline behind POST /v1/chat/completions and the dashboard webchat.

W1 extraction: the whole pipeline that used to live inline in
``endpoints/openai_compat.py::chat_completions`` now lives here so the API
route handler is a thin wrapper and the cookie-realm webchat endpoint can
reuse the exact same semantics (history scoping, memory+continuity
injections that are routed but never persisted, tool-pairing repair,
BYOK-only routing through the single ``router._iter_attempts`` loop,
persistence + runs rows).

Layering: this module is ``core/`` business logic. It never imports
FastAPI, the Router class, or any ``endpoints/`` module — the router,
stores, and the pre-resolved ``byok`` tuple are passed in. ``byok`` is
resolved by the caller via ``endpoints.byok.byok_attempt_source`` (which
is realm-agnostic across the ``api_key``/``session`` per-user kinds).
"""
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from invincible.compat.common import (
    estimate_token_sum,
    repair_tool_pairing,
)
from invincible.core.context_builder import build_context_messages
from invincible.core.principal import Principal
from invincible.core.router import (
    NO_CREDENTIALS_MESSAGE,
    AllProvidersFailedError,
    NoCredentialsConfiguredError,
    UpstreamClientError,
)
from invincible.core.settings import settings
from invincible.core.user_settings_store import override_flag, override_int

logger = logging.getLogger(__name__)


class ChatError(Exception):
    """A chat-pipeline failure already mapped to a wire response."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body
        super().__init__(json.dumps(body)[:300])


@dataclass
class PreparedChat:
    """Everything routing needs, resolved up front (mirrors the old inline order)."""

    session_id: str
    session_pk: int
    full_messages: list
    to_persist: list
    byok_kwargs: dict = field(default_factory=dict)
    max_turns: int | None = None
    user_overrides: dict = field(default_factory=dict)


def _sse_event(data) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _append_content(content: str, chunk: dict) -> str:
    for choice in chunk.get("choices") or []:
        piece = (choice.get("delta") or {}).get("content")
        if piece:
            content += piece
    return content


def chunk_text(chunk: dict) -> str:
    """The new assistant text carried by one OpenAI stream chunk."""
    return _append_content("", chunk)


def web_sse_event(name: str, data: dict) -> str:
    """One named SSE event for the dashboard webchat stream."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


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


async def _persist_new_turns(
    to_persist, assistant_message, store, session_id, memory,
    principal: Principal, *, runs_store=None, request_id: str | None = None,
    max_turns: int | None = None,
):
    """Append this request's new turns, then record the streamed usage.

    ``assistant_message`` is passed separately from the turns it joins
    because the usage estimate must measure THE REPLY ALONE. It used to be
    measured off the completed list, ``to_persist + [assistant]``, which
    counted the caller's own input turns as output (deep code review
    2026-09-24, finding 5).
    """
    new_turns = to_persist + [assistant_message]
    try:
        await store.append(
            session_id,
            new_turns,
            user_id=principal.user_id,
            project_id=principal.project_id,
            max_turns=max_turns,
        )
    except Exception:
        logger.exception("Failed to persist session history for %s", session_id)
    if memory is not None:
        try:
            await memory.record_memories(
                user_id=principal.user_id,
                client_session_id=session_id,
                messages_list=new_turns,
            )
        except Exception:
            logger.exception("Failed to record memories for %s", session_id)
    if runs_store is not None and request_id:
        try:
            await runs_store.attach_output(
                # Streaming never sees real upstream counts without a wire
                # change: a chars/4 estimate of the reply that actually
                # accumulated, flagged in the run row's meta. Measuring the
                # message (not its text) counts tool calls too, which is
                # most of a coding agent's output.
                request_id=request_id,
                output_tokens=estimate_token_sum([assistant_message]),
                estimated=True,
            )
        except Exception as exc:
            logger.warning("Failed to attach stream usage: %s", exc)


async def _stream_body(
    first, tail, store, session_id, to_persist, memory,
    *, principal: Principal, runs_store=None, request_id: str | None = None,
    max_turns: int | None = None,
):
    content = ""
    tool_states = {}

    def assistant_turn():
        return _stream_assistant_message(content, tool_states)

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
        await _persist_new_turns(
            to_persist, assistant_turn(), store, session_id, memory, principal,
            runs_store=runs_store, request_id=request_id,
            max_turns=max_turns,
        )
        return
    await _persist_new_turns(
        to_persist, assistant_turn(), store, session_id, memory, principal,
        runs_store=runs_store, request_id=request_id,
        max_turns=max_turns,
    )
    yield "data: [DONE]\n\n"


def models_from_providers(providers: list) -> list[dict]:
    """Map a provider pool (the router's loaded providers, or a BYOK
    user's credential candidates - same dict shape) to OpenAI /v1/models
    entries.

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


async def prepare_chat(
    messages: list[dict[str, Any]],
    *,
    principal: Principal,
    session_id: str,
    model: str | None,
    sessions,
    retrieval=None,
    continuity=None,
    byok=None,
) -> PreparedChat:
    """Resolve session, load history, build injections, repair pairing.

    ``byok`` is the ``(candidates, key_resolver, routing, overrides)``
    tuple from ``byok_attempt_source`` (or None on the legacy static-pool
    path). Raises :class:`ChatError` for the 400-class failures (unpaired
    tool history, zero connected credentials).
    """
    # Resolve-or-create the owning session row up front so every run
    # record, task read, and history write is scoped to this principal's
    # surrogate session.
    session_pk = await sessions.resolve_or_create(
        session_id,
        user_id=principal.user_id,
        project_id=principal.project_id,
    )

    history = await sessions.load(
        session_id,
        user_id=principal.user_id,
        project_id=principal.project_id,
    )
    user_overrides = {} if byok is None else byok[3]
    # Memory + continuity injections share one budget via the
    # ContextBuilder. Injected system messages are routed but never
    # persisted (system role is excluded below), so they never accumulate.
    injections = await build_context_messages(
        retrieval=(
            retrieval
            if override_flag(user_overrides, "memory", settings.memory_enabled)
            else None
        ),
        continuity_engine=(
            continuity
            if override_flag(
                user_overrides, "continuity", settings.continuity_enabled)
            else None
        ),
        user_id=principal.user_id,
        project_id=principal.project_id,
        session_id=session_id,
        session_pk=session_pk,
        new_messages=messages,
    )
    full_messages = history + injections + messages
    try:
        # Persisted history + the client's replayed messages must satisfy
        # the provider's tool-call pairing invariant before routing; a
        # stored assistant tool_calls turn whose tool result only arrives
        # later is what DeepSeek/vLLM rejects with "insufficient tool
        # messages following tool_calls". Repaired here, or refused with a
        # protocol-correct 400 - never forwarded half-paired.
        full_messages = repair_tool_pairing(full_messages)
    except ValueError as e:
        raise ChatError(400, {"error": {"message": str(e),
                                        "type": "invalid_request_error"}}) from None
    # Clients resend the system prompt on every request; persisting it would
    # accumulate duplicates that trimming never removes (system messages are
    # always kept). Route with it, but only persist the new turns.
    to_persist = [m for m in messages if m.get("role") != "system"]
    if byok is not None and not byok[0]:
        raise ChatError(400, {"error": {"message": NO_CREDENTIALS_MESSAGE,
                                        "type": "invalid_request_error"}})
    byok_kwargs = (
        {} if byok is None
        else {
            "byok_candidates": byok[0],
            "byok_key_resolver": byok[1],
            "byok_routing": byok[2],
            "overrides": byok[3],
        }
    )
    # This user's history turn cap (None = server default).
    max_turns = override_int(
        user_overrides, "history_max_turns", settings.history_max_turns)
    return PreparedChat(
        session_id=session_id,
        session_pk=session_pk,
        full_messages=full_messages,
        to_persist=to_persist,
        byok_kwargs=byok_kwargs,
        max_turns=max_turns,
        user_overrides=user_overrides,
    )


async def run_nonstreaming(
    prepared: PreparedChat,
    *,
    principal: Principal,
    model: str | None,
    sessions,
    memory,
    router,
) -> tuple[dict, dict]:
    """Route one non-streaming completion, persist turns, map errors.

    Returns ``(result_body, route_info)``. Raises :class:`ChatError`
    with the same status/body mapping the inline endpoint used.
    """
    try:
        result, info = await router.route_request_detailed(
            prepared.full_messages, model=model,
            session_id=prepared.session_id,
            session_pk=prepared.session_pk, **prepared.byok_kwargs,
        )
        choices = result.get("choices") or []
        if choices and "message" in choices[0]:
            new_turns = prepared.to_persist + [choices[0]["message"]]
            await sessions.append(
                prepared.session_id,
                new_turns,
                user_id=principal.user_id,
                project_id=principal.project_id,
                max_turns=prepared.max_turns,
            )
            try:
                if memory is not None:
                    await memory.record_memories(
                        user_id=principal.user_id,
                        client_session_id=prepared.session_id,
                        messages_list=new_turns,
                    )
            except Exception:
                logger.exception(
                    "Failed to record memories for %s", prepared.session_id)
        return result, info
    except NoCredentialsConfiguredError:
        # Defensive: the pre-router check in prepare_chat normally catches this.
        raise ChatError(400, {"error": {"message": NO_CREDENTIALS_MESSAGE,
                                        "type": "invalid_request_error"}}) from None
    except UpstreamClientError as e:
        raise ChatError(e.status_code, e.body) from None
    except Exception:
        # Never leak internal exception text (SQL/DSN details) to clients;
        # the full traceback is already in the server log. (This also
        # covers AllProvidersFailedError exhaustion on the non-streaming
        # path, exactly as the inline endpoint did.)
        logger.exception("chat completion failed")
        raise ChatError(503, {"error": {"message": "gateway error",
                                        "type": "gateway_error"}}) from None


async def open_stream(
    prepared: PreparedChat,
    *,
    model: str | None,
    router,
) -> tuple[tuple[Any, Any], dict]:
    """Open a streaming completion; same error mapping as the inline path."""
    try:
        return await router.stream_open_detailed(
            prepared.full_messages, model=model,
            session_id=prepared.session_id,
            session_pk=prepared.session_pk, **prepared.byok_kwargs,
        )
    except NoCredentialsConfiguredError:
        # Defensive: the pre-router check in prepare_chat normally catches this.
        raise ChatError(400, {"error": {"message": NO_CREDENTIALS_MESSAGE,
                                        "type": "invalid_request_error"}}) from None
    except UpstreamClientError as e:
        raise ChatError(e.status_code, e.body) from None
    except AllProvidersFailedError as e:
        raise ChatError(503, {"error": {"message": str(e),
                                        "type": "gateway_error"}}) from None
