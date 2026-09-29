# tests/test_migration_turns_seq_deferrable.py
"""Revision ``0014`` migration acceptance (live tier, scratch DBs).

``SessionStore._enforce_retention`` re-sequences a session's remaining
turns densely with ONE statement
(``UPDATE turns SET seq = ranked.rn - 1 ...``). PostgreSQL checks a
non-deferrable UNIQUE constraint row by row while the UPDATE runs, so a
row moving to a ``seq`` that a not-yet-updated row still holds raised
``UniqueViolationError`` on ``uq_turns_session_seq`` (production session
391, ``seq`` 181). Every later append to a session past the retention cap
then failed to persist its history.

Gates: upgrade 0013 -> 0014 makes the constraint DEFERRABLE INITIALLY
DEFERRED; downgrade restores the plain UNIQUE; re-running is a no-op;
``core.db`` metadata declares the SAME shape so a ``create_all``-built
database converges with a migrated one; and appends past the retention
cap persist on the migrated schema (the production failure mode).
"""
import pytest
from sqlalchemy import text

from invincible.core.db import make_engine
from tests.test_cli_db import make_scratch_url

SCRATCH_NAME = "invincible_turnsseq_mig"


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


async def _constraint_flags(engine) -> tuple:
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT condeferrable, condeferred FROM pg_constraint "
            "WHERE conname = 'uq_turns_session_seq'"
        ))).one()
    return (bool(row[0]), bool(row[1]))


@pytest.fixture
async def scratch_0013(admin_pg):
    """Scratch database upgraded only through 0013 (plain UNIQUE)."""
    url = make_scratch_url(SCRATCH_NAME)
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_NAME}")
    _upgrade_to(url, "0013")
    engine = make_engine(url)
    yield engine, url
    await engine.dispose()
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_NAME} WITH (FORCE)")


async def test_upgrade_0014_defers_the_constraint(scratch_0013, pg_live):
    engine, url = scratch_0013
    assert await _constraint_flags(engine) == (False, False)

    _upgrade_to(url, "0014")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version") == "0014"
    assert await _constraint_flags(engine) == (True, True)


async def test_downgrade_0014_restores_plain_unique(scratch_0013, pg_live):
    engine, url = scratch_0013
    _upgrade_to(url, "0014")
    _downgrade_to(url, "0013")

    assert await _scalar(
        engine, "SELECT version_num FROM alembic_version") == "0013"
    assert await _constraint_flags(engine) == (False, False)


async def test_upgrade_is_re_runnable(scratch_0013, pg_live):
    """The DROP is IF EXISTS, so a second run is a no-op. This keeps a
    half-applied migration from wedging the deploy."""
    engine, url = scratch_0013
    _upgrade_to(url, "0014")
    _upgrade_to(url, "0014")  # must not raise "constraint does not exist"

    assert await _constraint_flags(engine) == (True, True)


async def test_retention_resequencing_persists_past_cap(scratch_0013, pg_live):
    """Production failure mode: appends past the retention cap must persist
    instead of raising UniqueViolationError mid-resequence."""
    from invincible.core.db import ensure_local_owner
    from invincible.core.session_store import SessionStore

    engine, url = scratch_0013
    _upgrade_to(url, "0014")

    store = SessionStore(engine=engine)
    uid, pid = await ensure_local_owner(engine)
    owner = {"user_id": uid, "project_id": pid}

    for index in range(6):
        await store.append(
            "s",
            [{"role": "user", "content": f"q{index}"},
             {"role": "assistant", "content": f"a{index}"}],
            max_turns=2,
            **owner,
        )

    loaded = await store.load("s", max_turns=0, **owner)
    assert loaded == [
        {"role": "user", "content": "q4"},
        {"role": "assistant", "content": "a4"},
        {"role": "user", "content": "q5"},
        {"role": "assistant", "content": "a5"},
    ]


async def test_create_all_converges_with_migrated(admin_pg, pg_live):
    """``create_all`` (fresh installs) must build the same deferrable
    constraint the migration produces, or fresh and migrated databases
    silently differ.

    Uses a FRESH scratch database on purpose: ``create_all`` never alters
    a pre-existing table, so the shared ``invincible_test`` database keeps
    whatever shape it was created with until it is migrated.
    """
    from invincible.core.db import create_all_from_metadata

    name = "invincible_turnsseq_fresh"
    url = make_scratch_url(name)
    await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {name}")
    try:
        fresh_engine = make_engine(url)
        try:
            await create_all_from_metadata(fresh_engine)
            assert await _constraint_flags(fresh_engine) == (True, True)
        finally:
            await fresh_engine.dispose()
    finally:
        await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_metadata_declares_deferrable_constraint():
    """``core.db`` is schema truth: the deferrable shape must live in
    metadata too, since the lifespan's create_all builds from it."""
    from invincible.core import db as db_module

    table = db_module.metadata.tables["turns"]
    matches = [
        c for c in table.constraints
        if getattr(c, "name", None) == "uq_turns_session_seq"
    ]
    assert len(matches) == 1
    constraint = matches[0]
    assert sorted(constraint.columns.keys()) == ["seq", "session_id"]
    assert constraint.deferrable is True
    assert constraint.initially == "DEFERRED"
