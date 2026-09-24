# tests/test_migration_hot_path_indexes.py
"""Revision ``0010`` migration acceptance (live tier, scratch DBs).

Adds four indexes and no shape change:

- ``runs.request_id`` - looked up on every streamed request;
- ``oauth_codes.expires_at`` / ``oauth_tokens.expires_at`` - the OAuth
  sweep deletes by them;
- ``oauth_tokens.client_id`` - revocation and listing filter by it.

Gates: upgrade 0009 -> 0010 creates all four; downgrade removes them;
re-running is a no-op (the guards exist so a half-applied migration never
wedges a deploy); and ``core.db`` metadata declares the SAME names, so a
``create_all``-built database converges with a migrated one.
"""
import pytest
from sqlalchemy import text

from invincible.core.db import make_engine
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_hotpath_mig"

# (table, index name) - must match both the migration and core.db.
INDEXES = (
    ("runs", "idx_runs_request_id"),
    ("oauth_codes", "idx_oauth_codes_expires"),
    ("oauth_tokens", "idx_oauth_tokens_expires"),
    ("oauth_tokens", "idx_oauth_tokens_client"),
)


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


async def _index_count(engine, table: str, name: str) -> int:
    return await _scalar(engine, (
        "SELECT COUNT(*) FROM pg_indexes "
        f"WHERE schemaname = 'public' AND tablename = '{table}' "
        f"AND indexname = '{name}'"
    ))


@pytest.fixture
async def scratch_0009(admin_pg):
    """Scratch database upgraded only through 0009."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0009")
    engine = make_engine(url)
    yield engine, url
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


async def test_upgrade_0010_creates_the_indexes(scratch_0009, pg_live):
    engine, url = scratch_0009
    for table, name in INDEXES:
        assert await _index_count(engine, table, name) == 0, name

    _upgrade_to(url, "0010")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version") == "0010"
    for table, name in INDEXES:
        assert await _index_count(engine, table, name) == 1, name


async def test_downgrade_0010_drops_them(scratch_0009, pg_live):
    engine, url = scratch_0009
    _upgrade_to(url, "0010")
    _downgrade_to(url, "0009")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version") == "0009"
    for table, name in INDEXES:
        assert await _index_count(engine, table, name) == 0, name


async def test_upgrade_is_re_runnable(scratch_0009, pg_live):
    """Every statement is guarded, so a second run is a no-op. This is what
    keeps a half-applied migration from wedging the deploy."""
    engine, url = scratch_0009
    _upgrade_to(url, "0010")
    _upgrade_to(url, "0010")  # must not raise "index already exists"

    for table, name in INDEXES:
        assert await _index_count(engine, table, name) == 1, name


def test_metadata_declares_the_migrated_indexes():
    """``core.db`` is schema truth and the lifespan's create_all builds
    from it, so drift here means a fresh database and a migrated one
    silently differ."""
    from invincible.core import db as db_module

    for table_name, index_name in INDEXES:
        table = db_module.metadata.tables[table_name]
        declared = {index.name for index in table.indexes}
        assert index_name in declared, (
            f"{index_name} is in migration 0010 but missing from "
            f"core.db's {table_name} metadata"
        )
