# invincible/migrations/env.py
"""Alembic environment for Invincible (async engine).

URL resolution order: the ``db_url`` attribute set programmatically by
``invincible db upgrade``, then INVINCIBLE_DB_URL, then sqlalchemy.url from
a conventional alembic.ini. Fails loudly when none is available - migrations
must never silently target the wrong database.
"""
import os

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

from invincible.core.db import metadata as target_metadata
from invincible.core.db import run_coro_sync


def _resolve_url() -> str:
    url = (
        context.config.attributes.get("db_url")
        or os.getenv("INVINCIBLE_DB_URL")
        or context.config.get_main_option("sqlalchemy.url")
    )
    if not url:
        raise RuntimeError(
            "No database URL for migrations. Set INVINCIBLE_DB_URL or run "
            "`invincible db upgrade` (which passes the URL programmatically)."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = context.config.get_section(context.config.config_ini_section)
    engine = async_engine_from_config(section, prefix="sqlalchemy.")
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # run_coro_sync (not bare asyncio.run) so programmatic invocations
    # from an already-running loop keep working.
    run_coro_sync(run_migrations_online())
