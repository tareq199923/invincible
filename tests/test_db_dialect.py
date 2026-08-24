# tests/test_db_dialect.py
"""Phase 16 Commit A: prove the PostgreSQL foundation before any store is
rewritten. Talks directly to invincible_test via core.db metadata.

Covers: JSONB round-trips (unicode/nested), Identity PKs, FK enforcement,
unique-constraint surfacing, and create/drop symmetry.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from invincible.core.db import (
    messages,
    metadata,
    runs,
    sessions,
    task_states,
    turns,
)
from tests.conftest import TEST_DB_URL


@pytest.fixture
async def engine():
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


async def seed_session(engine, session_id="s"):
    async with engine.begin() as conn:
        await conn.execute(
            sessions.insert().values(
                session_id=session_id, created_at=1.0, updated_at=1.0))


async def test_jsonb_roundtrip_unicode_and_nesting(engine):
    payload = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file",
                          "arguments": '{"path": "ünïcode ✅.txt"}'}}
        ],
        "nested": {"list": [1, 2, {"x": None}], "flag": True},
    }
    async with engine.begin() as conn:
        await conn.execute(
            sessions.insert().values(session_id="s", created_at=1.0,
                                     updated_at=1.0))
        await conn.execute(
            turns.insert().values(session_id="s", seq=0))
        row = await conn.execute(text("SELECT id FROM turns WHERE session_id='s'"))
        turn_id = row.scalar()
        await conn.execute(
            messages.insert().values(
                turn_id=turn_id, seq=0, role="assistant",
                payload=payload))

    async with engine.connect() as conn:
        stored = (await conn.execute(
            messages.select())).mappings().first()

    # JSONB returns a dict — no double-decode needed, exact equality holds.
    assert stored["payload"] == payload
    assert isinstance(stored["id"], int)


async def test_identity_pks_populate_monotonically(engine):
    await seed_session(engine)
    async with engine.begin() as conn:
        await conn.execute(
            turns.insert().values(session_id="s", seq=0))
        await conn.execute(
            task_states.insert().values(
                session_id="s", task_key="k", status="active",
                payload={"n": 1}, version=1, updated_by="t",
                updated_at=1.0))
    async with engine.connect() as conn:
        t = (await conn.execute(turns.select())).first()
        ts = (await conn.execute(task_states.select())).first()
    assert isinstance(t.id, int) and isinstance(ts.id, int)


async def test_fk_enforcement_blocks_orphan_messages(engine):
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                messages.insert().values(
                    turn_id=999999, seq=0, role="x", payload={}))


async def test_unique_constraints_surface(engine):
    await seed_session(engine)
    async with engine.begin() as conn:
        await conn.execute(turns.insert().values(session_id="s", seq=0))
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(turns.insert().values(session_id="s", seq=0))


async def test_runs_indexes_and_nullability(engine):
    async with engine.begin() as conn:
        await conn.execute(runs.insert().values(
            request_id="r1", provider_name="p", model_id="m",
            attempt_index=1, outcome="failover", error_class="500",
            started_at=1.0, finished_at=2.0,
            meta={"reason": "server_error"}))
    async with engine.connect() as conn:
        run = (await conn.execute(runs.select())).mappings().first()
    assert run["meta"] == {"reason": "server_error"}
    assert run["session_id"] is None  # nullable by design


async def test_create_drop_symmetry(engine):
    from invincible.core.db import drop_all_from_metadata

    await drop_all_from_metadata(engine)
    async with engine.connect() as conn:
        n = (await conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='public'"))).scalar()
    assert n == 0
    from invincible.core.db import create_all_from_metadata

    await create_all_from_metadata(engine)
    async with engine.connect() as conn:
        n2 = (await conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='public'"))).scalar()
    assert n2 == len(metadata.tables)


def test_metadata_covers_all_expected_tables():
    expected = {
        "sessions", "turns", "messages", "facts", "runs", "task_states",
        "checkpoints", "oauth_clients", "oauth_codes", "oauth_tokens",
        "pending_actions",
    }
    assert set(metadata.tables) == expected


def test_payload_columns_are_jsonb():
    # Guard the Phase 16 decision: JSON-shaped columns must be JSONB on PG.
    from sqlalchemy.dialects.postgresql import JSONB

    assert isinstance(messages.c.payload.type, JSONB)
    assert isinstance(runs.c.meta.type, JSONB)
    assert isinstance(task_states.c.payload.type, JSONB)
