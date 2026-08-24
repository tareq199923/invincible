# tests/test_main.py
"""Lifespan wiring tests (Phase 16): real stores over the shared test
database, PendingActionStore persistence flag, and the loud-but-
non-blocking schema-revision warning."""
import logging

import pytest
from sqlalchemy import text

import invincible.main as main
from invincible.core.db import migration_heads
from tests.conftest import TEST_DB_URL


@pytest.fixture
async def lifespan_env(monkeypatch, pg_engine):
    """Point INVINCIBLE_DB_URL at the shared test database and guarantee
    the alembic_version table is dropped afterwards so revision-state
    tests never leak into each other."""
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    monkeypatch.delenv("INVINCIBLE_PERSIST_PENDING_ACTIONS", raising=False)
    yield pg_engine
    async with pg_engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


async def _stamp(pg_engine, revision: str | None) -> None:
    """Force the schema's recorded revision (None = remove management)."""
    async with pg_engine.begin() as conn:
        if revision is None:
            await conn.execute(
                text("DROP TABLE IF EXISTS alembic_version"))
            return
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            " version_num VARCHAR(32) NOT NULL)"
        ))
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(text(
            "INSERT INTO alembic_version (version_num) VALUES (:r)"
        ), {"r": revision})


async def test_lifespan_pending_actions_default_off(lifespan_env):
    """Without INVINCIBLE_PERSIST_PENDING_ACTIONS the lifespan must
    construct a memory-only PendingActionStore - a restart orphans staged
    actions (the original design)."""
    async with main.lifespan(main.app):
        assert main.app.state.pending_actions._engine is None


async def test_lifespan_pending_actions_opt_in(lifespan_env, monkeypatch):
    """With INVINCIBLE_PERSIST_PENDING_ACTIONS set, staged actions attach to
    the shared engine and survive restarts."""
    monkeypatch.setenv("INVINCIBLE_PERSIST_PENDING_ACTIONS", "1")
    async with main.lifespan(main.app):
        assert main.app.state.pending_actions._engine is not None
        # Clean shutdown drains fire-and-forget writes before disposal.
        await main.app.state.pending_actions.flush_persisted()


async def test_lifespan_warns_when_schema_not_managed(lifespan_env, caplog):
    """create_all bootstraps tables without alembic_version; startup must
    warn loudly that `invincible db upgrade` should be run."""
    with caplog.at_level(logging.WARNING, logger="invincible.db"):
        async with main.lifespan(main.app):
            pass
    assert "not managed by Alembic" in caplog.text
    assert "invincible db upgrade" in caplog.text


async def test_lifespan_warns_on_stale_revision(lifespan_env, caplog):
    heads = migration_heads()
    assert heads, "packaged migrations must load"
    stale = next(iter(h for h in ("9999", "0000") if h not in heads))
    await _stamp(lifespan_env, stale)

    with caplog.at_level(logging.WARNING, logger="invincible.db"):
        async with main.lifespan(main.app):
            pass
    assert "does not match migration head" in caplog.text
    assert stale in caplog.text
    assert "invincible db upgrade" in caplog.text


async def test_lifespan_quiet_when_revision_matches_head(lifespan_env, caplog):
    """A database stamped at head must not trigger the stale-schema warning."""
    heads = migration_heads()
    await _stamp(lifespan_env, heads[0])

    with caplog.at_level(logging.WARNING, logger="invincible.db"):
        async with main.lifespan(main.app):
            pass
    assert "not managed by Alembic" not in caplog.text
    assert "does not match migration head" not in caplog.text
