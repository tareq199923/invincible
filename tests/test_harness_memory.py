# tests/test_harness_memory.py
"""H3: hydration + summarization — hermetic (no Postgres, no Router).

The LLM is always a fake ``complete`` callable: these tests pin the
context shape and the compaction behavior, never a real provider call.
"""
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.harness_runtime import (
    compact_turns,
    hydrate_context,
    summarize_turns,
)


def _msg(role, text):
    return {"role": role, "content": text}


def test_hydrate_order_and_pinning():
    turns = [[_msg("assistant", "working")]]
    injections = [_msg("system", "continuity brief")]
    ctx = hydrate_context(
        "fix refunds", "did A", turns,
        system_prompt="you are triage", injections=injections,
    )
    roles = [(m["role"], m["content"]) for m in ctx]
    assert roles[0] == ("system", "you are triage")
    assert roles[1] == ("user", "fix refunds")  # pinned goal
    assert roles[2][0] == "system" and "did A" in roles[2][1]
    assert roles[3] == ("system", "continuity brief")
    assert roles[4] == ("assistant", "working")  # verbatim tail


def test_hydrate_minimal_is_task_only():
    assert hydrate_context("hi") == [{"role": "user", "content": "hi"}]


async def test_summarize_passes_system_and_transcript():
    seen = {}

    async def complete(system, user):
        seen["system"] = system
        seen["user"] = user
        return "new summary"

    out = await summarize_turns(
        [[_msg("assistant", "did X for item-1")]], "old",
        complete=complete,
    )
    assert out == "new summary"
    assert "terse" in seen["system"]
    assert "file paths" in seen["system"]
    assert "what failed" in seen["system"]
    assert "item-1" in seen["user"]
    assert "old" in seen["user"]


async def test_compact_noop_under_budget():
    async def complete(system, user):  # pragma: no cover
        raise AssertionError("no LLM call when under budget")

    turns = [[_msg("assistant", "short")]]
    kept, summary = await compact_turns(turns, "s", complete=complete)
    assert kept == turns
    assert summary == "s"


async def test_compact_folds_oldest_and_emits():
    bus = HarnessBus()
    big = "x" * 4000
    turns = [
        [_msg("assistant", f"old-{i} {big}")] for i in range(4)
    ] + [[_msg("assistant", "fresh")]]
    calls = []

    async def complete(system, user):
        calls.append(user)
        return "folded summary"

    kept, summary = await compact_turns(
        turns, "prior", complete=complete, max_tokens=3000,
        keep_tokens=1500, bus=bus, workflow_id="wf9",
    )
    assert summary == "folded summary"
    assert len(kept) < len(turns)
    assert kept[-1] == [_msg("assistant", "fresh")]  # newest kept
    assert len(calls) == 1  # exactly one LLM call
    types = [e["type"] for e in bus.history()]
    assert HarnessEventType.MEMORY_COMPACTED in types
    compacted = next(
        e for e in bus.history()
        if e["type"] == HarnessEventType.MEMORY_COMPACTED)
    assert compacted["workflow_id"] == "wf9"
    assert compacted["summarized_turns"] >= 1


def test_summarizer_flag_defaults_off(monkeypatch):
    from invincible.core.settings import settings

    monkeypatch.delenv("INVINCIBLE_HARNESS_SUMMARIZER", raising=False)
    assert settings.harness_summarizer_enabled() is False
    monkeypatch.setenv("INVINCIBLE_HARNESS_SUMMARIZER", "1")
    assert settings.harness_summarizer_enabled() is True
