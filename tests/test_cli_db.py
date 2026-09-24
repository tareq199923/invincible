# tests/test_cli_db.py
"""Phase 16 CLI surface: `dev-db` provisioning, Alembic `db upgrade`, and URL
normalization.

Live-tier tests (real local Postgres) auto-skip via the pg_live fixture so
the suite stays runnable on machines without a server; everything else is
hermetic.
"""
import asyncio
import socket

import pytest
from click.testing import CliRunner

from invincible.cli import DevDbError, _normalize_db_url, cli
from tests.conftest import TEST_DB_URL

# --- _normalize_db_url (hermetic) --------------------------------------------


def test_normalize_accepts_asyncpg_url_unchanged():
    raw = "postgresql+asyncpg://invincible:pw@127.0.0.1:5433/inv"
    assert _normalize_db_url(raw) == raw


def test_normalize_upgrades_plain_postgres_scheme(capsys):
    url = _normalize_db_url("postgresql://invincible:pw@127.0.0.1:5433/inv")
    # Password must survive normalization - SA 2.0 masks by default and a
    # masked DSN written to .env would fail confusingly at start.
    assert url == "postgresql+asyncpg://invincible:pw@127.0.0.1:5433/inv"
    assert "Normalized postgresql://" in capsys.readouterr().out


def test_normalize_rejects_unsupported_driver():
    with pytest.raises(ValueError, match="Unsupported driver"):
        _normalize_db_url("postgresql+psycopg2://u@h/db")
    with pytest.raises(ValueError, match="Unsupported driver"):
        _normalize_db_url("mysql://u@h/db")


def test_normalize_rejects_garbage():
    with pytest.raises(ValueError, match="Not a valid database URL"):
        _normalize_db_url("not-a-dsn at all")


# --- dev-db (hermetic) --------------------------------------------------------


def _bind_listener():
    """A TCP listener on a free localhost port (R4 stand-in for a busy port)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


async def test_port_busy_detects_any_listener():
    from invincible.cli import _port_busy

    sock = _bind_listener()
    port = sock.getsockname()[1]
    try:
        assert await _port_busy("127.0.0.1", port)
    finally:
        sock.close()
    # A closed listener frees the port again.
    await asyncio.sleep(0.05)
    assert not await _port_busy("127.0.0.1", port)


async def test_port_busy_false_on_free_port():
    from invincible.cli import _port_busy

    probe = _bind_listener()
    port = probe.getsockname()[1]
    probe.close()
    assert not await _port_busy("127.0.0.1", port)


async def test_first_free_port_skips_busy_ports():
    from invincible.cli import _first_free_port

    # Two listeners on consecutive ports: bind pairs until one lands
    # adjacent (ephemeral allocation usually needs no retries).
    first = second = None
    for _ in range(50):
        first = _bind_listener()
        second = _bind_listener()
        if second.getsockname()[1] == first.getsockname()[1] + 1:
            break
        first.close()
        second.close()
        first = second = None
    if first is None:
        pytest.skip("could not secure two consecutive free ports")
    try:
        base = first.getsockname()[1]
        assert await _first_free_port(base) == base + 2
    finally:
        first.close()
        second.close()


async def test_dev_db_starts_docker_on_next_free_port(monkeypatch):
    """R4: with 5433 occupied by anything, provisioning must publish the
    container on the next free port - and that port lands in the DSN."""
    from invincible.cli import _provision_dev_db_async

    blocker = _bind_listener()
    blocker_port = blocker.getsockname()[1]

    started_ports = []

    async def fake_start(port):
        started_ports.append(port)
        return f"postgresql://invincible@127.0.0.1:{port}/postgres"

    async def fake_connect(dsn):
        # Only the freshly started server answers; the blocker is not
        # Postgres and the conventional ports have nothing.
        return f":{started_ports[0]}/" in dsn if started_ports else False

    async def fake_ensure(admin_dsn, database="invincible"):
        return True

    monkeypatch.setattr(
        "invincible.cli._start_postgres_via_docker", fake_start)
    monkeypatch.setattr("invincible.cli._try_connect", fake_connect)
    monkeypatch.setattr("invincible.cli._ensure_database", fake_ensure)
    monkeypatch.delenv("INVINCIBLE_DB_URL", raising=False)

    try:
        url, notes = await _provision_dev_db_async(blocker_port)
    finally:
        blocker.close()

    assert started_ports == [blocker_port + 1]
    assert f":{blocker_port + 1}/" in url
    assert any(
        f"port {blocker_port} is busy" in note for note in notes)


async def test_dev_db_uses_requested_port_when_free(monkeypatch):
    from invincible.cli import _provision_dev_db_async

    finder = _bind_listener()
    free_port = finder.getsockname()[1]
    finder.close()  # release: the port is (almost certainly) free again

    started_ports = []

    async def fake_start(port):
        started_ports.append(port)
        return f"postgresql://invincible@127.0.0.1:{port}/postgres"

    async def fake_connect(dsn):
        return bool(started_ports) and f":{started_ports[0]}/" in dsn

    async def fake_ensure(admin_dsn, database="invincible"):
        return True

    monkeypatch.setattr(
        "invincible.cli._start_postgres_via_docker", fake_start)
    monkeypatch.setattr("invincible.cli._try_connect", fake_connect)
    monkeypatch.setattr("invincible.cli._ensure_database", fake_ensure)
    monkeypatch.delenv("INVINCIBLE_DB_URL", raising=False)

    url, notes = await _provision_dev_db_async(free_port)
    assert started_ports == [free_port]
    assert f":{free_port}/" in url
    assert not any("is busy" in note for note in notes)


def test_dev_db_prints_provisioned_url(monkeypatch):
    def fake_provision(port=5433):
        return (
            "postgresql+asyncpg://invincible@127.0.0.1:5433/invincible",
            ["found local Postgres", "created database 'invincible'"],
        )

    monkeypatch.setattr("invincible.cli._provision_dev_db", fake_provision)

    result = CliRunner().invoke(cli, ["dev-db"])
    assert result.exit_code == 0, result.output
    assert "dev-db: found local Postgres" in result.output
    assert "INVINCIBLE_DB_URL=postgresql+asyncpg://" in result.output
    assert "`invincible db upgrade`" in result.output


def test_dev_db_write_env_merges_preserving_comments(monkeypatch, tmp_path):
    def fake_provision(port=5433):
        return (
            "postgresql+asyncpg://invincible@127.0.0.1:5433/invincible",
            [],
        )

    monkeypatch.setattr("invincible.cli._provision_dev_db", fake_provision)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# my keys\nUNRELATED_SETTING=keep\n", encoding="utf-8"
    )
    monkeypatch.setenv("INVINCIBLE_DB_URL", "")  # not set -> provisions

    result = CliRunner().invoke(
        cli, ["dev-db", "--env-file", str(env_file), "--write-env"]
    )
    assert result.exit_code == 0, result.output
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# my keys"
    assert lines[1] == "UNRELATED_SETTING=keep"
    assert any(
        line.startswith(
            "INVINCIBLE_DB_URL=postgresql+asyncpg://invincible@127.0.0.1:5433/invincible"
        )
        for line in lines
    )
    assert "Wrote INVINCIBLE_DB_URL" in result.output


def test_dev_db_failure_exits_with_guidance(monkeypatch):
    def boom(port=5433):
        raise DevDbError("no server, no docker")

    monkeypatch.setattr("invincible.cli._provision_dev_db", boom)
    result = CliRunner().invoke(cli, ["dev-db"])
    assert result.exit_code == 1
    assert "no server, no docker" in result.output


async def test_dev_db_honors_existing_reachable_url(pg_live, monkeypatch):
    """Live tier: a reachable INVINCIBLE_DB_URL is verified, not replaced."""
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    result = CliRunner().invoke(cli, ["dev-db"])
    assert result.exit_code == 0, result.output
    assert "verified existing INVINCIBLE_DB_URL" in result.output
    assert f"INVINCIBLE_DB_URL={TEST_DB_URL}" in result.output


# --- db upgrade (live tier) ----------------------------------------------------


@pytest.fixture
async def scratch_db(admin_pg):
    """A throwaway database for migration runs; dropped afterwards."""
    name = "invincible_upgrade_scratch"
    url = make_scratch_url(name)
    await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {name}")
    yield url
    await admin_pg(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def make_scratch_url(name: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(TEST_DB_URL).set(database=name).render_as_string()


def test_db_upgrade_requires_db_url(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # isolate from any repo-root .env
    monkeypatch.delenv("INVINCIBLE_DB_URL", raising=False)
    result = CliRunner().invoke(cli, ["db", "upgrade"])
    assert result.exit_code == 1
    assert "invincible dev-db" in result.output


async def test_db_upgrade_creates_full_schema(scratch_db, monkeypatch):
    """CLI-level round trip: upgrade head on an empty scratch database,
    then confirm every metadata table (plus alembic_version) exists."""
    from sqlalchemy import inspect

    from invincible.core.db import make_engine, metadata

    monkeypatch.setenv("INVINCIBLE_DB_URL", scratch_db)
    result = CliRunner().invoke(cli, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert "Database upgraded to revision" in result.output

    engine = make_engine(scratch_db)

    def reflect(conn):
        insp = inspect(conn)
        return sorted(insp.get_table_names())

    async with engine.connect() as conn:
        tables = await conn.run_sync(reflect)
    await engine.dispose()

    assert tables == sorted(set(metadata.tables) | {"alembic_version"})

    # Idempotent: a second upgrade to head is a clean no-op.
    second = CliRunner().invoke(cli, ["db", "upgrade"])
    assert second.exit_code == 0, second.output


# --- db command surface ---------------------------------------------------------


def test_db_help_exposes_upgrade_only():
    result = CliRunner().invoke(cli, ["db", "--help"])
    assert result.exit_code == 0, result.output
    assert "upgrade" in result.output
    assert "import" not in result.output
