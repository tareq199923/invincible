# invincible/compat/common.py
"""Protocol-neutral helpers shared by the compatibility layers.

Everything here operates on the *internal* message model:

    [{"role": "system" | "user" | "assistant", "content": str}, …]

Tool-bearing conversations additionally use OpenAI shapes: assistant
messages may carry ``tool_calls`` and tool results are
``{"role": "tool", "tool_call_id", "content"}`` messages. It must never
depend on FastAPI or the Router.
"""
from invincible.core.trimming import estimate_tokens


def build_message(role: str, content: str) -> dict:
    """Build one internal message from a role and text content."""
    return {"role": role, "content": content}


def build_usage(input_tokens: int, output_tokens: int) -> dict:
    """Build a protocol-neutral usage counter pair."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def estimate_token_sum(messages: list) -> int:
    """Rough total token estimate for a list of internal messages.

    Reuses the shared trimming heuristic (``core.trimming.estimate_tokens``)
    so the compatibility layer never maintains its own token-counting logic.
    Always returns at least 1 per message, identical to the trimmers'
    estimate.
    """
    return sum(estimate_tokens(m) for m in messages)


def upstream_error_detail(body: object, limit: int = 300) -> str | None:
    """Extract a human-readable message from an upstream error body.

    Recognizes the common provider shapes — OpenAI-style
    ``{"error": {"message": ...}}``, plain ``{"error": "..."}``,
    ``{"message": ...}`` / ``{"detail": ...}`` — and returns the text
    capped at ``limit`` characters. Returns ``None`` when nothing
    recognizable is present so the caller keeps its generic message.
    The body comes from a provider API (never internal state), so
    echoing it matches the OpenAI endpoint's verbatim passthrough.
    """
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("type")
    elif isinstance(error, str):
        message = error
    else:
        message = body.get("message") or body.get("detail")
    if not isinstance(message, str) or not message.strip():
        return None
    return message[:limit]


def repair_tool_pairing(messages: list) -> list:
    """Return ``messages`` with every assistant ``tool_calls`` turn paired.

    Chat-completions upstreams validate that each assistant message
    carrying ``tool_calls`` is immediately followed by ``tool`` messages
    covering ALL of its ids, before any non-``tool`` message. A
    half-paired history is rejected outright (DeepSeek/vLLM on NVIDIA NIM
    answers 400 "An assistant message with 'tool_calls' must be followed
    by tool messages responding to each 'tool_call_id'"), so it must
    never be forwarded upstream.

    One deterministic left-to-right pass performs these repairs:

    - an assistant ``tool_calls`` entry with no ``id`` gets a stable
      synthetic id (``call_missing_<message index>_<position>``), so it
      can be paired at all;
    - a ``tool`` message whose ``tool_call_id`` matches an earlier
      under-covered turn is moved directly after that turn (out-of-order
      or interleaved outputs fold back in);
    - a ``tool`` message with a missing/unknown id is bound to the
      nearest under-covered turn's next uncovered call;
    - a ``tool`` message arriving before its assistant call is buffered
      and folded in once that assistant turn appears;
    - a turn whose ids nothing can cover has the dangling ``tool_calls``
      dropped - nothing responds to them, and forwarding them is exactly
      what upstream rejects.

    A ``tool`` message that can be bound to no assistant turn at all is
    unsatisfiable and raises ``ValueError`` naming the offending message
    index, so callers answer with a protocol-correct 400 rather than
    forwarding a request guaranteed to fail.

    Already-valid input comes back unchanged (same dict objects, same
    order), so this is safe to run on every request.
    """
    out: list = []
    turns: list = []
    orphan_tools: list = []

    def _insert(turn: dict, message: dict, bound_id: str) -> None:
        """Place ``message`` directly after ``turn``'s assistant message
        and any tool messages already attached to it, then shift the
        positions of every turn that now sits later in the list."""
        position = turn["position"] + 1 + turn["attached"]
        out.insert(position, message)
        for other in turns:
            if other is not turn and other["position"] >= position:
                other["position"] += 1
        turn["attached"] += 1
        turn["covered"].append(bound_id)

    def _open_slots(turn: dict) -> list:
        return [i for i in turn["ids"] if i not in turn["covered"]]

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{index}] must be an object")

        if message.get("role") != "tool":
            calls = message.get("tool_calls")
            if calls:
                normalized = []
                for position, call in enumerate(calls):
                    if call.get("id"):
                        normalized.append(call)
                    else:
                        normalized.append({
                            **call,
                            "id": f"call_missing_{index}_{position}",
                        })
                message = {**message, "tool_calls": normalized}
                turns.append({
                    "index": index,
                    "ids": [c["id"] for c in normalized],
                    "covered": [],
                    "attached": 0,
                    "position": len(out),
                })
            out.append(message)
            continue

        tool_id = message.get("tool_call_id") or ""
        target = None
        bound_id = tool_id

        if tool_id:
            for turn in reversed(turns):
                if tool_id in turn["ids"]:
                    if tool_id not in turn["covered"]:
                        target = turn
                    break

        if target is None:
            for turn in reversed(turns):
                slots = _open_slots(turn)
                if slots:
                    target = turn
                    bound_id = slots[0]
                    break

        if target is None:
            orphan_tools.append((index, message))
            continue

        if bound_id != tool_id:
            message = {**message, "tool_call_id": bound_id}
        _insert(target, message, bound_id)

    # Buffered outputs: a tool result that arrived before its call can
    # still be folded into the turn that later claimed its id.
    for index, message in list(orphan_tools):
        target = None
        tool_id = message.get("tool_call_id") or ""
        if tool_id:
            for turn in reversed(turns):
                if tool_id in turn["ids"] and tool_id not in turn["covered"]:
                    target = turn
                    break
        if target is None:
            continue
        bound_id = (tool_id if tool_id in target["ids"]
                    else _open_slots(target)[0])
        if bound_id != tool_id:
            message = {**message, "tool_call_id": bound_id}
        _insert(target, message, bound_id)
        orphan_tools.remove((index, message))

    # Nothing can answer a still-uncovered call: drop the dangling
    # tool_calls rather than forward a request the provider will reject.
    for turn in turns:
        slots = _open_slots(turn)
        if not slots:
            continue
        original = out[turn["position"]]
        surviving = [c for c in original.get("tool_calls") or []
                     if c.get("id") not in slots]
        repaired = {k: v for k, v in original.items() if k != "tool_calls"}
        if surviving:
            repaired["tool_calls"] = surviving
        out[turn["position"]] = repaired

    if orphan_tools:
        bad_index = min(index for index, _ in orphan_tools)
        raise ValueError(
            f"messages[{bad_index}] is a tool result whose tool_call_id "
            "matches no assistant tool_calls; the conversation cannot be "
            "paired for a chat-completions upstream"
        )
    return out


def route_headers(route_info: dict | None) -> dict:
    """``x-invincible-*`` response headers describing the attempt that
    actually served the request (Phase 13.5): provider, model, attempt
    count (1 = no failover), and the gateway request id. Empty dict when
    no route info exists (e.g. error paths where the request never
    reached a provider). Purely string-valued; protocol-neutral by design.
    """
    if not route_info:
        return {}
    return {
        "x-invincible-provider": route_info["provider_name"],
        "x-invincible-model": route_info["model_id"],
        "x-invincible-attempts": str(route_info["attempts"]),
        "x-invincible-request-id": route_info["request_id"],
    }
