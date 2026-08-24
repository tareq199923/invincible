# tests/test_continuity_engine.py
"""ContinuityEngine unit tests (Phase 15b): CAS versioning, checkpoints,
continuation-brief rendering, interruption signal.

All state lives in the shared ``pg_engine`` test database; teardown
truncation keeps tests isolated (no store handles to leak).
"""

import pytest

from invincible.core.continuity import (
    ContinuityConflictError,
    ContinuityEngine,
)
from invincible.core.run_store import RunStore


@pytest.fixture
async def stack(pg_engine):
    runs = RunStore(engine=pg_engine)
    engine = ContinuityEngine(engine=pg_engine, runs=runs)
    try:
        yield pg_engine, runs, engine
    finally:
        await engine.close()
        await runs.close()


def run_entry(request_id, outcome="ok", provider="alpha",
              finished_at=None, offset=0.0):
    import time as _time

    finished = finished_at if finished_at is not None else _time.time() + offset
    return {
        "request_id": request_id,
        "session_id": "s",
        "provider_name": provider,
        "model_id": "m",
        "attempt_index": 1,
        "outcome": outcome,
        "error_class": "500" if outcome != "ok" else None,
        "started_at": finished - 1,
        "finished_at": finished,
    }


# ---------------------------------------------------------------- state


async def test_set_get_roundtrip(stack):
    _, _, eng = stack
    head = await eng.set_state("s", {"next": 6}, actor="llm:beta",
                               task_key="count")
    assert head["version"] == 1 and head["status"] == "active"
    got = await eng.get_state("s", "count")
    assert got["payload"] == {"next": 6}
    assert got["version"] == 1 and got["updated_by"] == "llm:beta"


async def test_versions_monotonic_across_writes_and_actors(stack):
    _, _, eng = stack
    await eng.set_state("s", {"n": 1}, actor="llm:a")
    await eng.set_state("s", {"n": 2}, actor="mcp:task_state_set")
    head = await eng.set_state("s", {"n": 3}, actor="user")
    assert head["version"] == 3
    history = await eng.history("s")
    assert [h["version"] for h in history] == [3, 2, 1]
    assert history[0]["updated_by"] == "user"


async def test_cas_conflict_on_stale_version(stack):
    _, _, eng = stack
    await eng.set_state("s", {"v": 1}, actor="a", expected_version=0)
    with pytest.raises(ContinuityConflictError, match="current head"):
        await eng.set_state("s", {"v": 2}, actor="b", expected_version=0)
    ok = await eng.set_state("s", {"v": 2}, actor="b", expected_version=1)
    assert ok["version"] == 2


async def test_task_keys_isolated(stack):
    _, _, eng = stack
    await eng.set_state("s", {"a": 1}, actor="x", task_key="alpha")
    await eng.set_state("s", {"b": 2}, actor="x", task_key="beta")
    assert (await eng.get_state("s", "alpha"))["payload"] == {"a": 1}
    assert (await eng.get_state("s", "beta"))["payload"] == {"b": 2}
    assert await eng.get_state("s", "missing") is None


async def test_status_validation_and_transitions_in_history(stack):
    _, _, eng = stack
    with pytest.raises(ValueError, match="status must be one of"):
        await eng.set_state("s", {}, actor="x", status="wibble")
    await eng.set_state("s", {"p": 1}, actor="x", status="active")
    await eng.set_state("s", {"p": 2}, actor="x", status="done")
    statuses = [h["status"] for h in await eng.history("s")]
    assert statuses == ["done", "active"]
    assert (await eng.get_state("s"))["status"] == "done"


async def test_payload_constraints(stack):
    _, _, eng = stack
    with pytest.raises(ValueError, match="JSON object"):
        await eng.set_state("s", ["not", "a", "dict"], actor="x")
    with pytest.raises(ValueError, match="exceeds"):
        await eng.set_state("s", {"big": "x" * 5000}, actor="x")


async def test_active_task_keys_most_recent_first(stack):
    import asyncio as _aio

    _, _, eng = stack
    await eng.set_state("s", {"k": 1}, actor="x", task_key="first")
    await _aio.sleep(0.01)
    await eng.set_state("s", {"k": 2}, actor="x", task_key="second")
    assert await eng.active_task_keys("s") == ["second", "first"]


# ---------------------------------------------------------- checkpoints


async def test_checkpoint_pins_current_version_and_lists_newest_first(stack):
    _, _, eng = stack
    cp0 = await eng.create_checkpoint("s", note="started", actor="user")
    assert cp0["state_version"] == 0  # nothing tracked yet
    await eng.set_state("s", {"through": 5}, actor="llm:a")
    cp1 = await eng.create_checkpoint("s", note="through 5", actor="llm:a")
    assert cp1["state_version"] == 1

    cps = await eng.checkpoints("s")
    assert [c["id"] for c in cps] == [cp1["id"], cp0["id"]]
    assert cps[0]["note"] == "through 5"

    other = await eng.create_checkpoint("s2", task_key="other", note="")
    scoped = await eng.checkpoints("s2", task_key="other")
    assert [c["id"] for c in scoped] == [other["id"]]


# ------------------------------------------------- continuation brief


async def test_context_empty_without_state(stack):
    _, _, eng = stack
    assert await eng.context_message("s") is None


async def test_context_renders_state_and_latest_checkpoint(stack):
    _, _, eng = stack
    await eng.set_state(
        "s", {"completed_through": 5, "next_value": 6}, actor="llm:a"
    )
    await eng.create_checkpoint("s", note="through 5", actor="llm:a")
    msg = await eng.context_message("s")
    assert msg["role"] == "system"
    body = msg["content"]
    assert "Session continuity" in body
    assert "'default'" in body
    assert '"next_value": 6' in body
    assert "Latest checkpoint #" in body and "through 5" in body
    assert "[End session continuity]" in body


async def test_context_truncates_huge_payload_render(stack):
    _, _, eng = stack
    await eng.set_state("s", {"blob": "y" * 3000}, actor="x")
    msg = await eng.context_message("s")
    assert len(msg["content"]) < 2600
    assert "…[truncated]" in msg["content"]


async def test_context_global_budget_omits_overflow_tasks(stack):
    """m5: fat tasks exceed the whole-brief cap - rendering stops with an
    explicit omission marker and the total stays under budget. Recency
    selection keeps the five most-recently-updated keys (k0 ages out)."""
    _, _, eng = stack
    import asyncio as _aio
    import re as _re

    for i in range(6):
        await eng.set_state(
            "s", {"blob": "x" * 900, "i": i}, actor="x", task_key=f"k{i}"
        )
        await _aio.sleep(0.001)  # distinct updated_at for stable recency
    msg = await eng.context_message("s")
    body = msg["content"]
    assert len(body) <= 4200  # 4096 cap + header/footer slop
    assert "additional tasks omitted" in body
    rendered = _re.findall(r"Task '(k\d)'", body)
    assert rendered, "at least one task must render before the cut"
    assert "k0" not in rendered  # oldest key dropped by recency limit


async def test_interruption_signal_from_runs_after_last_checkpoint(stack):
    _, runs, eng = stack
    await eng.set_state("s", {"next": 6}, actor="mcp:task_state_set")
    await eng.create_checkpoint("s", note="before resume", actor="mcp")
    # Failure strictly after the checkpoint timestamp.
    await runs.record(run_entry("r1", outcome="failover", provider="alpha",
                                offset=5.0))
    msg = await eng.context_message("s")
    assert "ended unexpectedly on provider 'alpha' (500)" in msg["content"]

    # A later OK run clears the signal.
    await runs.record(run_entry("r2", outcome="ok", provider="beta",
                                offset=10.0))
    msg2 = await eng.context_message("s")
    assert "ended unexpectedly" not in msg2["content"]


async def test_no_interruption_when_failure_predates_checkpoint(stack):
    _, runs, eng = stack
    await runs.record(run_entry("r1", outcome="error", provider="groq",
                                finished_at=1_000_000_000.0))  # ancient
    await eng.set_state("s", {"next": 7}, actor="mcp:x")
    await eng.create_checkpoint("s", note="after failure", actor="user")
    msg = await eng.context_message("s")
    assert "ended unexpectedly" not in msg["content"]


async def test_toggle_off_disables_rendering(monkeypatch, stack):
    monkeypatch.setenv("INVINCIBLE_CONTINUITY", "0")
    _, _, eng = stack
    await eng.set_state("s", {"next": 6}, actor="x")
    assert await eng.context_message("s") is None
    monkeypatch.setenv("INVINCIBLE_CONTINUITY", "on")
    assert await eng.context_message("s") is not None


async def test_concurrent_sets_serialize_versions(stack):
    import asyncio as _aio

    _, _, eng = stack
    await _aio.gather(
        *[eng.set_state("s", {"w": i}, actor=f"a{i}") for i in range(5)]
    )
    versions = sorted(h["version"] for h in await eng.history("s", limit=10))
    assert versions == [1, 2, 3, 4, 5]
