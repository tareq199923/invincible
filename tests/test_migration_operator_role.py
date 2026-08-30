# tests/test_migration_operator_role.py
"""Operator-role migration acceptance (live tier, scratch DBs).

Revision ``0008`` adds ``users.role`` and elevates the system local owner
to operator - the consent-gate half of the Phase 5 hardening. Gates:

- upgrade 0007 -> 0008 adds the column with default 'user' and elevates
  the local owner;
- downgrade removes the column;
- fresh create_all databases already carrying the column skip cleanly
  (information-schema guard equivalence).
"""
import pytest
from sqlalchemy import text

from invincible.core.db import LOCAL_OWNER_EMAIL, make_engine
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_role_mig"


def _upgrade_to(url: str, target: str) -> None:
    from alembic import command as alembic_command

    from invincible.core.db import migrations_config

    cfg = migrations_config(db_url=url)
    alembic_command.upgrade(cfg, target)


def _downgrade_to(url: str, target: str) -> None:
    from alembic import command as alembic_command

    from invincible.core.db import migrations_config

    cfg = migrations_config(db_url=url)
    alembic_command.downgrade(cfg, target)


async def _scalar(engine, sql: str, **params) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def _has_role_column(engine) -> bool:
    return await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'users' "
        "AND column_name = 'role'"
    )) == 1


@pytest.fixture
async def scratch_0007(admin_pg):
    """Scratch database upgraded only through 0007 (no role column)."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0007")
    # 0002 seeds the local owner; a plain second account proves the
    # default is 'user' while the local owner gets elevated.
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO users (email, created_at) "
            "VALUES ('plain@example.com', 1.0)"))
    yield engine, url
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


async def test_upgrade_0008_adds_role_and_elevates_local_owner(
    scratch_0007, pg_live
):
    engine, url = scratch_0007
    assert not await _has_role_column(engine)

    _upgrade_to(url, "0008")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version") == "0008"
    assert await _has_role_column(engine)
    # Backfill default for every pre-existing row...
    assert await _scalar(
        engine, "SELECT role FROM users WHERE email = 'plain@example.com'"
    ) == "user"
    # ...except the local owner, which becomes an operator.
    assert await _scalar(
        engine, "SELECT role FROM users WHERE email = :e",
        e=LOCAL_OWNER_EMAIL,
    ) == "operator"


async def test_downgrade_0008_removes_column(scratch_0007, pg_live):
    engine, url = scratch_0007
    _upgrade_to(url, "0008")
    _downgrade_to(url, "0007")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version") == "0007"
    assert not await _has_role_column(engine)


async def test_fresh_create_all_skips_cleanly(admin_pg, pg_live):
    """create_all already builds users.role; 0008's guard must no-op."""
    from invincible.core.db import create_all_from_metadata

    name = "invincible_role_fresh"
    url = make_scratch_url(name)
    await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {name}")
    engine = make_engine(url)
    try:
        await create_all_from_metadata(engine)
        assert await _has_role_column(engine)

        # Stamp at 0007 then upgrade: guard sees the column and skips,
        # but the local-owner elevation UPDATE still runs (idempotent).
        from alembic import command as alembic_command

        from invincible.core.db import (
            migrations_config,
            seed_local_owner_conn,
        )

        async with engine.begin() as conn:
            await seed_local_owner_conn(conn)

        cfg = migrations_config(db_url=url)
        alembic_command.stamp(cfg, "0007")
        alembic_command.upgrade(cfg, "0008")

        assert await _scalar(
            engine, "SELECT version_num FROM alembic_version") == "0008"
        assert await _has_role_column(engine)
        assert await _scalar(
            engine, "SELECT role FROM users WHERE email = :e",
            e=LOCAL_OWNER_EMAIL,
        ) == "operator"
    finally:
        await engine.dispose()
        await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
