# tests/test_context_builder.py
"""Phase 4 ContextBuilder: one unified token budget for memory + continuity
injections. Pure/hermetic except one gated-orchestrator test.

Acceptance anchor: "total injected context stays within budget even
against the smallest configured provider" - trim_messages keeps system
messages unconditionally, so this budget is the only thing preventing an
oversized injection from blowing a small provider's context.
"""
import pytest

from invincible.core.context_builder import (
    assemble,
    build_context_messages,
    latest_user_text,
    render_memory_block,
)
from invincible.core.retrieval import RetrievedMemory


def hit(content, score=1.0):
    return RetrievedMemory(
        id=hash(content) % 100000,
        content=content,
        kind="fact",
        layer="auto",
        confidence=1.0,
        created_at=0.0,
        score=score,
    )


def continuity(content="Task 'default' (status: active, v3):\n{\"step\": 2}"):
    return {"role": "system", "content": content}


# --- helpers -------------------------------------------------------------------


def test_latest_user_text_picks_newest_user_string():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second question"},
    ]
    assert latest_user_text(msgs) == "second question"


def test_render_memory_block_empty_is_none():
    assert render_memory_block([]) is None


def test_rendered_block_lists_hits_in_order():
    block = render_memory_block([hit("alpha"), hit("beta")])
    assert "[Relevant memory" in block
    assert "- alpha" in block and "- beta" in block


# --- the unified budget ----------------------------------------------------------


def test_both_fit_within_generous_budget():
    out = assemble(
        memory_hits=[hit("name: Sark")],
        continuity_msg=continuity(),
        budget_tokens=1200,
    )
    assert len(out) == 2
    assert "[Relevant memory" in out[-1]["content"]


def test_continuity_wins_when_budget_tight():
    # Enough room for the brief only; the memory block must be dropped.
    out = assemble(
        memory_hits=[hit("name: Sark")],
        continuity_msg=continuity("x" * 400),
        budget_tokens=110,
    )
    assert len(out) == 1
    assert "[Relevant memory" not in out[0]["content"]
    assert "x" in out[0]["content"]


def test_oversized_brief_is_truncated_with_marker():
    out = assemble(
        memory_hits=[],
        continuity_msg=continuity("y" * 800),
        budget_tokens=50,
    )
    assert len(out) == 1
    assert out[0]["content"].endswith("[…context trimmed]")
    # And it actually fits the estimate the Router trims by.
    from invincible.core.trimming import estimate_tokens

    assert estimate_tokens(out[0]) <= 50


def test_zero_budget_yields_nothing():
    assert assemble(
        memory_hits=[hit("anything")], continuity_msg=continuity(),
        budget_tokens=0,
    ) == []


def test_total_never_exceeds_budget_even_with_many_hits():
    hits = [hit(f"kubernetes note {i}") for i in range(20)]
    out = assemble(
        memory_hits=hits, continuity_msg=continuity(), budget_tokens=300
    )
    from invincible.core.trimming import estimate_tokens

    total = sum(estimate_tokens(m) for m in out)
    assert total <= 300


# --- orchestrator gating -----------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_with_stores_absent_returns_empty():
    out = await build_context_messages(
        retrieval=None,
        continuity_engine=None,
        user_id=1,
        project_id=None,
        session_id="s",
        session_pk=None,
        new_messages=[{"role": "user", "content": "hello"}],
    )
    assert out == []
