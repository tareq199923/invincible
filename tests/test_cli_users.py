# tests/test_cli_users.py
"""`invincible users` - the operator-role elevation CLI.

Gates: promote flips a plain account to operator (and audit-writes it);
demote flips it back; both are no-ops when the role already matches;
unknown email is a clean error; the system local owner is refused
(its role is operator by construction - seed + migration 0008).
"""
from click.testing import CliRunner
from sqlalchemy import text

from invincible.cli import cli
from invincible.core.db import LOCAL_OWNER_EMAIL
from tests.conftest import TEST_DB_URL


async def _seed_plain_user(pg_engine, email="promote-me@example.com"):
    """Insert a plain-role user directly; returns its id."""
    async with pg_engine.begin() as conn:
        uid = (await conn.execute(text(
            "INSERT INTO users (email, created_at)"
            " VALUES (:e, 1.0) RETURNING id"
        ), {"e": email})).scalar_one()
    return uid


async def _role_of(pg_engine, uid: int) -> str:
    async with pg_engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT role FROM users WHERE id = :id"), {"id": uid})).scalar()


async def _audit_rows(pg_engine, action: str) -> list:
    async with pg_engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT meta FROM audit_log WHERE action = :a ORDER BY id"
        ), {"a": action})).all()


async def test_promote_grants_operator_role(pg_engine, monkeypatch):
    uid = await _seed_plain_user(pg_engine)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    result = CliRunner().invoke(cli, ["users", "promote",
                                       "promote-me@example.com"])
    assert result.exit_code == 0, result.output
    assert "promoted to operator" in result.output
    assert await _role_of(pg_engine, uid) == "operator"
    rows = await _audit_rows(pg_engine, "auth.user_promoted")
    assert any("promote-me@example.com" in str(r[0]) for r in rows)


async def test_demote_restores_plain_role(pg_engine, monkeypatch):
    uid = await _seed_plain_user(pg_engine, "demote-me@example.com")
    async with pg_engine.begin() as conn:
        await conn.execute(text(
            "UPDATE users SET role = 'operator' WHERE id = :id"),
            {"id": uid})
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    result = CliRunner().invoke(cli, ["users", "demote",
                                       "demote-me@example.com"])
    assert result.exit_code == 0, result.output
    assert "demoted to user" in result.output
    assert await _role_of(pg_engine, uid) == "user"


async def test_promote_is_noop_when_already_operator(
    pg_engine, monkeypatch
):
    uid = await _seed_plain_user(pg_engine, "already-op@example.com")
    async with pg_engine.begin() as conn:
        await conn.execute(text(
            "UPDATE users SET role = 'operator' WHERE id = :id"),
            {"id": uid})
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    result = CliRunner().invoke(cli, ["users", "promote",
                                       "already-op@example.com"])
    # No-op path: exit 0, no state change, no duplicate audit row.
    assert result.exit_code == 0, result.output
    assert await _role_of(pg_engine, uid) == "operator"


async def test_unknown_email_fails_cleanly(pg_engine, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    result = CliRunner().invoke(cli, ["users", "promote",
                                       "ghost@example.com"])
    assert result.exit_code == 1
    assert "No account with email" in result.output


async def test_local_owner_role_is_immutable(pg_engine, monkeypatch):
    async with pg_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO users (email, is_system, role, created_at)"
            " VALUES (:e, TRUE, 'operator', 1.0)"
            " ON CONFLICT (email) DO NOTHING"),
            {"e": LOCAL_OWNER_EMAIL})
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    for verb in ("demote", "promote"):
        result = CliRunner().invoke(cli, ["users", verb, LOCAL_OWNER_EMAIL])
        assert result.exit_code == 1, (verb, result.output)
        assert "cannot be changed" in result.output


async def test_list_shows_accounts_and_roles(pg_engine, monkeypatch):
    await _seed_plain_user(pg_engine, "listed@example.com")
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    result = CliRunner().invoke(cli, ["users", "list"])
    assert result.exit_code == 0, result.output
    assert "listed@example.com  user" in result.output


def test_users_group_registered():
    """The group is wired into the top-level CLI help."""
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "users" in result.output
