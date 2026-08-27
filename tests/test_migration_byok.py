# tests/test_migration_byok.py
"""Platform Phase 9 migration acceptance (PR-A, live tier, scratch DBs).

Revision ``0007`` adds ``user_provider_credentials``. Gates:

- upgrade 0006 -> 0007 creates the table, unique constraint, and user index;
- downgrade removes them;
- fresh create_all databases already carrying the table skip cleanly
  (information-schema guard equivalence).
"""
import pytest
from sqlalchemy import text

from invincible.core.db import make_engine
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_byok_mig"


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


async def _scalar(engine, sql: str) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


@pytest.fixture
async def scratch_0006(admin_pg):
    """Scratch database upgraded only through 0006."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0006")
    engine = make_engine(url)
    yield engine, url
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


async def test_upgrade_0007_creates_table_index_constraint(
    scratch_0006, pg_live
):
    engine, url = scratch_0006

    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' "
        "AND table_name = 'user_provider_credentials'"
    )) == 0

    _upgrade_to(url, "0007")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == "0007"

    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' "
        "AND table_name = 'user_provider_credentials'"
    )) == 1

    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.table_constraints "
        "WHERE constraint_name = 'uq_user_provider_credentials_user_name'"
    )) == 1

    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM pg_indexes "
        "WHERE indexname = 'idx_user_provider_credentials_user'"
    )) == 1

    # Required columns present (including key_masked from decision 1).
    cols = {
        row[0]
        for row in (
            await _rows(engine, (
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'user_provider_credentials'"
            ))
        )
    }
    for required in (
        "id", "user_id", "provider_name", "catalog_key", "model_id",
        "base_url", "encrypted_api_key", "key_masked", "status",
        "last_tested_at", "created_at", "updated_at",
    ):
        assert required in cols, required


async def test_downgrade_0007_removes_table(scratch_0006, pg_live):
    engine, url = scratch_0006
    _upgrade_to(url, "0007")
    _downgrade_to(url, "0006")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == "0006"
    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' "
        "AND table_name = 'user_provider_credentials'"
    )) == 0


async def test_fresh_create_all_skips_cleanly(admin_pg, pg_live):
    """create_all already builds the table; 0007's guard must no-op."""
    from invincible.core.db import create_all_from_metadata

    name = "invincible_byok_fresh"
    url = make_scratch_url(name)
    await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {name}")
    engine = make_engine(url)
    try:
        await create_all_from_metadata(engine)
        assert await _scalar(engine, (
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' "
            "AND table_name = 'user_provider_credentials'"
        )) == 1

        # Stamp at 0006 then upgrade: guard sees existing table and skips.
        from alembic import command as alembic_command

        from invincible.core.db import migrations_config

        cfg = migrations_config(db_url=url)
        alembic_command.stamp(cfg, "0006")
        alembic_command.upgrade(cfg, "0007")

        assert await _scalar(
            engine, "SELECT version_num FROM alembic_version"
        ) == "0007"
        assert await _scalar(engine, (
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' "
            "AND table_name = 'user_provider_credentials'"
        )) == 1
    finally:
        await engine.dispose()
        await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


async def _rows(engine, sql: str) -> list[tuple]:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).all()
