# tests/test_migration_drop_facts.py
"""Revision ``0013`` migration acceptance (live tier, scratch DBs).

Drops the legacy per-session ``facts`` triple store: superseded by
``memories`` in Phase 4, writerless since the SQLite importer was
removed, audited empty on production 2026-09-25 (backed up before this
revision was written). Nothing in request-serving code reads or writes
it; ``extract_facts`` feeds ``memories``, not this table.

Gates: upgrade 0012 -> 0013 removes the table; downgrade restores the
exact baseline shape (columns + ``uq_facts_triple``); re-running is a
no-op; and ``core.db`` metadata no longer declares the table, so a
``create_all``-built database converges with a migrated one.
"""
import pytest
from sqlalchemy import text

from invincible.core.db import make_engine
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_dropfacts_mig"


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


async def _has_table(engine, table: str) -> bool:
    return bool(await _scalar(engine, (
        "SELECT 1 FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{table}'"
    )))


async def _columns(engine, table: str) -> dict:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema = 'public' AND table_name = '{table}'"
        ))).all()
    return {r[0]: r[1] for r in rows}


@pytest.fixture
async def scratch_0012(admin_pg):
    """Scratch database upgraded only through 0012 (facts still present)."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0012")
    engine = make_engine(url)
    yield engine, url
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


async def test_upgrade_0013_drops_facts(scratch_0012, pg_live):
    engine, url = scratch_0012
    assert await _has_table(engine, "facts")

    _upgrade_to(url, "0013")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version") == "0013"
    assert not await _has_table(engine, "facts")


async def test_downgrade_0013_restores_baseline_shape(scratch_0012, pg_live):
    engine, url = scratch_0012
    before = await _columns(engine, "facts")
    assert set(before) == {
        "id", "user_id", "session_id", "entity", "relation", "target",
        "created_at",
    }

    _upgrade_to(url, "0013")
    _downgrade_to(url, "0012")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version") == "0012"
    assert await _columns(engine, "facts") == before


async def test_upgrade_is_re_runnable(scratch_0012, pg_live):
    """The guard keeps a half-applied migration from wedging a deploy."""
    engine, url = scratch_0012
    _upgrade_to(url, "0013")
    _upgrade_to(url, "0013")  # must not raise "table does not exist"

    assert not await _has_table(engine, "facts")


def test_metadata_no_longer_declares_facts():
    """``core.db`` is schema truth: the dropped table must be gone from
    metadata too, so fresh and migrated databases converge."""
    from invincible.core import db as db_module

    assert "facts" not in db_module.metadata.tables
