# tests/test_graph_golden.py
"""Golden-file pin of the /graph projection wire shape (Phase 5 PR-5B).

Captured from endpoints/graph.py BEFORE its body was extracted into
core/projection.py, making the refactor provably response-equivalent;
afterwards it guards the projection contract so any change shows up as
a reviewable golden diff instead of slipping through silently.

Regenerate ONLY with UPDATE_GOLDEN=1 pytest tests/test_graph_golden.py
and include the regenerated JSON in the commit that changes behavior.
Never hand-edit the JSON file.

Canonicalization: every float becomes "<float>" - generated_at and all
epoch stamps vary run to run; every other value, list order, and key
must match exactly. Surrogate ids stay stable because the pg_engine
fixture TRUNCATEs RESTART IDENTITY.
"""
import json
import os

import pytest

from invincible.core.continuity import ContinuityEngine
from invincible.core.run_store import RunStore
from invincible.main import app

ADMIN = {"Authorization": "Bearer admin-secret"}

GOLDEN_PATH = os.path.join(
    os.path.dirname(__file__), "golden", "session_graph_admin.json")

_TOP_LEVEL_KEYS = {
    "session_id", "known", "generated_at",
    "nodes", "edges", "timeline", "summary",
}


def canonicalize(value):
    if isinstance(value, float):
        return "<float>"
    if isinstance(value, list):
        return [canonicalize(v) for v in value]
    if isinstance(value, dict):
        return {k: canonicalize(v) for k, v in value.items()}
    return value


class _FakeClock:
    """Deterministic replacement for continuity's time module: state
    versions and checkpoints stamp strictly increasing epochs BEFORE the
    seeded run window (runs live at 1000+), so the checkpoint genuinely
    precedes the post-checkpoint failure and the timeline order is
    independent of wall-clock ticks."""

    def __init__(self):
        self._now = 900.0

    def time(self):
        self._now += 1.0
        return self._now


@pytest.fixture
async def golden_stack(client, pg_engine, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_ADMIN_KEY", "admin-secret")
    monkeypatch.setattr("invincible.core.continuity.time", _FakeClock())
    runs = RunStore(engine=pg_engine)
    engine = ContinuityEngine(engine=pg_engine, runs=runs)
    app.state.runs = runs
    app.state.continuity = engine
    try:
        yield runs, engine
    finally:
        await engine.close()
        await runs.close()


async def seed_rich_session(runs, engine):
    """One deterministic scenario exercising every node/edge kind:
    a failover chain, a clean second request, versioned task state, a
    checkpoint pinning the head, a post-checkpoint failure (interruption
    note), and one normalized turn."""
    base = 1000.0
    # The session row must exist first so every store write lands on the
    # owning surrogate pk - exactly what production callers resolve.
    from invincible.core.db import ensure_local_owner

    uid, pid = await ensure_local_owner(app.state.engine)
    await app.state.sessions.append("default", [
        {"role": "user", "content": "count please"},
        {"role": "assistant", "content": "1 2 3"},
    ])
    session_pk = await app.state.sessions.lookup(
        "default", user_id=uid, project_id=pid)

    async def attempt(rid, outcome, provider, index, offset, err=None):
        await runs.record({
            "request_id": rid,
            "session_id": "default",
            "session_pk": session_pk,
            "provider_name": provider,
            "model_id": f"{provider}-model",
            "attempt_index": index,
            "outcome": outcome,
            "error_class": err,
            "input_tokens": 11 * index if outcome == "ok" else None,
            "output_tokens": 7 if outcome == "ok" else None,
            "started_at": base + offset,
            "finished_at": base + offset + 1.0,
            "meta": None,
        })

    # One request_id, three attempts: alpha -> beta -> gamma.
    await attempt("req-1", "failover", "alpha", 1, 0.0, err="429")
    await attempt("req-1", "failover", "beta", 2, 2.0, err="500")
    await attempt("req-1", "ok", "gamma", 3, 4.0)
    # A separate, successful request on the winning provider.
    await attempt("req-2", "ok", "gamma", 1, 6.0)
    # Versioned task state + a checkpoint pinning the head...
    await engine.set_state("default", {"through": 5}, actor="mcp:tss",
                           session_pk=session_pk)
    await engine.set_state("default", {"through": 9}, actor="mcp:tss",
                           session_pk=session_pk)
    await engine.create_checkpoint(
        "default", note="through 9", actor="mcp:checkpoint_create",
        session_pk=session_pk)
    # ...then a post-checkpoint upstream failure (interruption note).
    await attempt("req-3", "error", "delta", 1, 8.0, err="timeout")


async def test_projection_matches_golden(client, golden_stack):
    runs, engine = golden_stack
    await seed_rich_session(runs, engine)

    resp = await client.get("/api/v1/sessions/default/graph", headers=ADMIN)
    assert resp.status_code == 200
    data = canonicalize(resp.json())
    assert set(data) == _TOP_LEVEL_KEYS
    assert data["known"] is True
    assert data["session_id"] == "default"

    update = os.getenv("UPDATE_GOLDEN") == "1"
    if update:
        os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
        with open(GOLDEN_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        return
    with open(GOLDEN_PATH, encoding="utf-8") as handle:
        golden = json.load(handle)
    assert data == golden
