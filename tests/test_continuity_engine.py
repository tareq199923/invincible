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
    cp0 = await eng.create_checkpoint("s", note="started")
    assert cp0["state_version"] == 0  # nothing tracked yet
    await eng.set_state("s", {"through": 5}, actor="llm:a")
    cp1 = await eng.create_checkpoint("s", note="through 5")
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
    await eng.create_checkpoint("s", note="through 5")
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
    await eng.create_checkpoint("s", note="before resume")
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
    await eng.create_checkpoint("s", note="after failure")
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


# --- checkpoints record no actor, on purpose ---------------------------------
# (deep code review 2026-09-24, finding 6)


async def test_create_checkpoint_no_longer_takes_an_actor(stack):
    """It used to accept ``actor`` and silently drop it, so callers
    believed they were recording provenance that went nowhere. The table
    has no column for it, and nothing reads one - so the parameter is
    gone rather than left lying in the signature.
    """
    _, _, eng = stack
    with pytest.raises(TypeError):
        await eng.create_checkpoint("s", note="x", actor="user")


async def test_failover_hook_note_marks_the_checkpoint_automatic(stack):
    """The provenance the removed parameter would have carried is already
    present: the hook writes it into the note. This is the claim
    ``create_checkpoint``'s docstring makes for deleting ``actor``.
    """
    from invincible.core.scope import UNSCOPED

    _, _, eng = stack
    await eng.set_state("s", {"next": 6}, actor="t")
    await eng.failover_hook()(
        request_id="req-1", session_id="s", session_pk=UNSCOPED,
        failed_provider="alpha", error_class="429")

    cps = await eng.checkpoints("s")
    assert cps, "the failover hook pinned no checkpoint"
    assert cps[0]["note"].startswith("auto: pre-failover")
    assert "alpha" in cps[0]["note"] and "429" in cps[0]["note"]


# --- the brief's round-trip cost (finding 7) ---------------------------------


async def _seed_keys(eng, session_id, count):
    """``count`` tracked tasks with a checkpoint each, recency-ordered."""
    import asyncio as _aio

    for i in range(count):
        await eng.set_state(session_id, {"i": i}, actor="x",
                            task_key=f"k{i}")
        await eng.create_checkpoint(session_id, task_key=f"k{i}",
                                    note=f"note-{i}")
        await _aio.sleep(0.001)  # distinct updated_at for stable recency


async def test_context_snapshot_matches_the_per_key_reads(stack):
    """The batched snapshot is the per-key reads, reassembled.

    ``context_message`` composes its brief from this dict instead of
    awaiting ``get_state`` / ``checkpoints`` once per task key, so the two
    must agree row-for-row (deep code review 2026-09-24, finding 7).
    """
    _, _, eng = stack
    await _seed_keys(eng, "s", 6)   # k0 ages out of the five-key window

    snap = await eng.context_snapshot("s")

    assert snap["task_keys"] == await eng.active_task_keys("s", limit=5)
    assert sorted(snap["states"]) == sorted(snap["task_keys"])
    for key in snap["task_keys"]:
        assert snap["states"][key] == await eng.get_state("s", key)
        assert snap["checkpoints"][key] == (
            await eng.checkpoints("s", key, limit=1))[0]
    assert snap["latest_checkpoint"] == (await eng.checkpoints("s", limit=1))[0]

    # The checkpoint query spans the WHOLE session on purpose: a newer
    # checkpoint on a key outside the rendered five must still be the one
    # ``checkpoints(limit=1)`` returns, because that is what the
    # interruption note compares the recent runs against.
    await eng.create_checkpoint("s", task_key="k0", note="outside window")
    snap = await eng.context_snapshot("s")
    assert "k0" not in snap["task_keys"]
    assert "k0" in snap["checkpoints"]
    assert snap["latest_checkpoint"]["note"] == "outside window"
    assert snap["latest_checkpoint"] == (await eng.checkpoints("s", limit=1))[0]


async def test_context_snapshot_unresolved_scope_reads_nothing(stack,
                                                               statements):
    """``session_pk=None`` is "scoped, owner unresolved" - not "no scope".

    The snapshot is the brief's only read path now, so it must fail closed
    exactly as ``get_state``/``checkpoints`` do: an empty snapshot, and
    crucially no SQL at all, so a foreign client string can never be
    matched (``core/scope.py``, finding 1).
    """
    _, _, eng = stack
    await _seed_keys(eng, "s", 1)

    del statements[:]
    snap = await eng.context_snapshot("s", session_pk=None)

    assert statements == []
    assert snap["task_keys"] == [] and snap["states"] == {}
    assert snap["checkpoints"] == {} and snap["latest_checkpoint"] is None
    assert snap["runs"] is None


async def test_context_snapshot_is_empty_when_nothing_is_tracked(stack):
    _, _, eng = stack
    snap = await eng.context_snapshot("s")
    assert snap["task_keys"] == []
    assert snap["states"] == {} and snap["checkpoints"] == {}
    assert snap["latest_checkpoint"] is None and snap["runs"] is None


async def test_context_brief_costs_a_fixed_number_of_queries(pg_engine,
                                                             statements):
    """Finding 7: the brief was 13 round-trips at five task keys.

    It awaited ``active_task_keys`` (1), then ``interruption_note`` - which
    itself did ``checkpoints(limit=1)`` (1) and ``runs.recent`` (1) - then
    ``get_state`` and ``checkpoints`` PER KEY (5 + 5). That is 13 queries on
    the critical path of every chat request, and it grew with the key count.

    Now it is a fixed four: keys, heads (DISTINCT ON task_key), newest
    checkpoint per task key, recent runs. This test is the only proof that
    the round-trips dropped - the rows are identical either way - so it
    counts them rather than asserting the shape in a comment.
    """
    from invincible.core.continuity import ContinuityEngine
    from invincible.core.run_store import RunStore

    runs = RunStore(engine=pg_engine)
    eng = ContinuityEngine(engine=pg_engine, runs=runs)
    await _seed_keys(eng, "one-key", 1)
    await _seed_keys(eng, "five-keys", 5)
    await runs.record({
        "request_id": "r1", "session_id": "five-keys",
        "provider_name": "alpha", "model_id": "m", "attempt_index": 1,
        "outcome": "ok", "started_at": 1.0, "finished_at": 2.0,
    })

    def selects() -> list[str]:
        return [sql for sql, _ in statements
                if sql.lstrip().upper().startswith("SELECT")]

    del statements[:]
    assert await eng.context_message("one-key") is not None
    one = len(selects())

    del statements[:]
    assert await eng.context_message("five-keys") is not None
    five = len(selects())

    assert five == 4, f"5-key brief issued {five} queries, expected the 4 above"
    assert one == five, (
        f"query count must not scale with task keys (1 key: {one}, "
        f"5 keys: {five})"
    )
    await eng.close()
    await runs.close()

