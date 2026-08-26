# tests/test_phase4_schema.py
"""Phase 4 migration 0005: usage-token columns on ``runs`` and the
``memories`` search vector + GIN index.

Scratch-database discipline per repo convention: upgrades run against
throwaway databases through the same programmatic Alembic path the CLI
uses, both directions are exercised, and rows present before the upgrade
must survive it unchanged. Live tier only - auto-skips without Postgres.
"""
import asyncpg
import pytest
from sqlalchemy import make_url

from invincible.core.db import metadata, migrations_config


@pytest.fixture
async def scratch_db(admin_pg):
    """A throwaway database for migration runs; dropped afterwards."""
    name = "invincible_phase4_scratch"
    url = _scratch_url(name)
    await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {name}")
    yield url
    await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _scratch_url(name: str) -> str:
    from tests.conftest import TEST_DB_URL

    return make_url(TEST_DB_URL).set(database=name).render_as_string()


async def _connect_scratch(scratch_db):
    dsn = make_url(scratch_db).render_as_string().replace("+asyncpg", "")
    return await asyncpg.connect(dsn, timeout=10)


def _run_alembic(scratch_db, revision: str) -> None:
    from alembic import command as alembic_command

    alembic_command.upgrade(migrations_config(db_url=scratch_db), revision)


async def _columns(conn, table: str) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {r["column_name"]: r["data_type"] for r in rows}


async def _has_index(conn, name: str) -> bool:
    return await conn.fetchval(
        "SELECT 1 FROM pg_indexes WHERE indexname = $1", name
    ) is not None


# --- hermetic: metadata declares the Phase 4 shapes -------------------------


def test_metadata_declares_runs_token_columns():
    runs = metadata.tables["runs"]
    assert "input_tokens" in runs.c and "output_tokens" in runs.c
    assert runs.c.input_tokens.nullable and runs.c.output_tokens.nullable


def test_metadata_declares_memories_search_vector():
    memories = metadata.tables["memories"]
    col = memories.c.search_vector
    assert col.computed is not None
    sql = str(col.computed.sqltext).lower()
    assert "to_tsvector('english'" in sql
    assert col.computed.persisted is True
    index = next(
        i for i in memories.indexes if i.name == "idx_memories_search"
    )
    assert index.dialect_options["postgresql"]["using"] == "gin"


# --- live tier: real migrations against scratch databases --------------------


async def _seed_0004_rows(conn):
    """Insert one user/project/memory/run chain using ONLY 0004-era
    columns (no tokens, no search_vector), returning their ids."""
    user_id = await conn.fetchval(
        "INSERT INTO users (email, password_hash, is_system, created_at)"
        " VALUES ('p4@example.com', NULL, FALSE, 1700000000.0)"
        " RETURNING id"
    )
    project_id = await conn.fetchval(
        "INSERT INTO projects (user_id, name, is_default, created_at)"
        " VALUES ($1, 'p4', TRUE, 1700000000.0) RETURNING id",
        user_id,
    )
    memory_id = await conn.fetchval(
        "INSERT INTO memories (user_id, project_id, scope, layer, kind,"
        " content, confidence, provenance, created_at)"
        " VALUES ($1, $2, 'project', 'auto', 'fact',"
        " 'PostgreSQL performance tuning notes', 0.6, 'chat:s1',"
        " 1700000100.0) RETURNING id",
        user_id,
        project_id,
    )
    run_id = await conn.fetchval(
        "INSERT INTO runs (request_id, session_id, provider_name, model_id,"
        " attempt_index, outcome, started_at, finished_at)"
        " VALUES ('req-p4', 'sess-p4', 'alpha', 'alpha-mini', 1, 'ok',"
        " 1700000200.0, 1700000201.0) RETURNING id"
    )
    return user_id, project_id, memory_id, run_id


async def test_upgrade_from_0004_preserves_rows_and_adds_artifacts(
    scratch_db,
):
    conn = await _connect_scratch(scratch_db)
    try:
        _run_alembic(scratch_db, "0004")
        user_id, project_id, memory_id, run_id = await _seed_0004_rows(conn)
    finally:
        await conn.close()

    _run_alembic(scratch_db, "head")

    conn = await _connect_scratch(scratch_db)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version")

        runs_cols = await _columns(conn, "runs")
        assert runs_cols["input_tokens"] == "integer"
        assert runs_cols["output_tokens"] == "integer"

        mem_cols = await _columns(conn, "memories")
        assert mem_cols["search_vector"] == "tsvector"
        assert await _has_index(conn, "idx_memories_search")

        # Pre-existing rows survive untouched; token columns read NULL...
        row = await conn.fetchrow(
            "SELECT input_tokens, output_tokens FROM runs WHERE id = $1",
            run_id,
        )
        assert row["input_tokens"] is None and row["output_tokens"] is None

        # ...and the generated vector was computed for existing content -
        # lexical retrieval finds the seeded memory by its terms. ("tuning"
        # stems to "tune" on both sides; note "postgres" does NOT match
        # stored "postgresql" under the english config - stemmer asymmetry.)
        hit = await conn.fetchval(
            "SELECT id FROM memories"
            " WHERE search_vector @@ plainto_tsquery('english', 'tuning')"
        )
        assert hit == memory_id

        # Ownership chain intact.
        owner, scoped_project = await conn.fetchrow(
            "SELECT user_id, project_id FROM memories WHERE id = $1",
            memory_id,
        )
        assert owner == user_id and scoped_project == project_id
    finally:
        await conn.close()


async def test_downgrade_removes_artifacts_and_keeps_rows(scratch_db):
    conn = await _connect_scratch(scratch_db)
    try:
        _run_alembic(scratch_db, "head")
        _, _, memory_id, run_id = await _seed_0004_rows(conn)
    finally:
        await conn.close()

    from alembic import command as alembic_command

    from invincible.core.db import migrations_config as mc

    alembic_command.downgrade(mc(db_url=scratch_db), "0004")

    conn = await _connect_scratch(scratch_db)
    try:
        runs_cols = await _columns(conn, "runs")
        assert "input_tokens" not in runs_cols
        assert "output_tokens" not in runs_cols
        mem_cols = await _columns(conn, "memories")
        assert "search_vector" not in mem_cols
        assert not await _has_index(conn, "idx_memories_search")
        assert await conn.fetchval(
            "SELECT 1 FROM runs WHERE id = $1", run_id
        )
        assert await conn.fetchval(
            "SELECT 1 FROM memories WHERE id = $1", memory_id
        )
    finally:
        await conn.close()


async def test_create_all_shape_matches_upgraded_shape(scratch_db):
    """Schema truth handshake for the two touched tables: a database built
    by runtime create_all must expose the same columns as one migrated."""
    from invincible.core.db import create_all_from_metadata, make_engine

    engine = make_engine(scratch_db)
    try:
        await create_all_from_metadata(engine)
    finally:
        await engine.dispose()

    conn = await _connect_scratch(scratch_db)
    try:
        for table in ("runs", "memories"):
            created = set(await _columns(conn, table))
            expected = set(metadata.tables[table].columns.keys())
            assert created == expected, table
        assert await _has_index(conn, "idx_memories_search")
    finally:
        await conn.close()
