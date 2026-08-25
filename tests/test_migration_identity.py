# tests/test_migration_identity.py
"""Platform Phase 1 migration acceptance (live tier, scratch databases).

The roadmap gate for Phase 1 is COUNT-PRESERVATION: revision ``0002`` must
carry every pre-existing session/turn/message row onto the surrogate
ownership shape without losing or duplicating anything. These tests drive
the real packaged Alembic environment against throwaway databases:

1. populated-0001 upgrade path (real upgrades) - full rebuild branch;
2. fresh ``create_all`` database (new shape already present) - seed-only
   branch;
3. downgrade round trip back to the legacy string-keyed shape.

Patterns follow test_cli_db.py (scratch databases via admin_pg);
everything here auto-skips without a reachable local Postgres via pg_live.
"""
import json

import pytest
from sqlalchemy import text

from invincible.core.db import (
    LOCAL_OWNER_EMAIL,
    LOCAL_PROJECT_NAME,
    make_engine,
    migration_heads,
)
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_p1_mig"


def _upgrade_to(url: str, target: str) -> None:
    """Run the packaged Alembic environment exactly like the CLI does."""
    from alembic import command as alembic_command

    from invincible.core.db import migrations_config

    cfg = migrations_config(db_url=url)
    alembic_command.upgrade(cfg, target)


async def _scalar(engine, sql: str) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


async def _rows(engine, sql: str) -> list[tuple]:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).all()


LEGACY_PAYLOADS = {
    # Payload documents asserted verbatim after migration (EXPECTED_ALPHA_FLAT
    # below must stay in sync with keys "p1"/"p2"/"p3").
    "p1": {"role": "user", "content": "remember that ünïcode works"},
    "p2": {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file",
                          "arguments": '{"path": "x.txt"}'}}
        ],
    },
    "p3": {"role": "user", "content": "go on"},
    "p4": {"role": "assistant", "content": "done"},
}

EXPECTED_ALPHA_FLAT = [LEGACY_PAYLOADS["p1"], LEGACY_PAYLOADS["p2"],
                       LEGACY_PAYLOADS["p3"]]


def _seed() -> list[tuple[str, dict]]:
    """Legacy-shaped rows as (sql, params); payloads via json.dumps so no
    hand-escaped literals can drift from what the assertions expect."""
    def msg(mid: int, turn_id: int, seq: int, role: str, key: str):
        return (
            "INSERT INTO messages (id, turn_id, seq, role, payload)"
            f" VALUES ({mid}, {turn_id}, {seq}, '{role}', :{key})",
            {key: json.dumps(LEGACY_PAYLOADS[key])},
        )

    return [
        ("INSERT INTO sessions VALUES"
         " ('alpha', 1700000001.0, 1700000099.0)", {}),
        ("INSERT INTO sessions VALUES"
         " ('beta', 1700000100.0, 1700000200.0)", {}),
        ("INSERT INTO sessions VALUES"
         " ('gamma-ünïcode ✅', 1700000300.0, 1700000300.0)", {}),
        # Explicit legacy ids so preservation is provable.
        ("INSERT INTO turns (id, session_id, seq) VALUES"
         " (501, 'alpha', 0)", {}),
        ("INSERT INTO turns (id, session_id, seq) VALUES"
         " (502, 'alpha', 1)", {}),
        ("INSERT INTO turns (id, session_id, seq) VALUES"
         " (601, 'beta', 0)", {}),
        msg(701, 501, 0, "user", "p1"),
        msg(702, 501, 1, "assistant", "p2"),
        msg(703, 502, 0, "user", "p3"),
        msg(704, 601, 0, "assistant", "p4"),
    ]


@pytest.fixture
async def scratch_0001(admin_pg):
    """Scratch database stamped at 0001 and seeded with legacy-shaped rows."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0001")
    engine = make_engine(url)
    async with engine.begin() as conn:
        for statement, params in _seed():
            await conn.execute(text(statement), params)
    yield engine
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


EXPECTED_ALPHA_FLAT = [
    {"role": "user", "content": "remember that ünïcode works"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file",
                          "arguments": '{"path": "x.txt"}'}}
        ],
    },
    {"role": "user", "content": "go on"},
]


async def _assert_migrated_content(engine) -> None:
    """Full-fidelity mapping check across the ownership triple."""
    rows = await _rows(engine, (
        "SELECT s.client_session_id, t.seq AS turn_seq,"
        " m.seq AS msg_seq, m.role, m.payload"
        " FROM messages m JOIN turns t ON m.turn_id = t.id"
        " JOIN sessions s ON t.session_id = s.id"
        " ORDER BY s.client_session_id, t.seq, m.seq"
    ))
    alpha = [r for r in rows if r[0] == "alpha"]
    beta = [r for r in rows if r[0] == "beta"]
    assert [(r[2], r[3], r[4]) for r in alpha] == [
        (0, "user", EXPECTED_ALPHA_FLAT[0]),
        (1, "assistant", EXPECTED_ALPHA_FLAT[1]),
        (0, "user", EXPECTED_ALPHA_FLAT[2]),
    ]
    assert [(r[2], r[3]) for r in beta] == [(0, "assistant")]

    # The empty legacy session kept its row but gained no turns.
    gamma_turns = await _scalar(engine, (
        "SELECT COUNT(*) FROM turns t JOIN sessions s ON t.session_id = s.id"
        " WHERE s.client_session_id LIKE 'gamma-%'"
    ))
    assert gamma_turns == 0


async def test_upgrade_from_populated_0001_preserves_counts(
    scratch_0001, pg_live
):
    """THE Phase 1 gate: populated legacy rows survive the rebuild intact."""
    from invincible.core.session_store import SessionStore

    engine = scratch_0001
    before = {
        table: await _scalar(engine, f"SELECT COUNT(*) FROM {table}")
        for table in ("sessions", "turns", "messages")
    }
    assert before == {"sessions": 3, "turns": 3, "messages": 4}

    _upgrade_to(make_scratch_url(SCRATCH_NAME), "head")

    # Row counts preserved everywhere the revision moved.
    for table, n in before.items():
        after = await _scalar(engine, f"SELECT COUNT(*) FROM {table}")
        assert after == n, f"{table}: {n} -> {after}"

    # Migration control landed on the packaged head.
    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == migration_heads()[0]

    # System *local* owner seeded exactly once.
    owners = await _rows(engine, (
        "SELECT u.email, p.name FROM users u"
        " JOIN projects p ON p.user_id = u.id"
    ))
    assert owners == [(LOCAL_OWNER_EMAIL, LOCAL_PROJECT_NAME)]

    await _assert_migrated_content(engine)

    # Integration proof: the CURRENT store reads migrated history through
    # the local-owner fallback with zero code changes.
    loaded = await SessionStore(engine=engine).load("alpha")
    assert loaded == EXPECTED_ALPHA_FLAT


async def test_upgrade_on_fresh_create_all_database_is_seed_only(
    admin_pg, pg_live
):
    """Fresh-database flow: create_all built the NEW shape before any
    upgrade ran; revision 0002 must take its seed-only branch."""
    name = "invincible_p1_fresh"
    url = make_scratch_url(name)
    await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {name}")
    try:
        from invincible.core.db import create_all_from_metadata

        engine = make_engine(url)
        await create_all_from_metadata(engine)

        _upgrade_to(url, "head")

        assert await _scalar(
            engine, "SELECT version_num FROM alembic_version"
        ) == migration_heads()[0]
        assert await _scalar(engine, "SELECT COUNT(*) FROM users") == 1
        assert await _scalar(engine, "SELECT COUNT(*) FROM projects") == 1
        assert await _scalar(engine, "SELECT COUNT(*) FROM sessions") == 0
        await engine.dispose()
    finally:
        await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


async def test_downgrade_round_trip_preserves_counts(scratch_0001, pg_live):
    """head -> 0001 lands back on the legacy string-keyed shape without
    losing rows; re-upgrading restores the identity world."""
    engine = scratch_0001
    url = make_scratch_url(SCRATCH_NAME)
    original = {
        table: await _scalar(engine, f"SELECT COUNT(*) FROM {table}")
        for table in ("sessions", "turns", "messages")
    }

    _upgrade_to(url, "0001")

    # Legacy shape is back (client-supplied string PK).
    columns = await _rows(
        engine,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema='public' AND table_name='sessions'"
        " ORDER BY column_name",
    )
    assert [c[0] for c in columns] == [
        "created_at", "session_id", "updated_at",
    ]
    for table, n in original.items():
        after = await _scalar(engine, f"SELECT COUNT(*) FROM {table}")
        assert after == n, f"{table}: expected {n}, saw {after}"

    # Data survived the round trip verbatim.
    doc = json.loads(await _scalar(engine, (
        "SELECT m.payload::text FROM messages m JOIN turns t"
        " ON m.turn_id = t.id JOIN sessions s ON t.session_id = s.session_id"
        " WHERE s.session_id = 'alpha' AND m.seq = 0 AND t.seq = 0"
    )))
    assert doc == LEGACY_PAYLOADS["p1"]

    # And forward again.
    _upgrade_to(url, "head")
    for table, n in original.items():
        after = await _scalar(engine, f"SELECT COUNT(*) FROM {table}")
        assert after == n, f"{table}: expected {n}, saw {after}"
    await _assert_migrated_content(engine)
