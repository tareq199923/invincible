# tests/test_cli_users.py
"""`invincible users` - the host account-inspection CLI.

Phase 2 removed the operator surface: promote/demote are gone (the role
column is dormant - kept only because migrations reference it), so this
file pins what remains - `users list` shows accounts and roles
informationally, and the group stays wired into the CLI.
"""
from click.testing import CliRunner
from sqlalchemy import text

from invincible.cli import cli
from tests.conftest import TEST_DB_URL


async def _seed_plain_user(pg_engine, email="listed@example.com"):
    """Insert a plain-role user directly; returns its id."""
    async with pg_engine.begin() as conn:
        uid = (await conn.execute(text(
            "INSERT INTO users (email, created_at)"
            " VALUES (:e, 1.0) RETURNING id"
        ), {"e": email})).scalar_one()
    return uid


async def test_list_shows_accounts_and_roles(pg_engine, monkeypatch):
    await _seed_plain_user(pg_engine, "listed@example.com")
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    result = CliRunner().invoke(cli, ["users", "list"])
    assert result.exit_code == 0, result.output
    assert "listed@example.com  user" in result.output


async def test_list_empty_instance(pg_engine, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    result = CliRunner().invoke(cli, ["users", "list"])
    assert result.exit_code == 0, result.output
    assert "No accounts." in result.output


def test_users_group_registered():
    """The group is wired into the top-level CLI help."""
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "users" in result.output


def test_promote_demote_are_gone():
    """Phase 2: the operator elevation commands no longer exist."""
    for verb in ("promote", "demote"):
        result = CliRunner().invoke(cli, ["users", verb, "x@example.com"])
        assert result.exit_code != 0, (verb, result.output)
        assert "No such command" in result.output
