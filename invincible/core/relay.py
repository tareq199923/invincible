# invincible/core/relay.py
"""Context relay: digest old turns into one synthetic system message.

Where ``compress_messages`` shrinks individual tool results in place and
``trim_messages`` drops whole oldest turns, relay recovers the middle
ground: once the conversation's estimated size crosses a threshold, all
but the newest turns are replaced by a single structured digest — per
turn, the user's text (truncated), the tools invoked (arguments
truncated), and how many tool results were elided. Old tool results are
the dominant, least-used cost in tool-heavy sessions.

Hard guarantees:

- **Turn atomicity.** Turns are grouped with ``group_into_turns`` and the
  newest ``INVINCIBLE_RELAY_KEEP_TURNS`` are kept verbatim, so an
  assistant ``tool_calls`` is never separated from its results. The
  digest itself contains no ``tool_calls``/``tool_call_id``, so a digested
  turn can never dangle.
- **System messages pass through untouched.** Injected memory/continuity
  system messages and the client's own system prompt arrive at the front
  of the message list; they are extracted before grouping and never
  digested. The digest is emitted after them, so once ``trim_messages``
  re-hoists system messages to the front, the digest lands after the
  client's system prompt and before the kept turns.
- **Bounded digest.** Only the newest ``INVINCIBLE_RELAY_DIGEST_MAX_ENTRIES``
  digested turns get individual entries; older ones collapse into a count
  line. Trimming never drops system messages, so the digest must not grow
  with the history it summarizes.
- **Send-time only.** Same contract as compression.py: callers send the
  result but never persist it; stored history stays verbatim. The input
  is never mutated.
- **Never raises.** Toggle off, below-threshold history, or any error
  returns the original messages unchanged (with a warning log).
"""
import json
import logging
from dataclasses import dataclass

from invincible.core.settings import settings
from invincible.core.trimming import estimate_tokens, group_into_turns

logger = logging.getLogger("invincible.relay")

# Per-entry size caps (chars): enough to recognize a turn, far below the
# cost of what it summarizes.
_USER_TEXT_CHARS = 200
_TOOL_ARGS_CHARS = 80

_DIGEST_HEADER = (
    "[Context relay] Earlier turns of this conversation were compacted to "
    "save context space. Digest of the older turns:"
)


@dataclass
class RelayStats:
    """Outcome of one ``relay_messages`` call."""

    applied: bool = False
    turns_digested: int = 0
    digest_chars: int = 0


def relay_enabled() -> bool:
    """Whether context relay is active (default on).

    ``INVINCIBLE_RELAY`` values ``0``/``false``/``off`` (any case) disable
    it. Read live via Settings so tests and restarts can flip it without
    rebuilding the Router.
    """
    return settings.relay_enabled()


def _text_of(message: dict) -> str:
    """Flat text of a message content (string or OpenAI content parts)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def _tool_calls_of(message: dict) -> list[str]:
    """``name(args…)`` renderings of an assistant message's tool calls."""
    rendered = []
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return rendered
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            continue
        args = function.get("arguments")
        if isinstance(args, dict):
            try:
                args = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                args = ""
        if not isinstance(args, str):
            args = ""
        if len(args) > _TOOL_ARGS_CHARS:
            args = args[:_TOOL_ARGS_CHARS] + "…"
        rendered.append(f"{name}({args})" if args else name)
    return rendered


def _digest_entry(turn: list) -> str | None:
    """One digest bullet for a turn, or None when it has nothing to say."""
    user_text = ""
    tool_calls: list[str] = []
    tool_results = 0
    for message in turn:
        role = message.get("role")
        if role == "user" and not user_text:
            user_text = _text_of(message).strip()
        elif role == "assistant":
            tool_calls.extend(_tool_calls_of(message))
        elif role == "tool":
            tool_results += 1
    if not (user_text or tool_calls or tool_results):
        return None
    lines = []
    if user_text:
        if len(user_text) > _USER_TEXT_CHARS:
            user_text = user_text[:_USER_TEXT_CHARS] + "…"
        lines.append(f'- user: "{user_text}"')
    if tool_calls:
        lines.append(f"  tools: {', '.join(tool_calls)}")
    if tool_results:
        lines.append(f"  ({tool_results} tool result(s) elided)")
    return "\n".join(lines)


def relay_messages(messages: list) -> tuple[list, RelayStats]:
    """Return ``(messages_for_sending, stats)``.

    Below the token threshold (or with too few turns to digest) the input
    list is returned unchanged. Never raises and never mutates the input.
    """
    try:
        return _relay(messages)
    except Exception:
        logger.warning(
            "Context relay failed; sending full history", exc_info=True
        )
        return messages, RelayStats()


def _relay(messages: list) -> tuple[list, RelayStats]:
    if not relay_enabled():
        return messages, RelayStats()
    systems = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    total_tokens = sum(estimate_tokens(m) for m in rest)
    if total_tokens <= settings.relay_threshold_tokens():
        return messages, RelayStats()
    keep = settings.relay_keep_turns()
    turns = group_into_turns(rest)
    if len(turns) <= keep:
        return messages, RelayStats()
    old_turns, kept_turns = turns[:-keep], turns[-keep:]
    max_entries = settings.relay_digest_max_entries()
    skipped = max(0, len(old_turns) - max_entries)
    lines = [_DIGEST_HEADER]
    if skipped:
        lines.append(f"({skipped} earlier turn(s) omitted entirely.)")
    for turn in old_turns[-max_entries:]:
        entry = _digest_entry(turn)
        if entry:
            lines.append(entry)
    digest = {"role": "system", "content": "\n".join(lines)}
    relayed = systems + [digest] + [m for turn in kept_turns for m in turn]
    stats = RelayStats(
        applied=True,
        turns_digested=len(old_turns),
        digest_chars=len(digest["content"]),
    )
    return relayed, stats
