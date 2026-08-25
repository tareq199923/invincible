# tests/test_migration_isolation.py
"""Platform Phase 2 migration acceptance (live tier, scratch databases).

Revision ``0003`` adds ownership (``session_pk``) to runs/task_states/
checkpoints and user subjects to OAuth rows, then backfills everything to
the system *local* owner. Gates verified here:

- row counts preserved everywhere (nothing moves or vanishes);
- ``session_pk`` mapping lands on the RIGHT surrogate session, creating
  local-owned sessions rows for strings that had none;
- OAuth subjects stamped onto pre-existing clients/codes/tokens;
- the legacy string-keyed version constraint is replaced by the
  owner-scoped partial unique index - proven by inserting two owners'
  version chains under ONE shared client string without collision;
- downgrade restores the 0002 world.
"""
import pytest
from sqlalchemy import text

from invincible.core.db import LOCAL_OWNER_EMAIL, make_engine, migration_heads
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_p2_mig"


def _upgrade_to(url: str, target: str) -> None:
    from alembic import command as alembic_command

    from invincible.core.db import migrations_config

    cfg = migrations_config(db_url=url)
    alembic_command.upgrade(cfg, target)


async def _scalar(engine, sql: str) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


@pytest.fixture
async def scratch_0002(admin_pg):
    """Scratch database at 0002, seeded with string-keyed continuity/run
    rows plus ownerless OAuth rows."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0002")
    engine = make_engine(url)

    statements = [
        # one REAL session ('alpha'); 'orphan-str' below is deliberately
        # referenced by continuity/run rows WITHOUT a sessions row - the
        # 0003 backfill must create its local-owned session and map it.
        "INSERT INTO sessions (user_id, project_id, client_session_id,"
        " created_at, updated_at)"
        " SELECT u.id, p.id, 'alpha', 1700000001.0, 1700000099.0"
        " FROM users u JOIN projects p ON p.user_id = u.id"
        " WHERE u.email = 'local@invincible.local' AND p.name = 'local'",
        "INSERT INTO task_states (session_id, task_key, status, payload,"
        " version, updated_by, updated_at) VALUES"
        " ('alpha', 'k', 'active', '{}', 1, 't', 1.0),"
        " ('orphan-str', 'k', 'active', '{}', 1, 't', 1.0)",
        "INSERT INTO checkpoints (session_id, task_key, state_version,"
        " note, created_at) VALUES"
        " ('alpha', 'k', 1, '', 2.0),"
        " ('orphan-str', 'k', 1, '', 2.0)",
        "INSERT INTO runs (request_id, session_id, provider_name, model_id,"
        " attempt_index, outcome, started_at) VALUES"
        " ('r1', 'alpha', 'p', 'm', 0, 'ok', 1.0),"
        " ('r2', 'orphan-str', 'p', 'm', 0, 'ok', 1.0)",
        # ownerless oauth rows (pre-Phase-2 shapes)
        "INSERT INTO oauth_clients (client_id, client_name, redirect_uris,"
        " created_at) VALUES ('c1', 'n', '[\"http://localhost:9/cb\"]', 1.0)",
        "INSERT INTO oauth_codes (code, client_id, redirect_uri,"
        " code_challenge, expires_at, used) VALUES"
        " ('cd', 'c1', 'http://localhost:9/cb', 'ch', 99.0, FALSE)",
        "INSERT INTO oauth_tokens (token_hash, token_type, client_id,"
        " expires_at, revoked, created_at) VALUES"
        " ('th', 'access', 'c1', 99.0, FALSE, 1.0)",
    ]
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))
    yield engine
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


async def test_upgrade_0003_backfills_and_preserves_counts(
    scratch_0002, pg_live
):
    from invincible.core.db import LOCAL_PROJECT_NAME

    engine = scratch_0002
    url = make_scratch_url(SCRATCH_NAME)
    before = {
        table: await _scalar(engine, f"SELECT COUNT(*) FROM {table}")
        for table in (
            "sessions", "turns", "messages", "task_states", "checkpoints",
            "runs", "oauth_clients", "oauth_codes", "oauth_tokens",
        )
    }

    _upgrade_to(url, "head")

    after = {
        table: await _scalar(engine, f"SELECT COUNT(*) FROM {table}")
        for table in before
    }
    # sessions grows by exactly one (the orphan string's new row);
    # nothing else moves.
    assert after["sessions"] == before["sessions"] + 1
    for table in set(before) - {"sessions"}:
        assert after[table] == before[table], table

    uid = await _scalar(
        engine,
        f"SELECT u.id FROM users u WHERE u.email = '{LOCAL_OWNER_EMAIL}'",
    )

    # OAuth subjects all point at the local owner.
    for table, column in (
        ("oauth_clients", "owner_user_id"),
        ("oauth_codes", "subject_user_id"),
        ("oauth_tokens", "subject_user_id"),
    ):
        n_unowned = await _scalar(
            engine,
            f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL",
        )
        n_local = await _scalar(
            engine,
            f"SELECT COUNT(*) FROM {table} WHERE {column} = {uid}",
        )
        assert n_unowned == 0, table
        assert n_local == before[table], table

    # session_pk mapping: both strings resolve to distinct local-owned rows.
    mapping = dict(await _rows(engine, (
        "SELECT s.client_session_id, s.id FROM sessions s"
        " JOIN projects p ON p.id = s.project_id"
        f" WHERE p.name = '{LOCAL_PROJECT_NAME}'"
    )))
    assert set(mapping) == {"alpha", "orphan-str"}
    for table in ("task_states", "checkpoints", "runs"):
        bad = await _scalar(
            engine,
            f"SELECT COUNT(*) FROM {table}"
            " WHERE session_pk IS NULL OR session_pk NOT IN"
            f" ({mapping['alpha']}, {mapping['orphan-str']})",
        )
        assert bad == 0, table

    # Constraint swap: legacy constraint gone, partial index present.
    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.table_constraints"
        " WHERE constraint_name = 'uq_task_states_version'"
    )) == 0
    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM pg_indexes"
        " WHERE indexname = 'uq_task_states_owner_version'"
    )) == 1


async def _rows(engine, sql: str) -> list[tuple]:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).all()


async def _exec(engine, sql: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql))


async def test_owner_scoped_version_chains_do_not_collide(
    scratch_0002, admin_pg, pg_live
):
    """THE Phase 2 isolation proof at the storage layer: two owners using
    the SAME client string maintain independent version chains."""
    engine = scratch_0002
    url = make_scratch_url(SCRATCH_NAME)
    _upgrade_to(url, "head")

    uid = await _scalar(
        engine,
        f"SELECT id FROM users WHERE email = '{LOCAL_OWNER_EMAIL}'",
    )
    # second owner with a project of their own
    await _exec(engine,
        "INSERT INTO users (email, created_at)"
        " VALUES ('other@example.com', 1.0)")
    await _exec(engine, (
        "INSERT INTO projects (user_id, name, is_default, created_at)"
        " SELECT id, 'personal', TRUE, 1.0 FROM users"
        " WHERE email = 'other@example.com'"
    ))
    other_uid = await _scalar(engine, (
        "SELECT id FROM users WHERE email = 'other@example.com'"
    ))
    other_pid = await _scalar(engine, (
        "SELECT p.id FROM projects p JOIN users u ON u.id = p.user_id"
        " WHERE u.email = 'other@example.com'"
    ))

    # Both owners resolve-or-create a session row for the SAME string.
    await _exec(engine, (
        "INSERT INTO sessions (user_id, project_id, client_session_id,"
        " created_at, updated_at)"
        f" VALUES ({uid},"
        " (SELECT p.id FROM projects p JOIN users u ON u.id = p.user_id"
        f"  WHERE u.email = '{LOCAL_OWNER_EMAIL}' AND p.is_default),"
        f" 'shared', 1.0, 1.0), ({other_uid}, {other_pid},"
        " 'shared', 1.0, 1.0)"
    ))
    alpha_pk, other_pk = [
        int(r[0]) for r in await _rows(engine, (
            "SELECT s.id FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.client_session_id = 'shared' ORDER BY s.id"
        ))
    ]

    insert_sql = (
        "INSERT INTO task_states (session_id, session_pk, task_key, status,"
        " payload, version, updated_by, updated_at)"
        " VALUES ('shared', :pk, 'k', 'active', '{}', :v, 't', 1.0)"
    )
    async with engine.begin() as conn:
        for pk in (alpha_pk, other_pk):
            await conn.execute(text(insert_sql), {"pk": pk, "v": 1})
            await conn.execute(text(insert_sql), {"pk": pk, "v": 2})

    # Same (string, key, version) across owners coexists...
    n_v1 = await _scalar(engine, (
        "SELECT COUNT(*) FROM task_states"
        " WHERE session_id='shared' AND task_key='k' AND version=1"
    ))
    assert n_v1 == 2

    # ...but the same OWNER cannot double-spend a version.
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(insert_sql), {"pk": alpha_pk, "v": 2})


async def test_downgrade_0003_restores_0002(scratch_0002, pg_live):
    engine = scratch_0002
    url = make_scratch_url(SCRATCH_NAME)

    # alembic targets a revision id:
    _upgrade_to(url, "0002")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == "0002"
    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.columns"
        " WHERE table_name='task_states' AND column_name='session_pk'"
    )) == 0
    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.table_constraints"
        " WHERE constraint_name = 'uq_task_states_version'"
    )) == 1
    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.tables"
        " WHERE table_schema='public' AND table_name = 'login_attempts'"
    )) == 0

    # Forward again stays consistent.
    _upgrade_to(url, "head")
    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == migration_heads()[0]
