# invincible/core/compression.py
"""Send-time request compression (Roadmap Phase 9).

Shrinks the message list *before* ``trim_messages`` decides what fits the
provider's context budget, so fewer turns are dropped and fewer tokens are
sent per request. Two rules, both deliberately conservative:

1. **Tool-result truncation** — long ``role: "tool"`` contents are the
   dominant token cost in tool-heavy sessions; keep the head and tail with
   an explicit marker in the middle.
2. **Blank-line collapsing** — runs of 3+ newlines collapse to two in any
   string content (harmless for prose and code, recovers real tokens from
   verbose tool output and pasted logs).

Hard guarantees:

- **Send-time only.** Callers must pass the result to the provider and
  never persist it; stored history stays verbatim (compressing stored
  turns would progressively degrade them on every round trip).
- **Structure-preserving.** Roles, ``tool_calls``, ``tool_call_id`` and
  message order are untouched, so ``group_into_turns``' tool-turn
  atomicity is unaffected. Input messages are never mutated.
- **Toggleable.** On by default; set ``INVINCIBLE_COMPRESSION=0`` (or
  ``false``/``off``) to disable. Read per call so tests and restarts can
  flip it without rebuilding the Router.
"""
import os
import re

# Tool results longer than this are truncated to head + marker + tail.
DEFAULT_TOOL_RESULT_MAX_CHARS = 4000
# Head/tail split of the kept budget (tail keeps the end of logs, where
# errors usually live).
_TOOL_HEAD_FRACTION = 0.6

_BLANK_RUN = re.compile(r"\n{3,}")

TRUNCATION_MARKER = "\n…[middle {dropped} chars compressed away]…\n"


def compression_enabled() -> bool:
    """Whether send-time compression is active (default on).

    ``INVINCIBLE_COMPRESSION`` values ``0``/``false``/``off`` (any case)
    disable it; anything else (including unset) enables it.
    """
    return os.getenv("INVINCIBLE_COMPRESSION", "").strip().lower() not in (
        "0",
        "false",
        "off",
    )


def _collapse_blank_runs(text: str) -> str:
    return _BLANK_RUN.sub("\n\n", text)


def _truncate_tool_content(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    budget = max_chars
    head = int(budget * _TOOL_HEAD_FRACTION)
    tail = budget - head
    dropped = len(content) - head - tail
    marker = TRUNCATION_MARKER.format(dropped=dropped)
    return content[:head] + marker + content[len(content) - tail:]


def compress_messages(
    messages: list,
    tool_result_max_chars: int = DEFAULT_TOOL_RESULT_MAX_CHARS,
) -> list:
    """Return a compressed copy of ``messages`` for sending only.

    Every returned message is a new dict; the input list and its messages
    are never mutated, so callers can safely persist the originals.
    """
    compressed = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or not content:
            compressed.append(m)
            continue
        new_content = _collapse_blank_runs(content)
        if m.get("role") == "tool":
            new_content = _truncate_tool_content(new_content, tool_result_max_chars)
        if new_content == content:
            compressed.append(m)
        else:
            compressed.append({**m, "content": new_content})
    return compressed
