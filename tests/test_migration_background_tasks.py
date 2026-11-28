"""Revision 0019 migration acceptance for durable background task records."""

import pytest
from sqlalchemy import text

from invincible.core.db import make_engine
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_bg_tasks_mig"
TABLE = "background_tasks"
USER_INDEX = "idx_bg_tasks_user_created"
STATUS_INDEX = "idx_bg_tasks_status"


def _upgrade_to(url: str, target: str) -> None:
    from alembic import command as alembic_command

    from invincible.core.db import migrations_config

    alembic_command.upgrade(migrations_config(db_url=url), target)


def _downgrade_to(url: str, target: str) -> None:
    from alembic import command as alembic_command

    from invincible.core.db import migrations_config

    alembic_command.downgrade(migrations_config(db_url=url), target)


async def _scalar(engine, sql: str) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


@pytest.fixture
async def scratch_0018(admin_pg):
    """Scratch database migrated through 0018, before the new table."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0018")
    engine = make_engine(url)
    yield engine, url
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


async def test_upgrade_0019_creates_table_columns_and_indexes(
    scratch_0018, pg_live
):
    engine, url = scratch_0018
    assert await _scalar(
        engine,
        "SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{TABLE}'",
    ) == 0

    _upgrade_to(url, "0019")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == "0019"
    assert await _scalar(
        engine,
        "SELECT COUNT(*) FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = '{TABLE}'",
    ) == 8
    assert await _scalar(
        engine,
        "SELECT COUNT(*) FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'background_tasks' "
        f"AND indexname IN ('{USER_INDEX}', '{STATUS_INDEX}')",
    ) == 2
    # The session reference is nullable and SET NULL on session delete;
    # confdeltype 'n' is PostgreSQL's ON DELETE SET NULL (asyncpg returns
    # the one-byte "char" type as bytes).
    assert await _scalar(
        engine,
        "SELECT confdeltype FROM pg_constraint "
        "WHERE conrelid = 'background_tasks'::regclass "
        "AND conname = 'background_tasks_session_pk_fkey'",
    ) == b"n"
    assert await _scalar(
        engine,
        "SELECT COUNT(*) FROM pg_constraint "
        "WHERE conrelid = 'background_tasks'::regclass "
        "AND conname = 'background_tasks_pkey'",
    ) == 1


async def test_downgrade_0019_drops_table_and_indexes(
    scratch_0018, pg_live
):
    engine, url = scratch_0018
    _upgrade_to(url, "0019")
    _downgrade_to(url, "0018")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == "0018"
    assert await _scalar(
        engine,
        "SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{TABLE}'",
    ) == 0
    assert await _scalar(
        engine,
        "SELECT COUNT(*) FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'background_tasks'",
    ) == 0


async def test_upgrade_0019_is_rerunnable(scratch_0018, pg_live):
    engine, url = scratch_0018
    _upgrade_to(url, "0019")
    _upgrade_to(url, "0019")

    assert await _scalar(
        engine,
        "SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{TABLE}'",
    ) == 1
    assert await _scalar(
        engine,
        "SELECT COUNT(*) FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'background_tasks' "
        f"AND indexname IN ('{USER_INDEX}', '{STATUS_INDEX}')",
    ) == 2


async def test_create_all_built_table_is_accepted_by_upgrade(
    scratch_0018, pg_live
):
    from invincible.core.db import create_all_from_metadata

    engine, url = scratch_0018
    await create_all_from_metadata(engine)
    _upgrade_to(url, "0019")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == "0019"
    assert await _scalar(
        engine,
        "SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{TABLE}'",
    ) == 1


def test_metadata_matches_migration_0019():
    from invincible.core.db import metadata

    table = metadata.tables[TABLE]
    assert set(table.c.keys()) == {
        "workflow_id",
        "user_id",
        "session_pk",
        "mode",
        "status",
        "created_at",
        "started_at",
        "finished_at",
    }
    assert table.c.session_pk.nullable is True
    assert table.c.user_id.nullable is False
    assert table.c.workflow_id.primary_key is True
    session_fk = next(
        fk for fk in table.c.session_pk.foreign_keys
        if fk.column.table.name == "sessions"
    )
    assert session_fk.ondelete == "SET NULL"
    assert {
        USER_INDEX, STATUS_INDEX
    } <= {index.name for index in table.indexes}
