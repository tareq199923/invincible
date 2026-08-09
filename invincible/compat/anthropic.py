# invincible/compat/anthropic.py
"""Pure translation helpers for the Anthropic Messages API.

Converts between the Anthropic wire format and Invincible's internal message
model - nothing more. This module must not import FastAPI or the Router; the
endpoint wires the two together.

Internal message model (shared with the OpenAI compatibility layer):

    [{"role": "system" | "user" | "assistant", "content": str}, …]

plus the OpenAI tool shapes when a conversation uses tools: assistant
messages carry ``tool_calls`` (preserving the Anthropic tool_use id) and
tool results are ``{"role": "tool", "tool_call_id", "content"}`` messages.
"""
import json
import logging
import uuid
from collections.abc import (  # noqa: F401  (AsyncGenerator re-exported for type hints)
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
)

from invincible.compat.common import (
    build_message,
    build_usage,
    estimate_token_sum,
)

logger = logging.getLogger(__name__)

# HTTP status -> Anthropic error type. Anything unmapped becomes api_error.
ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    500: "api_error",
    503: "overloaded_error",
}

# OpenAI finish_reason -> Anthropic stop_reason.
FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}

DEFAULT_STOP_REASON = "end_turn"


def translate_finish_reason(finish_reason: str | None) -> str:
    """Map an OpenAI finish reason to the Anthropic stop_reason vocabulary.

    Unknown or missing reasons map to ``end_turn`` so a stream always closes
    with a valid stop reason. New mappings (``pause_turn``, …) can be added
    here without touching endpoint code.
    """
    if finish_reason is None:
        return DEFAULT_STOP_REASON
    return FINISH_REASON_MAP.get(finish_reason, DEFAULT_STOP_REASON)


def anthropic_tools_to_openai(tools: list | None) -> list | None:
    """Translate Anthropic ``tools[]`` into OpenAI function tools.

    Anthropic and OpenAI both carry tool definitions as JSON Schema, so the
    mapping is structural: ``{name, description, input_schema}`` becomes
    ``{"type": "function", "function": {name, description, parameters}}``.
    Anthropic-only decorations (``cache_control``, ``display_title_pages``,
    …) are dropped. Returns ``None`` for an empty/malformed input so the
    Router only forwards tools the client actually declared.
    """
    if not tools:
        return None
    openai_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        function = {"name": name}
        description = tool.get("description")
        if description:
            function["description"] = description
        schema = tool.get("input_schema")
        if isinstance(schema, dict):
            function["parameters"] = schema
        openai_tools.append({"type": "function", "function": function})
    return openai_tools or None


def translate_tool_choice(choice) -> str | dict | None:
    """Translate an Anthropic ``tool_choice`` into its OpenAI equivalent.

    ``{"type": "auto"}`` and ``{"type": "none"}`` map to the matching
    strings; ``{"type": "any"}`` maps to ``"required"``; a specific tool
    choice becomes the forced-function form. Anything unrecognized returns
    ``None`` so the Router leaves ``tool_choice`` unset rather than risk a
    provider 400 on a form it doesn't understand.
    """
    if choice is None:
        return None
    if isinstance(choice, str):
        return choice if choice in ("auto", "none") else None
    if not isinstance(choice, dict):
        return None
    choice_type = choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "none":
        return "none"
    if choice_type == "any":
        return "required"
    if choice_type == "tool":
        name = choice.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


def flatten_content_blocks(content, role: str) -> str:
    """Flatten an Anthropic ``content`` value into plain text.

    Handles both shapes Anthropic clients send: a plain string, or a list of
    content blocks (``text``, ``tool_use``, ``tool_result``, …).

    Text blocks are concatenated; ``tool_result`` blocks contribute their
    text; ``tool_use`` blocks become a compact placeholder tag so tool
    context survives the round trip without pretending to execute tools.
    Unsupported block types are skipped. Tool-related blocks are never
    silently discarded - they degrade to text.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if text:
                parts.append(str(text))
        elif block_type == "tool_result":
            result = block.get("content", "")
            parts.append(flatten_content_blocks(result, role))
        elif block_type == "tool_use":
            name = block.get("name") or "tool"
            parts.append(f"[tool_use: {name}]")
    return "".join(parts)


def _tool_use_blocks(content) -> list:
    """The ``tool_use`` blocks in an Anthropic content value."""
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _text_blocks(content) -> str:
    """The text-only portion of an Anthropic content value.

    Unlike :func:`flatten_content_blocks`, ``tool_use`` blocks are excluded
    rather than degraded to a placeholder: for assistant messages the tool
    calls are preserved separately as OpenAI ``tool_calls``.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if text:
                parts.append(str(text))
    return "".join(parts)


def _assistant_to_internal(content) -> dict:
    """Translate one assistant message, preserving ``tool_use`` as ``tool_calls``.

    The Anthropic tool_use ``id`` is kept verbatim as the OpenAI tool call id
    so a subsequent ``tool_result`` (which references it via ``tool_use_id``)
    can be mapped to a matching ``role: "tool"`` message losslessly.
    """
    text = _text_blocks(content)
    tool_uses = _tool_use_blocks(content)
    if not tool_uses:
        return build_message("assistant", text)
    tool_calls = []
    for block in tool_uses:
        tool_calls.append(
            {
                "id": block.get("id") or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": block.get("name") or "",
                    "arguments": json.dumps(block.get("input") or {}),
                },
            }
        )
    return {"role": "assistant", "content": text or None, "tool_calls": tool_calls}


def _user_to_internal(content) -> list:
    """Translate one user message into internal messages.

    ``tool_result`` blocks become OpenAI ``role: "tool"`` messages keyed by
    their ``tool_use_id``; any remaining text becomes a trailing user
    message (OpenAI requires tool results to be their own messages, and
    Anthropic puts them at the front of the user content list).
    """
    if isinstance(content, str):
        return [build_message("user", content)] if content else []
    if not isinstance(content, list):
        return [build_message("user", str(content))] if content else []

    tool_results = []
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_result":
            tool_results.append(block)
        elif block_type == "text":
            text = block.get("text")
            if text:
                parts.append(str(text))

    messages = []
    for block in tool_results:
        result_text = flatten_content_blocks(block.get("content", ""), "user")
        messages.append(
            {
                "role": "tool",
                "tool_call_id": block.get("tool_use_id") or f"call_{uuid.uuid4().hex}",
                "content": result_text,
            }
        )
    text = "".join(parts)
    if text:
        messages.append(build_message("user", text))
    return messages


def anthropic_to_internal(messages: list, system=None) -> list:
    """Translate an Anthropic Messages request into internal messages.

    ``system`` may be a string or a list of text blocks; it becomes a
    leading ``system`` message (the Router always keeps system messages).
    ``messages[]`` may also contain ``role == "system"`` entries (which
    Claude Code sends); they are converted into internal ``system`` messages
    exactly like the top-level ``system`` field. ``tool_use`` and
    ``tool_result`` blocks are preserved as OpenAI ``tool_calls`` and
    ``role: "tool"`` messages (the Router's wire format) instead of being
    flattened to text, so tool-bearing conversations round-trip losslessly.
    Messages that translate to nothing are skipped; a request with no usable
    content raises ``ValueError`` so the endpoint can answer with an
    Anthropic ``invalid_request_error``.
    """
    internal: list = []

    if system is not None:
        system_text = flatten_content_blocks(system, "system")
        if system_text:
            internal.append(build_message("system", system_text))

    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Each message must be an object")
        role = message.get("role")
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"Unsupported message role: {role!r}")
        if role == "system":
            system_text = flatten_content_blocks(message.get("content", ""), "system")
            if system_text:
                internal.append(build_message("system", system_text))
            continue
        if role == "user":
            internal.extend(_user_to_internal(message.get("content", "")))
            continue
        translated = _assistant_to_internal(message.get("content", ""))
        if translated.get("content") or translated.get("tool_calls"):
            internal.append(translated)

    if not internal:
        raise ValueError("Request contains no usable text content")
    return internal


def _message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def build_message_skeleton(message_id: str, model: str, input_tokens: int) -> dict:
    """The Anthropic ``message`` skeleton used by ``message_start`` events."""
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": build_usage(input_tokens, 0),
    }


def internal_to_anthropic(
    openai_body: dict, requested_model: str | None, input_tokens: int
) -> dict:
    """Translate an internal (OpenAI-shaped) Router response into an
    Anthropic Messages response.

    Provider ``tool_calls`` become ``tool_use`` content blocks (the id is
    kept verbatim so the client's ``tool_result`` can reference it), and a
    turn that emitted tool calls always closes with ``stop_reason:
    "tool_use"`` - a client like Claude Code only executes tools when it
    sees that stop reason, regardless of the provider's own finish reason.
    The ``model`` field echoes the client's model hint; it never influences
    routing and never requires the provider to expose Claude model names.
    ``usage`` token counts are estimates (the Router's own heuristic) since
    upstream responses may omit usage entirely.
    """
    choices = openai_body.get("choices") or []
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message") or {}
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []

    content_blocks = []
    if content:
        content_blocks.append({"type": "text", "text": content})
    for call in tool_calls:
        function = call.get("function") or {}
        try:
            parsed = json.loads(function.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex}",
                "name": function.get("name") or "function",
                "input": parsed,
            }
        )

    output_tokens = estimate_token_sum([build_message("assistant", content)])
    model = requested_model or openai_body.get("model") or "invincible"
    stop_reason = translate_finish_reason(first_choice.get("finish_reason"))
    if tool_calls:
        stop_reason = "tool_use"

    return {
        "id": _message_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": build_usage(input_tokens, output_tokens),
    }


def build_error(status_code: int, message: str) -> tuple[int, dict]:
    """Build an Anthropic-compatible error response.

    Returns ``(http_status, body)`` where the body is always:

        {"type": "error", "error": {"type": <mapped>, "message": <msg>}}

    The message is the caller's (sanitized) text; upstream provider error
    bodies are never forwarded verbatim.
    """
    error_type = ERROR_TYPE_BY_STATUS.get(status_code, "api_error")
    return status_code, {
        "type": "error",
        "error": {"type": error_type, "message": message},
    }


def sse_frame(event: str, data: dict) -> str:
    """Render one Anthropic SSE event (``event:`` + ``data:`` lines)."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _delta_piece(chunk: dict) -> str:
    """The text delta carried by one OpenAI stream chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("delta") or {}).get("content") or ""


def _delta_finish(chunk: dict) -> str | None:
    """The finish_reason carried by one OpenAI stream chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return None
    return choices[0].get("finish_reason")


def _delta_tool_calls(chunk: dict) -> list:
    """The ``delta.tool_calls`` entries carried by one OpenAI stream chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return []
    return (choices[0].get("delta") or {}).get("tool_calls") or []


async def _complete(
    on_complete: Callable[[dict], Awaitable[None]] | None, message: dict
) -> None:
    if on_complete is not None:
        await on_complete(message)


def _stream_assistant_message(
    reply_text: str, tool_states: dict
) -> dict:
    """Assemble the internal assistant message for a finished stream.

    ``tool_states`` maps upstream ``tool_calls`` index → accumulated
    ``{block_index, id, name, arguments}``. Indexes are deterministic
    (ascending upstream index order), matching the id the client received in
    the SSE frames so the persisted history lines up with what was streamed.
    """
    tool_calls = []
    for idx in sorted(tool_states):
        state = tool_states[idx]
        tool_calls.append(
            {
                "id": state["id"] or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": state["name"] or "",
                    "arguments": state["arguments"] or "{}",
                },
            }
        )
    message = {"role": "assistant", "content": reply_text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


async def build_stream_events(
    first: dict | None,
    tail: AsyncIterator[dict],
    requested_model: str | None,
    input_tokens: int,
    on_complete: Callable[[dict], Awaitable[None]] | None = None,
) -> AsyncGenerator[str, None]:
    """Wrap the Router's OpenAI stream into Anthropic SSE events.

    Yields pre-formatted frames in the canonical Anthropic order:

        message_start → content_block_start → content_block_delta*
        → content_block_stop → message_delta → message_stop

    Content blocks are allocated lazily in first-seen order: text pieces
    open a ``text`` block, and each new upstream ``tool_calls`` index opens
    a ``tool_use`` block whose ``arguments`` pieces are forwarded as
    ``input_json_delta`` frames. A turn that emitted tool calls always
    closes with ``stop_reason: "tool_use"``.

    ``on_complete`` (if given) is awaited exactly once with the accumulated
    assistant message (text + tool_calls) - on success *and* on a mid-stream
    failure - so the caller can persist the session once. A mid-stream
    upstream failure emits a well-formed ``error`` event and stops; the
    stream never emits malformed SSE and always closes.
    """
    message_id = _message_id()
    model = requested_model or "invincible"
    reply_text = ""
    finish_reason = None
    text_started = False
    text_block_index = None
    tool_states: dict = {}
    next_block_index = 0

    def text_block_start():
        nonlocal text_started, text_block_index, next_block_index
        if text_started:
            return
        text_started = True
        text_block_index = next_block_index
        next_block_index += 1
        yield sse_frame(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": text_block_index,
                "content_block": {"type": "text", "text": ""},
            },
        )

    def feed(chunk: dict):
        nonlocal reply_text, finish_reason, next_block_index
        finish_reason = _delta_finish(chunk) or finish_reason
        piece = _delta_piece(chunk)
        if piece:
            if not text_started:
                yield from text_block_start()
            reply_text += piece
            yield sse_frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": text_block_index,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )
        for tool_call in _delta_tool_calls(chunk):
            index = tool_call.get("index", 0)
            state = tool_states.get(index)
            if state is None:
                function = tool_call.get("function") or {}
                block_index = next_block_index
                next_block_index += 1
                state = {
                    "block_index": block_index,
                    "id": tool_call.get("id"),
                    "name": function.get("name"),
                    "arguments": "",
                }
                tool_states[index] = state
                yield sse_frame(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": state["id"] or "",
                            "name": state["name"] or "",
                            "input": {},
                        },
                    },
                )
            function = tool_call.get("function") or {}
            arguments = function.get("arguments")
            if arguments:
                state["arguments"] += arguments
                yield sse_frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": state["block_index"],
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": arguments,
                        },
                    },
                )

    yield sse_frame(
        "message_start",
        {
            "type": "message_start",
            "message": build_message_skeleton(message_id, model, input_tokens),
        },
    )

    try:
        if first is not None:
            for frame in feed(first):
                yield frame
        async for chunk in tail:
            for frame in feed(chunk):
                yield frame
    except Exception as e:
        logger.warning("Anthropic stream terminated after an upstream error: %s", e)
        yield sse_frame(
            "error",
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "Stream terminated unexpectedly",
                },
            },
        )
        await _complete(
            on_complete,
            _stream_assistant_message(reply_text, tool_states),
        )
        return

    for block_index in range(next_block_index):
        yield sse_frame(
            "content_block_stop",
            {"type": "content_block_stop", "index": block_index},
        )
    stop_reason = "tool_use" if tool_states else translate_finish_reason(finish_reason)
    yield sse_frame(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason,
                "stop_sequence": None,
            },
            "usage": build_usage(
                input_tokens,
                estimate_token_sum([build_message("assistant", reply_text)]),
            ),
        },
    )
    yield sse_frame("message_stop", {"type": "message_stop"})
    await _complete(
        on_complete, _stream_assistant_message(reply_text, tool_states)
    )
