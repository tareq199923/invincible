# invincible/core/trimming.py
"""Context-budget helpers shared by the Router and compatibility layers
(extracted from ``core/router.py``, Phase 13).

Pure functions over the internal message model:

    [{"role": "system" | "user" | "assistant", "content": str}, …]

plus the OpenAI tool shapes when a conversation uses tools. Nothing here
performs I/O or knows about providers; having them in their own module lets
the session store and compat layer import them without depending on the
Router.
"""
import json

DEFAULT_MAX_CONTEXT = 32000
RESERVE_TOKENS = 1000  # headroom left for the provider's own response


def estimate_tokens(message: dict) -> int:
    """Rough token estimate: ~4 chars per token. This is a heuristic, not an
    exact tokenizer match - it will over/under-count on code-heavy content,
    but it's cheap and good enough to decide what to drop, not to bill by."""
    return max(1, len(json.dumps(message)) // 4)


def group_into_turns(messages: list) -> list:
    """Group non-system messages into turns, where a new turn starts at each
    user message. This keeps an assistant's tool_calls together with the
    tool result message(s) that answer them and the eventual assistant
    follow-up, since all of those belong to the same user turn and must
    never be split apart when trimming."""
    turns = []
    current = []
    for m in messages:
        if m.get("role") == "user" and current:
            turns.append(current)
            current = []
        current.append(m)
    if current:
        turns.append(current)
    return turns


def trim_messages(
    messages: list,
    max_context: int,
    reserve_tokens: int = RESERVE_TOKENS,
) -> list:
    """Keep all system messages, then keep as many of the most recent turns
    as fit inside max_context (minus reserve_tokens for the response).
    Always keeps at least the single most recent turn, even if it alone
    exceeds budget - there's nothing better to send in that case.
    Turns are dropped as atomic units so a tool_call is never separated
    from its tool result."""
    system_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    def turn_tokens(turn):
        return sum(estimate_tokens(m) for m in turn)

    system_tokens = sum(estimate_tokens(m) for m in system_msgs)
    budget = max(max_context - reserve_tokens - system_tokens, 0)

    turns = group_into_turns(rest)
    if not turns:
        return system_msgs

    kept = [turns[-1]]
    used = turn_tokens(turns[-1])

    for turn in reversed(turns[:-1]):
        t = turn_tokens(turn)
        if used + t > budget:
            break
        kept.insert(0, turn)
        used += t

    return system_msgs + [m for turn in kept for m in turn]
