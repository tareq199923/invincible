# invincible/core/tool_compression.py
"""Send-time tool-schema compression.

Tool definitions are the half of the payload ``compress_messages`` never
sees: the router forwards ``tools`` verbatim, and coding agents resend
their full toolset on every turn (Claude Code alone ships ~30 schemas
whose multi-paragraph descriptions dominate the tools payload). This
module shrinks the schema text itself, aggressively but conservatively:

1. **Description truncation** — tool-level descriptions are capped
   (default 512 chars, head-only with an explicit marker); descriptions
   inside ``parameters`` are capped tighter (default 160). Names,
   ``required``, types, enums and structure are never touched —
   ``tool_choice`` and model tool-calling depend only on those.
2. **Schema noise stripping** — a fixed allowlist of non-semantic JSON
   Schema keys is removed: ``title``, ``examples``, ``$schema``, ``$id``,
   ``$comment``. Nothing that affects validation (``additionalProperties``,
   ``default``, ``format``, ``enum``) is removed.
3. **Whitespace normalization** — runs of 3+ newlines inside kept
   descriptions collapse to two (same approach as compression.py).

Hard guarantees:

- **Send-time only.** The router sends the result; the caller's tools list
  stays verbatim. The input is never mutated.
- **Cached.** Clients resend an identical tools list every turn, so the
  transform is memoized on the canonical serialization (LRU, 128 entries,
  keyed together with the two caps so live config changes are honored).
  Cached results are shared between requests and must be treated as
  read-only.
- **Never raises.** Toggle off, unserializable input, or any transform
  error returns the original list untouched (with a warning log).
"""
import copy
import functools
import json
import logging
import re
from dataclasses import dataclass

from invincible.core.settings import settings

logger = logging.getLogger("invincible.tool_compression")

_BLANK_RUN = re.compile(r"\n{3,}")

# Fixed allowlist: common in real-world schemas, never affect validation.
_STRIP_KEYS = frozenset({"title", "examples", "$schema", "$id", "$comment"})

_TRUNCATION_MARKER = "…[description truncated]"

# Schema keys whose values are themselves subschemas (or lists of them)
# worth recursing into. ``properties``/``items``/``additionalProperties``
# are handled explicitly below.
_SUBSCHEMA_CONTAINER_KEYS = ("anyOf", "oneOf", "allOf", "$defs", "definitions")


@dataclass
class ToolCompressionStats:
    """Sizes and transform counters for one ``compress_tools`` call."""

    tools_before_bytes: int = 0
    tools_after_bytes: int = 0
    descriptions_truncated: int = 0
    keys_stripped: int = 0
    cache_hit: bool = False


def tool_compression_enabled() -> bool:
    """Whether tool-schema compression is active (default on).

    ``INVINCIBLE_TOOL_COMPRESSION`` values ``0``/``false``/``off`` (any
    case) disable it. Read live via Settings so tests and restarts can
    flip it without rebuilding the Router.
    """
    return settings.tool_compression_enabled()


def _truncate(text: str, max_chars: int) -> str:
    return text[:max_chars] + _TRUNCATION_MARKER


def _normalize_description(node: dict, max_chars: int, stats) -> None:
    description = node.get("description")
    if not isinstance(description, str) or not description:
        return
    new = _BLANK_RUN.sub("\n\n", description)
    if len(new) > max_chars:
        new = _truncate(new, max_chars)
        stats.descriptions_truncated += 1
    if new != description:
        node["description"] = new


def _strip_noise_keys(node: dict, stats) -> None:
    for key in node.keys() & _STRIP_KEYS:
        del node[key]
        stats.keys_stripped += 1


def _walk_schema(node, prop_max: int, stats) -> None:
    """Recursively strip noise keys and cap descriptions in a schema tree."""
    if isinstance(node, dict):
        _strip_noise_keys(node, stats)
        _normalize_description(node, prop_max, stats)
        properties = node.get("properties")
        if isinstance(properties, dict):
            for value in properties.values():
                _walk_schema(value, prop_max, stats)
        for key in ("items", "additionalProperties"):
            value = node.get(key)
            if isinstance(value, (dict, list)):
                _walk_schema(value, prop_max, stats)
        for key in _SUBSCHEMA_CONTAINER_KEYS:
            value = node.get(key)
            if isinstance(value, dict):
                for sub in value.values():
                    _walk_schema(sub, prop_max, stats)
            elif isinstance(value, list):
                for sub in value:
                    _walk_schema(sub, prop_max, stats)
    elif isinstance(node, list):
        for value in node:
            _walk_schema(value, prop_max, stats)


def _compress_tool(tool, tool_max: int, prop_max: int, stats) -> None:
    function = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(function, dict):
        return
    _normalize_description(function, tool_max, stats)
    parameters = function.get("parameters")
    if isinstance(parameters, dict):
        _walk_schema(parameters, prop_max, stats)


def _measure(tools) -> int:
    return len(json.dumps(tools, ensure_ascii=False))


@functools.lru_cache(maxsize=128)
def _compress_cached(
    serialized: str, tool_max: int, prop_max: int
) -> tuple[list, int, int, int]:
    """Memoized transform over the canonical serialization.

    Returns ``(compressed, after_bytes, descriptions_truncated,
    keys_stripped)``. The compressed list is cached and shared between
    calls — it must never be mutated.
    """
    stats = ToolCompressionStats()
    compressed = copy.deepcopy(json.loads(serialized))
    for tool in compressed:
        _compress_tool(tool, tool_max, prop_max, stats)
    after_bytes = _measure(compressed)
    return (
        compressed,
        after_bytes,
        stats.descriptions_truncated,
        stats.keys_stripped,
    )


def compress_tools(tools: list | None) -> tuple[list | None, ToolCompressionStats]:
    """Return ``(tools_for_sending, stats)`` for the router payload.

    Applies the layered transforms above to a deep copy. Never raises and
    never mutates the input: toggle off, unserializable entries, or any
    unexpected error return the original list with ``cache_hit=False``
    (sizes left at 0 when measurement itself was impossible).
    """
    stats = ToolCompressionStats()
    if not tools:
        return tools, stats
    try:
        return _compress_tools_inner(tools, stats)
    except Exception:
        logger.warning(
            "Tool schema compression failed; sending original tools",
            exc_info=True,
        )
        return tools, ToolCompressionStats()


def _compress_tools_inner(
    tools: list, stats: ToolCompressionStats
) -> tuple[list | None, ToolCompressionStats]:
    stats.tools_before_bytes = _measure(tools)
    if not tool_compression_enabled():
        stats.tools_after_bytes = stats.tools_before_bytes
        return tools, stats
    try:
        serialized = json.dumps(tools, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        # Wire-derived tools are always JSON; anything else is sent as-is.
        stats.tools_after_bytes = stats.tools_before_bytes
        return tools, stats
    tool_max = settings.tool_description_max_chars()
    prop_max = settings.tool_property_description_max_chars()
    hits_before = _compress_cached.cache_info().hits
    compressed, after_bytes, truncated, stripped = _compress_cached(
        serialized, tool_max, prop_max
    )
    stats.tools_after_bytes = after_bytes
    stats.descriptions_truncated = truncated
    stats.keys_stripped = stripped
    # lru_cache has no per-call hit/miss result; the global hit counter's
    # delta is the per-call answer (worst case under contention: a miss is
    # reported as a hit — cosmetic only).
    stats.cache_hit = _compress_cached.cache_info().hits > hits_before
    return compressed, stats
