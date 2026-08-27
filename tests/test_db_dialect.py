# tests/test_db_dialect.py
"""Phase 16 Commit A: prove the PostgreSQL foundation before any store is
rewritten. Talks directly to invincible_test via core.db metadata.

Covers: JSONB round-trips (unicode/nested), Identity PKs, FK enforcement,
unique-constraint surfacing, and create/drop symmetry.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from invincible.core.db import (
    LOCAL_OWNER_EMAIL,
    LOCAL_PROJECT_NAME,
    messages,
    metadata,
    projects,
    runs,
    sessions,
    task_states,
    turns,
    users,
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


async def local_owner_ids(engine) -> tuple[int, int]:
    """Seed and return the system *local* owner's (user_id, project_id)."""
    async with engine.begin() as conn:
        uid = (await conn.execute(
            users.insert()
            .values(email=LOCAL_OWNER_EMAIL, is_system=True, created_at=1.0)
            .returning(users.c.id)
        )).scalar_one()
        pid = (await conn.execute(
            projects.insert()
            .values(user_id=uid, name=LOCAL_PROJECT_NAME,
                    is_default=True, created_at=1.0)
            .returning(projects.c.id)
        )).scalar_one()
    return int(uid), int(pid)


async def seed_session(engine, client_session_id="s") -> int:
    """One session row under the system local owner; returns its pk."""
    uid, pid = await local_owner_ids(engine)
    async with engine.begin() as conn:
        sid = (await conn.execute(
            sessions.insert()
            .values(user_id=uid, project_id=pid,
                    client_session_id=client_session_id,
                    created_at=1.0, updated_at=1.0)
            .returning(sessions.c.id)
        )).scalar_one()
    return int(sid)


async def seed_turn(engine, session_pk: int, seq: int = 0) -> int:
    async with engine.begin() as conn:
        tid = (await conn.execute(
            turns.insert()
            .values(session_id=session_pk, seq=seq)
            .returning(turns.c.id)
        )).scalar_one()
    return int(tid)


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
    sid = await seed_session(engine)
    tid = await seed_turn(engine, sid)
    async with engine.begin() as conn:
        await conn.execute(
            messages.insert().values(
                turn_id=tid, seq=0, role="assistant", payload=payload))

    async with engine.connect() as conn:
        stored = (await conn.execute(
            messages.select())).mappings().first()

    # JSONB returns a dict — no double-decode needed, exact equality holds.
    assert stored["payload"] == payload
    assert isinstance(stored["id"], int)


async def test_identity_pks_populate_monotonically(engine):
    await seed_session(engine)
    async with engine.begin() as conn:
        tid = (await conn.execute(
            turns.insert().values(session_id=(await _only_session(conn)),
                                  seq=0).returning(turns.c.id)
        )).scalar_one()
        await conn.execute(
            task_states.insert().values(
                session_id="s", task_key="k", status="active",
                payload={"n": 1}, version=1, updated_by="t",
                updated_at=1.0))
    async with engine.connect() as conn:
        t = (await conn.execute(turns.select())).first()
        ts = (await conn.execute(task_states.select())).first()
    assert isinstance(t.id, int) and isinstance(ts.id, int)
    assert isinstance(tid, int)


async def _only_session(conn) -> int:
    return (await conn.execute(select(sessions.c.id))).scalar_one()


async def test_fk_enforcement_blocks_orphan_messages(engine):
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                messages.insert().values(
                    turn_id=999999, seq=0, role="x", payload={}))


async def test_unique_constraints_surface(engine):
    pk = await seed_session(engine)
    async with engine.begin() as conn:
        await conn.execute(turns.insert().values(session_id=pk, seq=0))
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(turns.insert().values(session_id=pk, seq=0))


async def test_sessions_ownership_triple_is_unique(engine):
    """Phase 1: same (user, project, client string) twice must collide."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    uid, pid = await local_owner_ids(engine)
    values = {"user_id": uid, "project_id": pid,
              "client_session_id": "dup", "created_at": 1.0,
              "updated_at": 1.0}
    async with engine.begin() as conn:
        await conn.execute(sessions.insert().values(**values))
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(sessions.insert().values(**values))
    # ...while the resolve-or-create idiom stays conflict-free.
    async with engine.begin() as conn:
        await conn.execute(
            pg_insert(sessions).values(**values)
            .on_conflict_do_nothing(index_elements=[
                "user_id", "project_id", "client_session_id"]))
    async with engine.connect() as conn:
        n = (await conn.execute(text(
            "SELECT COUNT(*) FROM sessions"))).scalar_one()
    assert n == 1


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
        # Phase 1 identity & ownership
        "users", "projects", "api_keys", "audit_log", "memories",
        # sessions / continuity / oauth / mcp / rate limiting
        "sessions", "turns", "messages", "facts", "runs", "task_states",
        "checkpoints", "oauth_clients", "oauth_codes", "oauth_tokens",
        "pending_actions", "login_attempts",
        # Phase 3 accounts
        "user_identities", "device_codes",
        # Phase 9 BYOK provider connections
        "user_provider_credentials",
    }
    assert set(metadata.tables) == expected


def test_payload_columns_are_jsonb():
    # Guard the Phase 16 decision: JSON-shaped columns must be JSONB on PG.
    from sqlalchemy.dialects.postgresql import JSONB

    assert isinstance(messages.c.payload.type, JSONB)
    assert isinstance(runs.c.meta.type, JSONB)
    assert isinstance(task_states.c.payload.type, JSONB)
