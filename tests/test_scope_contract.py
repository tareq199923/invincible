# tests/test_scope_contract.py
"""The session-scope contract shared by the run and continuity stores.

Regression cover for deep code review 2026-09-24, finding 1. The stores
used to read ``session_pk=None`` as "no scope requested" and fall back to
matching the caller-supplied client string with no owner predicate, so a
request path that resolved ownership and FAILED still got the owner's
rows back.

``session_pk`` now distinguishes three cases (``core/scope.py``):

- OMITTED - the legacy single-tenant path (tests, local tooling).
- an ``int`` - scoped to that owning session.
- ``None`` - the caller asked for ownership scoping and resolved no
  owner. Reads return nothing; writes refuse.

The endpoint tests in ``test_isolation.py`` cover the request path, which
now stops at ``graph.py`` before it ever reaches these stores. These
tests pin the layer underneath, so a future caller cannot reintroduce the
fallback by passing ``None``.
"""
import pytest

from invincible.core.continuity import ContinuityEngine
from invincible.core.run_store import RunStore
from invincible.core.scope import UnresolvedScopeError


@pytest.fixture
async def stack(pg_engine):
    runs = RunStore(engine=pg_engine)
    engine = ContinuityEngine(engine=pg_engine, runs=runs)
    try:
        yield runs, engine
    finally:
        await engine.close()
        await runs.close()


async def _seed_legacy_rows(runs, engine):
    """One run, state and checkpoint written the pre-isolation way."""
    await runs.record({
        "request_id": "scope-req",
        "session_id": "scope-s",
        "provider_name": "alpha",
        "model_id": "m-a",
        "attempt_index": 1,
        "outcome": "ok",
        "started_at": 100.0,
        "finished_at": 101.0,
    })
    await engine.set_state("scope-s", {"secret": "S"}, actor="t")
    await engine.create_checkpoint("scope-s", note="N")


async def test_omitting_the_scope_keeps_the_legacy_path(stack):
    """Existing single-tenant callers are unaffected: the rows written
    and read with no ``session_pk`` at all still round-trip."""
    runs, engine = stack
    await _seed_legacy_rows(runs, engine)

    assert len(await runs.recent(session_id="scope-s")) == 1
    assert await engine.active_task_keys("scope-s") == ["default"]
    assert (await engine.get_state("scope-s"))["payload"] == {"secret": "S"}
    assert len(await engine.checkpoints("scope-s")) == 1


async def test_unresolved_scope_reads_nothing(stack):
    """``session_pk=None`` - the caller proved no ownership - must not
    fall back to the string match that leaked."""
    runs, engine = stack
    await _seed_legacy_rows(runs, engine)

    assert await runs.recent(session_id="scope-s", session_pk=None) == []
    assert await engine.active_task_keys("scope-s", session_pk=None) == []
    assert await engine.get_state("scope-s", session_pk=None) is None
    assert await engine.history("scope-s", session_pk=None) == []
    assert await engine.checkpoints("scope-s", session_pk=None) == []
    assert await engine.interruption_note("scope-s", session_pk=None) is None
    assert await engine.context_message("scope-s", session_pk=None) is None


async def test_unresolved_scope_writes_refuse(stack):
    """A write with no resolved owner would land unscoped, so it raises
    instead of silently writing (the session_store.py invariant)."""
    runs, engine = stack
    with pytest.raises(UnresolvedScopeError):
        await engine.set_state("scope-s", {"a": 1}, actor="t", session_pk=None)
    with pytest.raises(UnresolvedScopeError):
        await engine.create_checkpoint("scope-s", session_pk=None)


async def test_none_scope_does_not_reach_sql(stack):
    """The sentinel is not a SQL value, and the legacy path must still
    store a NULL surrogate rather than trying to bind it."""
    runs, engine = stack
    await engine.set_state("scope-s", {"a": 1}, actor="t")
    await engine.create_checkpoint("scope-s", note="legacy")

    assert len(await engine.checkpoints("scope-s")) == 1
    assert await runs.recent(session_id="scope-s") == []


async def test_a_real_scope_still_isolates(stack, pg_engine):
    """Two principals sharing a client string never see each other, and a
    scoped read ignores the legacy NULL-surrogate rows entirely."""
    import time

    from sqlalchemy import insert

    from invincible.core.db import ensure_local_owner, projects, users
    from invincible.core.session_store import SessionStore

    runs, engine = stack
    uid_a, pid_a = await ensure_local_owner(pg_engine)
    async with pg_engine.begin() as conn:
        uid_b = (await conn.execute(
            insert(users).values(
                email="scope-b@example.com", created_at=time.time())
        )).inserted_primary_key[0]
        pid_b = (await conn.execute(
            insert(projects).values(
                user_id=uid_b, name="other", is_default=True,
                created_at=time.time())
        )).inserted_primary_key[0]

    sessions = SessionStore(pg_engine)
    pk_a = await sessions.resolve_or_create(
        "shared-name", user_id=uid_a, project_id=pid_a)
    pk_b = await sessions.resolve_or_create(
        "shared-name", user_id=uid_b, project_id=pid_b)

    await engine.set_state("shared-name", {"who": "a"}, actor="a",
                           session_pk=pk_a)
    await engine.create_checkpoint("shared-name", note="A's",
                                   session_pk=pk_a)

    assert (await engine.get_state("shared-name", session_pk=pk_a))[
        "payload"] == {"who": "a"}
    assert await engine.get_state("shared-name", session_pk=pk_b) is None
    assert await engine.checkpoints("shared-name", session_pk=pk_b) == []
    assert await runs.recent(session_id="shared-name", session_pk=pk_b) == []
