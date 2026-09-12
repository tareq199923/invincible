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
