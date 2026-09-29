# tests/test_migration_session_title_pin.py
"""Revision ``0015`` migration acceptance (live tier, scratch DBs).

The dashboard sidebar renames a conversation (``sessions.title``, NULL =
keep deriving the label from the first user message) and pins it to the
top (``sessions.pinned``, NOT NULL default false). Both are per-session
presentation state, so they ride on the ``sessions`` row.

Gates: upgrade 0014 -> 0015 adds both columns with the declared shape;
downgrade removes them; re-running is a no-op; ``core.db`` metadata
declares the SAME shape so a ``create_all``-built database converges with
a migrated one; and the sidebar read/write paths work on the migrated
schema (pinned-first ordering included).
"""
import pytest
from sqlalchemy import text

from invincible.core.db import make_engine
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_sessionmeta_mig"


def _upgrade_to(url: str, target: str) -> None:
    from alembic import command as alembic_command

    from invincible.core.db import migrations_config

    alembic_command.upgrade(migrations_config(db_url=url), target)


def _downgrade_to(url: str, target: str) -> None:
    from alembic import command as alembic_command

    from invincible.core.db import migrations_config

    alembic_command.downgrade(migrations_config(db_url=url), target)


async def _columns(engine) -> dict:
    """{column_name: (is_nullable, column_default)} for ``sessions``."""
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT column_name, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name = 'sessions'"
        ))).all()
    return {name: (nullable, default) for name, nullable, default in rows}


async def _version(engine) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(
            text("SELECT version_num FROM alembic_version"))).scalar_one()


@pytest.fixture
async def scratch_0014(admin_pg):
    """Scratch database upgraded only through 0014 (no title/pinned)."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0014")
    engine = make_engine(url)
    yield engine, url
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


async def test_upgrade_0015_adds_title_and_pinned(scratch_0014, pg_live):
    engine, url = scratch_0014
    assert "title" not in await _columns(engine)
    assert "pinned" not in await _columns(engine)

    _upgrade_to(url, "0015")

    columns = await _columns(engine)
    assert columns["title"] == ("YES", None)
    assert columns["pinned"][0] == "NO"
    assert columns["pinned"][1] == "false"
    assert await _version(engine) == "0015"


async def test_downgrade_0015_drops_the_columns(scratch_0014, pg_live):
    engine, url = scratch_0014
    _upgrade_to(url, "0015")
    _downgrade_to(url, "0014")

    columns = await _columns(engine)
    assert "title" not in columns and "pinned" not in columns
    assert await _version(engine) == "0014"


async def test_upgrade_is_re_runnable(scratch_0014, pg_live):
    """IF NOT EXISTS keeps a half-applied migration from wedging a deploy."""
    engine, url = scratch_0014
    _upgrade_to(url, "0015")
    _upgrade_to(url, "0015")  # must not raise "column already exists"

    columns = await _columns(engine)
    assert columns["title"] == ("YES", None)
    assert columns["pinned"][0] == "NO"


async def test_sidebar_management_on_the_migrated_schema(scratch_0014, pg_live):
    """The failure this guards: a rename/pin/sidebar query that only works
    on a create_all-built database. Exercise the real paths instead."""
    from invincible.core.db import ensure_local_owner
    from invincible.core.session_store import SessionStore

    engine, url = scratch_0014
    _upgrade_to(url, "0015")

    store = SessionStore(engine=engine)
    uid, pid = await ensure_local_owner(engine)
    owner = {"user_id": uid, "project_id": pid}
    await store.append("web-a", [{"role": "user", "content": "first chat"}],
                       **owner)
    await store.append("web-b", [{"role": "user", "content": "second chat"}],
                       **owner)

    rows = await store.sidebar_rows(uid, client_session_id_prefix="web-")
    assert [r["client_session_id"] for r in rows] == ["web-b", "web-a"]
    assert rows[0]["pinned"] is False and rows[0]["title"] is None

    old_pk = rows[1]["id"]
    assert await store.rename_session(
        old_pk, title="pinned one", **owner) is True
    assert await store.set_pinned(old_pk, pinned=True, **owner) is True

    rows = await store.sidebar_rows(uid, client_session_id_prefix="web-")
    assert [(r["client_session_id"], r["title"], r["pinned"])
            for r in rows] == [("web-a", "pinned one", True),
                               ("web-b", None, False)]

    assert await store.delete_session(old_pk, **owner) is True
    rows = await store.sidebar_rows(uid, client_session_id_prefix="web-")
    assert [r["client_session_id"] for r in rows] == ["web-b"]


async def test_create_all_converges_with_migrated(admin_pg, pg_live):
    """``create_all`` (fresh installs) must build the same columns the
    migration produces, or fresh and migrated databases silently differ.

    A FRESH scratch database on purpose: ``create_all`` never alters a
    pre-existing table, so the shared ``invincible_test`` database keeps
    whatever shape it was created with until it is migrated.
    """
    from invincible.core.db import create_all_from_metadata

    name = "invincible_sessionmeta_fresh"
    url = make_scratch_url(name)
    await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {name}")
    try:
        fresh_engine = make_engine(url)
        try:
            await create_all_from_metadata(fresh_engine)
            columns = await _columns(fresh_engine)
            assert columns["title"] == ("YES", None)
            assert columns["pinned"][0] == "NO"
            assert columns["pinned"][1] == "false"
        finally:
            await fresh_engine.dispose()
    finally:
        await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_metadata_declares_title_and_pinned():
    """``core.db`` is schema truth: the lifespan's create_all builds from
    this metadata, so both columns must live there too."""
    from invincible.core import db as db_module

    table = db_module.metadata.tables["sessions"]
    assert table.c.title.nullable is True
    assert table.c.pinned.nullable is False
    assert "false" in str(table.c.pinned.server_default.arg)

