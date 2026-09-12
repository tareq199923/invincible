# invincible/compat/responses.py
"""Pure translation helpers for the OpenAI Responses API.

Converts between the Responses wire format (spoken by Codex and other
OpenAI SDK clients) and Invincible's internal message model - nothing
more. This module must not import FastAPI or the Router; the endpoint
wires the two together.

Internal message model (shared with the OpenAI/Anthropic compatibility
layers):

    [{"role": "system" | "user" | "assistant", "content": str}, …]

plus the OpenAI tool shapes when a conversation uses tools: assistant
messages carry ``tool_calls`` and tool results are ``{"role": "tool",
"tool_call_id", "content"}`` messages.

Responses input items map onto that model:

    {type: "message", role, content[] | str}
        -> one internal message (input_text/output_text blocks join)
    {type: "function_call", call_id, name, arguments}
        -> assistant message with tool_calls (call_id kept verbatim)
    {type: "function_call_output", call_id, output}
        -> {"role": "tool", "tool_call_id": call_id, ...}
    {type: "reasoning"} and unknown types
        -> skipped (chat-completions upstreams have no channel for them)
"""
import json
import logging
import time
import uuid
from collections.abc import (  # noqa: F401  (AsyncGenerator re-exported for type hints)
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
)

from invincible.compat.common import (
    build_message,
    estimate_token_sum,
)

logger = logging.getLogger(__name__)

# HTTP status -> OpenAI error type. Anything unmapped becomes server_error.
ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    409: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "server_error",
    503: "server_error",
}

# OpenAI finish_reason -> Responses status. Tool-call turns are reported
# "completed" (Codex executes tools from the output items, not a status).
FINISH_REASON_TO_STATUS = {
    "stop": "completed",
    "length": "incomplete",
    "content_filter": "incomplete",
    "tool_calls": "completed",
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _content_text(content) -> str:
    """Flatten one Responses message ``content`` into plain text.

    Handles both shapes clients send: a plain string, or a list of content
    parts (``input_text``, ``output_text``, ``summary_text``, …). Unknown
    part types contribute nothing; image/file parts are not supported on
    the chat-completions wire and degrade to their text siblings only.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict):
            text = part.get("text")
            if text:
                parts.append(str(text))
    return "".join(parts)


def _message_item_to_internal(item: dict) -> dict | None:
    """Translate one ``{type: "message"}`` input item."""
    role = item.get("role")
    if role == "developer":
        role = "system"
    if role not in ("user", "assistant", "system"):
        return None
    text = _content_text(item.get("content"))
    if role == "assistant":
        return {"role": "assistant", "content": text or None}
    return build_message(role, text) if text else None


def _function_call_to_internal(item: dict) -> dict | None:
    """Translate one ``{type: "function_call"}`` input item.

    The Responses ``call_id`` is kept verbatim as the OpenAI tool call id
    so the following ``function_call_output`` (which references it via the
    same ``call_id``) maps to a matching ``role: "tool"`` message
    losslessly - the same trick the Anthropic layer uses with ``toolu_``
    ids.
    """
    name = item.get("name")
    if not name:
        return None
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {})
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": item.get("call_id") or _new_id("call"),
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def _function_call_output_to_internal(item: dict) -> dict | None:
    """Translate one ``{type: "function_call_output"}`` input item.

    ``output`` is a plain string on the wire; some clients wrap it as
    ``{"content": ...}`` or ``{"output": ...}`` - both flatten.
    """
    call_id = item.get("call_id")
    if not call_id:
        return None
    output = item.get("output")
    if isinstance(output, dict):
        output = output.get("content", output.get("output", ""))
    if not isinstance(output, str):
        output = json.dumps(output) if output is not None else ""
    return {"role": "tool", "tool_call_id": call_id, "content": output}


def responses_to_internal(input_value, instructions=None) -> list:
    """Translate a Responses request into internal messages.

    ``instructions`` becomes a leading ``system`` message (the Router
    always keeps system messages). ``input`` may be a plain string (one
    user message) or the item list Codex sends. Messages that translate
    to nothing are skipped; a request with no usable content raises
    ``ValueError`` so the endpoint can answer with an OpenAI
    ``invalid_request_error``.
    """
    internal: list = []

    if instructions:
        internal.append(build_message("system", instructions))

    if isinstance(input_value, str):
        if input_value:
            internal.append(build_message("user", input_value))
    elif isinstance(input_value, list):
        for item in input_value:
            if not isinstance(item, dict):
                raise ValueError("Each input item must be an object")
            item_type = item.get("type")
            if item_type == "message":
                translated = _message_item_to_internal(item)
            elif item_type == "function_call":
                translated = _function_call_to_internal(item)
            elif item_type == "function_call_output":
                translated = _function_call_output_to_internal(item)
            else:
                # "reasoning", "web_search_call", … - nothing to route.
                continue
            if translated is not None:
                internal.append(translated)
    else:
        raise ValueError("input must be a string or a list of items")

    if not internal:
        raise ValueError("Request contains no usable text content")
    return internal


def responses_tools_to_openai(tools: list | None) -> list | None:
    """Translate Responses ``tools[]`` into OpenAI function tools.

    Responses tool definitions are FLAT (``{type: "function", name,
    description, parameters}``); chat-completions tools nest the same
    fields under ``function``. Responses-only decorations (``strict``,
    ``enable_thinking``, …) are dropped. Returns ``None`` for an
    empty/malformed input so the Router only forwards tools the client
    actually declared.
    """
    if not tools:
        return None
    openai_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            # web_search/file_search/… have no chat-completions shape.
            continue
        name = tool.get("name")
        if not name:
            continue
        function = {"name": name}
        description = tool.get("description")
        if description:
            function["description"] = description
        schema = tool.get("parameters")
        if isinstance(schema, dict):
            function["parameters"] = schema
        openai_tools.append({"type": "function", "function": function})
    return openai_tools or None


def translate_tool_choice(choice) -> str | dict | None:
    """Translate a Responses ``tool_choice`` into its chat-completions
    equivalent.

    ``"auto"``/``"none"``/``"required"`` pass through; the Responses
    forced-function form (``{"type": "function", "name"}`` - flat, no
    nesting) becomes the chat-completions forced-function form. Anything
    unrecognized returns ``None`` so the Router leaves ``tool_choice``
    unset rather than risk a provider 400.
    """
    if choice is None:
        return None
    if isinstance(choice, str):
        return choice if choice in ("auto", "none", "required") else None
    if not isinstance(choice, dict):
        return None
    if choice.get("type") == "function":
        name = choice.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


def _output_items_from_message(message: dict) -> list:
    """The Responses output items for one assistant chat message.

    Text becomes a ``message`` item with one ``output_text`` part; each
    ``tool_calls`` entry becomes a ``function_call`` item whose
    ``call_id`` echoes the provider's tool call id (Codex references it
    verbatim in its ``function_call_output``).
    """
    items = []
    content = message.get("content") or ""
    if content:
        items.append(
            {
                "type": "message",
                "id": _new_id("msg"),
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": content,
                     "annotations": []}
                ],
            }
        )
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        items.append(
            {
                "type": "function_call",
                "id": _new_id("fc"),
                "call_id": call.get("id") or _new_id("call"),
                "name": function.get("name") or "function",
                "arguments": function.get("arguments") or "{}",
                "status": "completed",
            }
        )
    return items


def internal_to_responses(
    openai_body: dict, requested_model: str | None, input_tokens: int
) -> dict:
    """Translate an internal (OpenAI-shaped) Router response into a
    Responses API response object.

    Provider ``tool_calls`` become ``function_call`` output items and the
    ``model`` field echoes the client's model hint (it never influences
    routing and never requires the provider to expose the same names).
    ``usage`` counts are estimates (the Router's own heuristic) since
    upstream responses may omit usage entirely.
    """
    choices = openai_body.get("choices") or []
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message") or {}
    finish_reason = first_choice.get("finish_reason")
    status = FINISH_REASON_TO_STATUS.get(finish_reason or "stop",
                                         "completed")

    output = _output_items_from_message(message)
    content = message.get("content") or ""
    output_tokens = estimate_token_sum(
        [build_message("assistant", content)])

    return {
        "id": _new_id("resp"),
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": requested_model or openai_body.get("model")
        or "invincible",
        "output": output,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def build_error(status_code: int, message: str) -> tuple[int, dict]:
    """Build an OpenAI-compatible error response.

    Returns ``(http_status, body)`` where the body is always:

        {"error": {"message": <msg>, "type": <mapped>, "code": None}}

    The message is the caller's (sanitized) text; upstream provider error
    bodies are never forwarded verbatim.
    """
    error_type = ERROR_TYPE_BY_STATUS.get(status_code, "server_error")
    return status_code, {
        "error": {"message": message, "type": error_type, "code": None}
    }


def sse_frame(event: str, data: dict) -> str:
    """Render one Responses SSE event (``event:`` + ``data:`` lines)."""
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
    """The ``delta.tool_calls`` entries carried by one OpenAI stream
    chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return []
    return (choices[0].get("delta") or {}).get("tool_calls") or []


async def _complete(
    on_complete: Callable[[dict], Awaitable[None]] | None, message: dict
) -> None:
    if on_complete is not None:
        await on_complete(message)


def _stream_assistant_message(reply_text: str, tool_states: dict) -> dict:
    """Assemble the internal assistant message for a finished stream.

    ``tool_states`` maps upstream ``tool_calls`` index → accumulated
    ``{call_id, name, arguments}``. Call ids are deterministic (ascending
    upstream index order), matching what the client received in the SSE
    frames so the persisted history lines up with what was streamed.
    """
    tool_calls = []
    for idx in sorted(tool_states):
        state = tool_states[idx]
        tool_calls.append(
            {
                "id": state["call_id"] or _new_id("call"),
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


def _message_item(item_id: str, text: str) -> dict:
    """A completed ``message`` output item (for response.completed)."""
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "status": "completed",
        "content": [
            {"type": "output_text", "text": text, "annotations": []}
        ],
    }


def _function_call_item(item_id: str, call_id: str, name: str,
                        arguments: str) -> dict:
    """A completed ``function_call`` output item."""
    return {
        "type": "function_call",
        "id": item_id,
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


async def build_stream_events(
    first: dict | None,
    tail: AsyncIterator[dict],
    requested_model: str | None,
    input_tokens: int,
    on_complete: Callable[[dict], Awaitable[None]] | None = None,
) -> AsyncGenerator[str, None]:
    """Wrap the Router's OpenAI stream into Responses SSE events.

    Yields pre-formatted frames in the canonical Responses order:

        response.created → response.output_item.added →
        response.content_part.added → response.output_text.delta* →
        response.output_text.done → response.content_part.done →
        response.output_item.done → … → response.completed

    Output items are allocated lazily in first-seen order: the first text
    piece opens a ``message`` item (plus its ``output_text`` part), and
    each new upstream ``tool_calls`` index opens a ``function_call`` item
    whose argument pieces are forwarded as
    ``response.function_call_arguments.delta`` frames. ``response.completed``
    carries the fully-assembled response object (status, output items,
    usage) exactly like the non-streaming shape.

    ``on_complete`` (if given) is awaited exactly once with the
    accumulated assistant message (text + tool_calls) - on success *and*
    on a mid-stream failure - so the caller can persist the session once.
    A mid-stream upstream failure emits a well-formed ``error`` event and
    stops; the stream never emits malformed SSE and always closes.
    """
    response_id = _new_id("resp")
    model = requested_model or "invincible"
    reply_text = ""
    finish_reason = None
    text_item_id = None
    text_output_index = None
    text_started = False
    tool_states: dict = {}
    completed_items: list = []
    next_output_index = 0

    def _response_skeleton(status: str, output: list) -> dict:
        return {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": model,
            "output": output,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0,
                      "total_tokens": input_tokens},
        }

    def feed(chunk: dict):
        """Yield the frames for one upstream chunk (a plain generator so
        the caller can ``yield from`` it inside the async loop)."""
        nonlocal reply_text, finish_reason, text_started
        nonlocal text_item_id, text_output_index, next_output_index
        finish_reason = _delta_finish(chunk) or finish_reason
        piece = _delta_piece(chunk)
        if piece:
            if not text_started:
                text_started = True
                text_item_id = _new_id("msg")
                text_output_index = next_output_index
                next_output_index += 1
                yield sse_frame(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": text_output_index,
                        "item": {
                            "type": "message",
                            "id": text_item_id,
                            "role": "assistant",
                            "status": "in_progress",
                            "content": [],
                        },
                    },
                )
                yield sse_frame(
                    "response.content_part.added",
                    {
                        "type": "response.content_part.added",
                        "item_id": text_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "",
                                 "annotations": []},
                    },
                )
            reply_text += piece
            yield sse_frame(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": text_item_id,
                    "output_index": text_output_index,
                    "content_index": 0,
                    "delta": piece,
                },
            )
        for tool_call in _delta_tool_calls(chunk):
            index = tool_call.get("index", 0)
            state = tool_states.get(index)
            if state is None:
                function = tool_call.get("function") or {}
                state = {
                    "output_index": next_output_index,
                    "item_id": _new_id("fc"),
                    "call_id": tool_call.get("id"),
                    "name": function.get("name"),
                    "arguments": "",
                }
                next_output_index += 1
                tool_states[index] = state
                yield sse_frame(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": state["output_index"],
                        "item": {
                            "type": "function_call",
                            "id": state["item_id"],
                            "call_id": state["call_id"] or "",
                            "name": state["name"] or "",
                            "arguments": "",
                            "status": "in_progress",
                        },
                    },
                )
            function = tool_call.get("function") or {}
            arguments = function.get("arguments")
            if arguments:
                state["arguments"] += arguments
                yield sse_frame(
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": state["item_id"],
                        "output_index": state["output_index"],
                        "delta": arguments,
                    },
                )

    yield sse_frame(
        "response.created",
        {"type": "response.created",
         "response": _response_skeleton("in_progress", [])},
    )

    try:
        if first is not None:
            for frame in feed(first):
                yield frame
        async for chunk in tail:
            for frame in feed(chunk):
                yield frame
    except Exception as e:
        logger.warning("Responses stream terminated after an upstream "
                       "error: %s", e)
        yield sse_frame(
            "error",
            {
                "type": "error",
                "code": "stream_error",
                "message": "Stream terminated unexpectedly",
            },
        )
        await _complete(
            on_complete,
            _stream_assistant_message(reply_text, tool_states),
        )
        return

    # Close the open items in first-seen order: text part, text item,
    # then each function_call item.
    if text_started:
        yield sse_frame(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": text_item_id,
                "output_index": text_output_index,
                "content_index": 0,
                "text": reply_text,
            },
        )
        yield sse_frame(
            "response.content_part.done",
            {
                "type": "response.content_part.done",
                "item_id": text_item_id,
                "output_index": text_output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": reply_text,
                         "annotations": []},
            },
        )
        completed_items.append(_message_item(text_item_id, reply_text))

    for index in sorted(tool_states):
        state = tool_states[index]
        call_id = state["call_id"] or _new_id("call")
        arguments = state["arguments"] or "{}"
        yield sse_frame(
            "response.function_call_arguments.done",
            {
                "type": "response.function_call_arguments.done",
                "item_id": state["item_id"],
                "output_index": state["output_index"],
                "arguments": arguments,
            },
        )
        yield sse_frame(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": state["output_index"],
                "item": _function_call_item(
                    state["item_id"], call_id, state["name"] or "",
                    arguments),
            },
        )
        completed_items.append(_function_call_item(
            state["item_id"], call_id, state["name"] or "", arguments))

    if text_started:
        yield sse_frame(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": text_output_index,
                "item": _message_item(text_item_id, reply_text),
            },
        )

    output_tokens = estimate_token_sum(
        [build_message("assistant", reply_text)])
    final = _response_skeleton("completed", completed_items)
    final["usage"] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    yield sse_frame(
        "response.completed",
        {"type": "response.completed", "response": final},
    )
    await _complete(
        on_complete, _stream_assistant_message(reply_text, tool_states)
    )
