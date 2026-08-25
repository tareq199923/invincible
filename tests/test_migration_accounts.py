# tests/test_migration_accounts.py
"""Platform Phase 3 migration acceptance (live tier, scratch databases).

Revision ``0004`` adds accounts schema: ``projects.archived_at``, the
scoped/widened ``login_attempts`` primary key, plus new ``user_identities``
and ``device_codes`` tables. Gates verified here:

- pre-existing login_attempts rows land in the 'owner' scope;
- the composite primary key actually enforces per-scope counters;
- identity-link and device-code uniqueness constraints hold;
- downgrade restores the 0003 world exactly.
"""
import time

import pytest
from sqlalchemy import text

from invincible.core.db import make_engine, migration_heads
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_p3_mig"


def _upgrade_to(url: str, target: str) -> None:
    from alembic import command as alembic_command

    from invincible.core.db import migrations_config

    cfg = migrations_config(db_url=url)
    alembic_command.upgrade(cfg, target)


def _downgrade_to(url: str, target: str) -> None:
    # alembic's upgrade() treats an already-passed ancestor as a no-op,
    # so downward moves go through command.downgrade explicitly.
    from alembic import command as alembic_command

    from invincible.core.db import migrations_config

    cfg = migrations_config(db_url=url)
    alembic_command.downgrade(cfg, target)


async def _scalar(engine, sql: str):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


async def _exec(engine, sql: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql))


@pytest.fixture
async def scratch_0003(admin_pg):
    """Scratch database at 0003 holding one pre-Phase-3 lockout row."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0003")
    engine = make_engine(url)
    await _exec(engine, (
        "INSERT INTO login_attempts (ip, window_start, count, updated_at)"
        " VALUES ('203.0.113.9', 1.0, 3, 1.0)"
    ))
    yield engine
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


async def test_upgrade_0004_accounts_schema(scratch_0003, pg_live):
    engine = scratch_0003
    url = make_scratch_url(SCRATCH_NAME)
    _upgrade_to(url, "head")
    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == migration_heads()[0]

    # projects.archived_at lands nullable
    nullable = await _scalar(engine, (
        "SELECT is_nullable FROM information_schema.columns"
        " WHERE table_name='projects' AND column_name='archived_at'"
    ))
    assert str(nullable) == "YES"

    # existing lockout rows are stamped into the owner scope
    scopes = await _scalar(engine, (
        "SELECT DISTINCT scope FROM login_attempts"
        " WHERE ip = '203.0.113.9'"
    ))
    assert scopes == "owner"

    # composite PK: a second scope for the same ip coexists...
    await _exec(engine, (
        "INSERT INTO login_attempts (ip, scope, window_start, count,"
        " updated_at) VALUES ('203.0.113.9', 'auth-login', 2.0, 1, 2.0)"
    ))
    assert await _scalar(engine,
                         "SELECT COUNT(*) FROM login_attempts") == 2
    # ...but the same (ip, scope) cannot double-spend its counter.
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await _exec(engine, (
            "INSERT INTO login_attempts (ip, scope, window_start, count,"
            " updated_at) VALUES ('203.0.113.9', 'auth-login', 3.0, 9, 3.0)"
        ))

    # user_identities: (provider, provider_account_id) is globally unique
    uid = await _scalar(engine, (
        "SELECT id FROM users WHERE email = 'local@invincible.local'"
    ))
    await _exec(engine, (
        f"INSERT INTO user_identities (user_id, provider,"
        f" provider_account_id, created_at)"
        f" VALUES ({uid}, 'github', '555', {time.time():.6f})"
    ))
    with pytest.raises(IntegrityError):
        await _exec(engine, (
            f"INSERT INTO user_identities (user_id, provider,"
            f" provider_account_id, created_at)"
            f" VALUES ({uid}, 'github', '555', {time.time():.6f})"
        ))

    # device_codes: user_code unique; hashed code is the PK
    await _exec(engine, (
        "INSERT INTO device_codes (device_code_hash, user_code, status,"
        " interval_seconds, created_at, expires_at)"
        " VALUES ('h1', 'ABCD2345', 'pending', 5, 1.0, 99.0)"
    ))
    with pytest.raises(IntegrityError):
        await _exec(engine, (
            "INSERT INTO device_codes (device_code_hash, user_code, status,"
            " interval_seconds, created_at, expires_at)"
            " VALUES ('h2', 'ABCD2345', 'pending', 5, 2.0, 99.0)"
        ))


async def test_downgrade_0004_restores_0003(scratch_0003, pg_live):
    engine = scratch_0003
    url = make_scratch_url(SCRATCH_NAME)
    _upgrade_to(url, "head")
    _downgrade_to(url, "0003")

    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.tables"
        " WHERE table_name IN ('device_codes', 'user_identities')"
    )) == 0
    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.columns"
        " WHERE table_name='projects' AND column_name='archived_at'"
    )) == 0
    assert await _scalar(engine, (
        "SELECT COUNT(*) FROM information_schema.columns"
        " WHERE table_name='login_attempts' AND column_name='scope'"
    )) == 0
    # the pre-existing owner-scoped counter survived both directions
    assert await _scalar(engine, (
        "SELECT count FROM login_attempts WHERE ip = '203.0.113.9'"
    )) == 3

    # Forward again stays consistent.
    _upgrade_to(url, "head")
    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version"
    ) == migration_heads()[0]
