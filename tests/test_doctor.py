import os

import pytest
from click.testing import CliRunner

from invincible import __version__
from invincible.cli import cli
from tests.conftest import TEST_DB_URL

VALID_YAML = (
    "providers:\n  - name: solo\n    tier: 1\n    base_url: https://solo.example.com/v1\n"
    "    api_key_env: SOLO_API_KEY\n    model_id: solo-model\n"
)

OWNER_LABEL = "INVINCIBLE_OWNER_SECRET exists (owner login for /mcp)"
DB_URL_LABEL = "INVINCIBLE_DB_URL exists"
REACHABLE_LABEL = "PostgreSQL reachable"
REVISION_LABEL = "schema revision matches head"

HERMETIC_DB_OK = [
    (DB_URL_LABEL, True, ""),
    (REACHABLE_LABEL, True, "postgresql+asyncpg://invincible@***/db"),
    (REVISION_LABEL, True, "revision 0001"),
]


@pytest.fixture(autouse=True)
def _clean_invincible_env(monkeypatch):
    """doctor reads env vars from the process environment (including a
    project .env loaded at import time) and earlier tests may leak them
    into it, so make every test start from a clean slate."""
    for key in (
        "GATEWAY_API_KEY",
        "INVINCIBLE_OWNER_SECRET",
        "MCP_SHARED_SECRET",
        "INVINCIBLE_CONFIG_PATH",
        "INVINCIBLE_DB_PATH",
        "INVINCIBLE_DB_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def _set_secrets(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gw-key")
    monkeypatch.setenv("INVINCIBLE_OWNER_SECRET", "owner-key")


def _invoke(args=None):
    return CliRunner().invoke(cli, args or ["doctor"])


def _hermetic_db(monkeypatch, rows=None):
    """Replace the live DB checks so provider/key/env-file behavior is
    testable without Postgres. The fake must be a coroutine function -
    doctor drives it through asyncio.run."""
    async def _fake_check_database(url):
        return list(rows or HERMETIC_DB_OK)

    monkeypatch.setattr("invincible.cli._check_database", _fake_check_database)


def _config_and_chdir(monkeypatch, tmp_path):
    config = tmp_path / "providers.yaml"
    config.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(config))
    monkeypatch.chdir(tmp_path)


def test_doctor_help():
    result = _invoke(["doctor", "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output
    assert "diagnostics" in result.output


def test_doctor_all_ok(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke()
    assert result.exit_code == 0
    assert f"Invincible version: {__version__}" in result.output
    assert "OK  providers.yaml exists" in result.output
    assert "OK  providers.yaml loads" in result.output
    assert f"OK  {DB_URL_LABEL}" in result.output
    assert f"OK  {REACHABLE_LABEL}" in result.output
    assert f"OK  {REVISION_LABEL}" in result.output
    assert "OK  GATEWAY_API_KEY exists" in result.output
    assert f"OK  {OWNER_LABEL}" in result.output


def test_doctor_prints_version(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke()
    assert result.exit_code == 0
    assert __version__ in result.output


def test_doctor_missing_secrets_fail(monkeypatch, tmp_path):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke()
    assert result.exit_code == 1
    assert "FAIL  GATEWAY_API_KEY exists" in result.output
    assert f"FAIL  {OWNER_LABEL}" in result.output


def test_doctor_legacy_alias_counts_as_owner_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    monkeypatch.setenv("GATEWAY_API_KEY", "gw-key")
    monkeypatch.setenv("MCP_SHARED_SECRET", "legacy-owner")
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke()
    assert result.exit_code == 0
    assert f"OK  {OWNER_LABEL}  (falling back to MCP_SHARED_SECRET)" in result.output


def test_doctor_missing_providers_yaml_fails(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _hermetic_db(monkeypatch)
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(missing))
    monkeypatch.chdir(tmp_path)

    result = _invoke()
    assert result.exit_code == 1
    assert f"FAIL  providers.yaml exists  ({missing})" in result.output
    assert "FAIL  providers.yaml loads" in result.output


def test_doctor_malformed_providers_yaml_fails(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _hermetic_db(monkeypatch)
    bad = tmp_path / "providers.yaml"
    bad.write_text("key: [1, 2\n", encoding="utf-8")
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(bad))
    monkeypatch.chdir(tmp_path)

    result = _invoke()
    assert result.exit_code == 1
    assert f"OK  providers.yaml exists  ({bad})" in result.output
    assert "FAIL  providers.yaml loads" in result.output


# --- database checks ---------------------------------------------------------


def test_doctor_missing_db_url_is_loud(monkeypatch, tmp_path):
    """No INVINCIBLE_DB_URL: doctor fails with a pointer at dev-db/setup.
    Uses the REAL _check_database - its missing-URL branch needs no PG."""
    _set_secrets(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke()
    assert result.exit_code == 1
    assert f"FAIL  {DB_URL_LABEL}" in result.output
    assert "invincible dev-db" in result.output
    assert f"FAIL  {REACHABLE_LABEL}" in result.output
    assert f"FAIL  {REVISION_LABEL}" in result.output


async def test_doctor_live_reports_connectivity_and_revision(
    pg_live, pg_engine, stamp_revision, monkeypatch, tmp_path
):
    """Live tier: against the real test database, stamped at head, every
    DB check passes and the revision is named."""
    from invincible.core.db import migration_heads

    _set_secrets(monkeypatch)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _config_and_chdir(monkeypatch, tmp_path)
    await stamp_revision(migration_heads()[0])

    result = _invoke()
    assert result.exit_code == 0
    assert f"OK  {DB_URL_LABEL}" in result.output
    assert f"OK  {REACHABLE_LABEL}" in result.output
    assert (
        f"OK  {REVISION_LABEL}  (revision {migration_heads()[0]})"
        in result.output
    )


async def test_doctor_live_schema_mismatch_is_loud(
    pg_live, pg_engine, stamp_revision, monkeypatch, tmp_path
):
    """A recorded revision that differs from the packaged head must FAIL
    loudly and tell the operator what to run."""
    _set_secrets(monkeypatch)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _config_and_chdir(monkeypatch, tmp_path)
    await stamp_revision("9999")

    result = _invoke()
    assert result.exit_code == 1
    line = next(
        ln for ln in result.output.splitlines() if REVISION_LABEL in ln
    )
    assert line.startswith("FAIL")
    assert "database at 9999, expected 0001" in line
    assert "`invincible db upgrade`" in line


async def test_doctor_live_unmanaged_populated_schema_is_loud(
    pg_live, pg_engine, stamp_revision, monkeypatch, tmp_path
):
    """Tables without alembic_version = unmanaged schema: loud FAIL."""
    _set_secrets(monkeypatch)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _config_and_chdir(monkeypatch, tmp_path)
    await stamp_revision(None)  # drop alembic_version entirely

    result = _invoke()
    assert result.exit_code == 1
    line = next(
        ln for ln in result.output.splitlines() if REVISION_LABEL in ln
    )
    assert line.startswith("FAIL")
    assert "unmanaged by Alembic" in line
    assert "`invincible db upgrade`" in line


def test_doctor_live_unreachable_database_fails(pg_live, monkeypatch, tmp_path):
    """A configured URL pointing at a dead server fails the reachability
    check while still crediting the URL's existence."""
    from sqlalchemy.engine import make_url

    _set_secrets(monkeypatch)
    dead_url = make_url(TEST_DB_URL).set(
        host="127.0.0.1", port=9, database="deadbeef"
    ).render_as_string()
    monkeypatch.setenv("INVINCIBLE_DB_URL", dead_url)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke()
    assert result.exit_code == 1
    assert f"OK  {DB_URL_LABEL}" in result.output
    reachable_line = next(
        ln for ln in result.output.splitlines() if REACHABLE_LABEL in ln
    )
    assert reachable_line.startswith("FAIL")
    # The masked URL is shown, never the raw secret-bearing one.
    assert "deadbeef" in reachable_line
    assert ":" + str(make_url(dead_url).password) not in result.output


async def test_doctor_live_empty_unmanaged_database_hinted(
    pg_live, admin_pg, monkeypatch, tmp_path
):
    """A reachable but completely empty database points at db upgrade."""
    from sqlalchemy.engine import make_url

    _set_secrets(monkeypatch)
    # Derived from the contract URL so CI (5432) and local (5433) both work.
    fresh_url = (
        make_url(TEST_DB_URL)
        .set(database="invincible_doctor_empty")
        .render_as_string(hide_password=False)
    )
    db_name = "invincible_doctor_empty"
    await admin_pg(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {db_name}")
    try:
        monkeypatch.setenv("INVINCIBLE_DB_URL", fresh_url)
        _config_and_chdir(monkeypatch, tmp_path)

        result = _invoke()
        assert result.exit_code == 1
        assert f"OK  {REACHABLE_LABEL}" in result.output
        line = next(
            ln for ln in result.output.splitlines() if REVISION_LABEL in ln
        )
        assert line.startswith("FAIL")
        assert "empty database" in line
    finally:
        await admin_pg(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")


# --- rich console ------------------------------------------------------------


def test_doctor_uses_rich_console_when_available(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    printed = []

    class FakeConsole:
        def print(self, text):
            printed.append(text)

    monkeypatch.setattr("invincible.cli._doctor_console", lambda: FakeConsole())
    result = _invoke()
    assert result.exit_code == 0
    assert f"Invincible version: {__version__}" in printed
    assert any("[green]OK[/green]" in line for line in printed)


def test_doctor_rich_console_propagates_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    printed = []

    class FakeConsole:
        def print(self, text):
            printed.append(text)

    monkeypatch.setattr("invincible.cli._doctor_console", lambda: FakeConsole())
    result = _invoke()
    assert result.exit_code == 1
    assert any("[red]FAIL[/red]  GATEWAY_API_KEY exists" in line for line in printed)


# --- env file handling --------------------------------------------------------


def test_doctor_loads_keys_from_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    (tmp_path / ".env").write_text(
        "GATEWAY_API_KEY=gw-from-env\nINVINCIBLE_OWNER_SECRET=owner-from-env\n"
        f"INVINCIBLE_DB_URL={TEST_DB_URL}\n",
        encoding="utf-8",
    )
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke()
    assert result.exit_code == 0
    assert "OK  GATEWAY_API_KEY exists" in result.output
    assert f"OK  {OWNER_LABEL}" in result.output
    # doctor stays quiet about the env file; output format is unchanged.
    assert "Loaded environment from" not in result.output


def test_doctor_existing_exports_win_over_env_file(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    (tmp_path / ".env").write_text(
        "GATEWAY_API_KEY=env-gw\nINVINCIBLE_OWNER_SECRET=env-owner\n",
        encoding="utf-8",
    )
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke()
    assert result.exit_code == 0
    assert "OK  GATEWAY_API_KEY exists" in result.output
    assert f"OK  {OWNER_LABEL}" in result.output
    assert os.environ["GATEWAY_API_KEY"] == "gw-key"
    assert os.environ["INVINCIBLE_OWNER_SECRET"] == "owner-key"


def test_doctor_missing_env_file_reports_missing_keys(monkeypatch, tmp_path):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke()
    assert result.exit_code == 1
    assert "FAIL  GATEWAY_API_KEY exists" in result.output
    assert f"FAIL  {OWNER_LABEL}" in result.output


def test_doctor_env_file_without_keys_still_fails(monkeypatch, tmp_path):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    (tmp_path / ".env").write_text(
        "SOME_OTHER_KEY=value\n", encoding="utf-8"
    )
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke()
    assert result.exit_code == 1
    assert "FAIL  GATEWAY_API_KEY exists" in result.output
    assert f"FAIL  {OWNER_LABEL}" in result.output


def test_doctor_custom_env_file_option(monkeypatch, tmp_path):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    custom = tmp_path / ".env.doctor"
    custom.write_text(
        "GATEWAY_API_KEY=custom-gw\nINVINCIBLE_OWNER_SECRET=custom-owner\n"
        f"INVINCIBLE_DB_URL={TEST_DB_URL}\n",
        encoding="utf-8",
    )
    _hermetic_db(monkeypatch)
    _config_and_chdir(monkeypatch, tmp_path)

    result = _invoke(["doctor", "--env-file", str(custom)])
    assert result.exit_code == 0
    assert "OK  GATEWAY_API_KEY exists" in result.output
    assert f"OK  {OWNER_LABEL}" in result.output
