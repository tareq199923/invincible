"""Revision 0018 migration acceptance for background-task checkpoints."""

import pytest
from sqlalchemy import text

from invincible.core.db import make_engine
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_bg_checkpoint_mig"
TABLE = "background_task_checkpoints"
INDEX = "idx_bg_checkpoints_user_workflow"


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
async def scratch_0017(admin_pg):
    """Scratch database migrated through 0017, before the new table."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0017")
    engine = make_engine(url)
    yield engine, url
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


async def test_upgrade_0018_creates_table_constraints_and_index(
    scratch_0017, pg_live
):
    engine, url = scratch_0017
    assert await _scalar(
        engine,
        f"SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{TABLE}'",
    ) == 0

    _upgrade_to(url, "0018")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == "0018"
    assert await _scalar(
        engine,
        f"SELECT COUNT(*) FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = '{TABLE}'",
    ) == 10
    assert await _scalar(
        engine,
        f"SELECT COUNT(*) FROM pg_indexes "
        f"WHERE schemaname = 'public' AND tablename = '{TABLE}' "
        f"AND indexname = '{INDEX}'",
    ) == 1
    assert await _scalar(
        engine,
        f"SELECT COUNT(*) FROM pg_constraint "
        f"WHERE conrelid = '{TABLE}'::regclass "
        "AND conname IN ("
        "'uq_bg_checkpoints_session_seq', "
        "'uq_bg_checkpoints_session_call')",
    ) == 2


async def test_downgrade_0018_drops_table_and_index(scratch_0017, pg_live):
    engine, url = scratch_0017
    _upgrade_to(url, "0018")
    _downgrade_to(url, "0017")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == "0017"
    assert await _scalar(
        engine,
        f"SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{TABLE}'",
    ) == 0


async def test_upgrade_0018_is_rerunnable(scratch_0017, pg_live):
    engine, url = scratch_0017
    _upgrade_to(url, "0018")
    _upgrade_to(url, "0018")

    assert await _scalar(
        engine,
        f"SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{TABLE}'",
    ) == 1
    assert await _scalar(
        engine,
        f"SELECT COUNT(*) FROM pg_indexes "
        f"WHERE schemaname = 'public' AND tablename = '{TABLE}' "
        f"AND indexname = '{INDEX}'",
    ) == 1


async def test_create_all_built_table_is_accepted_by_upgrade(
    scratch_0017, pg_live
):
    from invincible.core.db import create_all_from_metadata

    engine, url = scratch_0017
    await create_all_from_metadata(engine)
    _upgrade_to(url, "0018")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == "0018"
    assert await _scalar(
        engine,
        f"SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{TABLE}'",
    ) == 1


def test_metadata_matches_migration_0018():
    from invincible.core.db import metadata

    table = metadata.tables[TABLE]
    assert set(table.c.keys()) == {
        "id",
        "user_id",
        "session_pk",
        "workflow_id",
        "seq",
        "call_id",
        "tool_name",
        "args_hash",
        "result_bytes",
        "created_at",
    }
    assert {
        constraint.name for constraint in table.constraints
    } >= {
        "uq_bg_checkpoints_session_seq",
        "uq_bg_checkpoints_session_call",
    }
    assert INDEX in {index.name for index in table.indexes}
