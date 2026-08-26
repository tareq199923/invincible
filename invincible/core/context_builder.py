# invincible/core/context_builder.py
"""Unified injection budget (Platform Phase 4).

Everything the gateway adds above the stored history - retrieved memories
and the continuity brief - competes for ONE token budget here, so the
smallest configured provider always fits: ``trim_messages`` keeps system
messages unconditionally, which makes oversized injections the one thing
trimming cannot save us from.

Priority when the budget is tight: the continuity brief wins (canonical,
versioned task state beats fuzzy memory); the memory block fills whatever
remains and is truncated - or dropped - to fit. Fits are computed with the
same ~4-chars-per-token heuristic the Router trims by.
"""
from invincible.core.continuity import context_system_message
from invincible.core.retrieval import RetrievedMemory
from invincible.core.settings import settings
from invincible.core.trimming import estimate_tokens

_TRUNCATION_MARK = " […context trimmed]"


def latest_user_text(messages: list) -> str:
    """The newest user-role string content - the retrieval query."""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
    return ""


def _cut(content: str, token_budget: int) -> str | None:
    """Fit content into ``token_budget`` estimate tokens, or None."""
    if token_budget <= 0:
        return None
    # json wrapper ("role"/"content" keys, quotes, escaping slack).
    overhead = 64
    max_chars = token_budget * 4 - overhead - len(_TRUNCATION_MARK)
    if max_chars <= 0:
        return None
    if len(content) <= max_chars:
        return content
    trimmed = content[:max_chars].rstrip()
    return trimmed + _TRUNCATION_MARK if trimmed else None


def render_memory_block(hits: list[RetrievedMemory]) -> str | None:
    """Render retrieved memories as one injectable system block."""
    if not hits:
        return None
    lines = [
        "[Relevant memory — earlier statements from this user, most"
        " relevant first. They may be stale if contradicted lately.]"
    ]
    lines += [f"- {hit.content}" for hit in hits]
    return "\n".join(lines)


def assemble(
    *,
    memory_hits: list[RetrievedMemory],
    continuity_msg: dict | None,
    budget_tokens: int,
) -> list[dict]:
    """Both injections under one shared budget; pure and hermetic."""
    out: list[dict] = []
    remaining = budget_tokens
    if continuity_msg is not None:
        fitted = _cut(continuity_msg["content"], remaining)
        if fitted is not None:
            msg = {"role": "system", "content": fitted}
            out.append(msg)
            remaining -= min(estimate_tokens(msg), remaining)
    block = render_memory_block(memory_hits)
    if block is not None:
        fitted = _cut(block, remaining)
        if fitted is not None:
            out.append({"role": "system", "content": fitted})
    return out


async def build_context_messages(
    *,
    retrieval,
    continuity_engine,
    user_id: int,
    project_id: int | None,
    session_id: str,
    session_pk: int | None,
    new_messages: list,
    budget_tokens: int | None = None,
) -> list[dict]:
    """Endpoint-facing orchestrator: retrieve, render, assemble.

    Returns zero to two system messages. Never raises into the request
    path beyond what the stores already guarantee; both stores tolerate
    being None (feature off / fixture-less tests).
    """
    query = latest_user_text(new_messages)
    hits: list[RetrievedMemory] = []
    if retrieval is not None and settings.memory_enabled():
        hits = await retrieval.retrieve(
            user_id=user_id, project_id=project_id, query=query
        )
    continuity_msg = await context_system_message(
        continuity_engine, session_id, session_pk=session_pk
    )
    if budget_tokens is None:
        budget_tokens = settings.injection_budget_tokens()
    return assemble(
        memory_hits=hits,
        continuity_msg=continuity_msg,
        budget_tokens=budget_tokens,
    )
