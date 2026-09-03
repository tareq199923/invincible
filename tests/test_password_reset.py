# tests/test_password_reset.py
"""Operator-side password recovery (the `invincible users
reset-password` CLI and its UserService core).

Design premise: a self-hosted gateway has no email infrastructure, so
the operator IS the recovery mechanism and database access is the proof
of authority. Pinned behaviors: the old password stops authenticating,
the new one works, every pre-reset browser cookie dies (session_version
bump), inv_ keys survive (separate realm), and the CLI surfaces the
same semantics end-to-end.
"""
import re

from click.testing import CliRunner

from invincible.cli import cli
from invincible.core.accounts import UserService
from invincible.main import app
from tests.conftest import TEST_DB_URL, register_account


async def _login(client, email, password):
    return await client.post(
        "/auth/login", json={"email": email, "password": password})


async def test_reset_password_replaces_credentials(client):
    await register_account(client, "reset@example.com")
    service = UserService(app.state.engine)

    user = await service.get_by_email("reset@example.com")
    await service.reset_password(user["id"], "new-long-password")

    old = await _login(client, "reset@example.com", "longenough1")
    assert old.status_code == 401

    new = await _login(client, "reset@example.com", "new-long-password")
    assert new.status_code == 200


async def test_reset_password_kills_existing_session_cookies(client):
    await register_account(client, "locked@example.com")
    # A live browser session for the account...
    me = await client.get("/auth/me")
    assert me.status_code == 200

    engine = app.state.engine
    service = UserService(engine)
    user = await service.get_by_email("locked@example.com")
    await service.reset_password(user["id"], "replacement-pass")

    # ...no longer resolves after the reset (version bump).
    me = await client.get("/auth/me")
    assert me.status_code == 401


async def test_reset_password_leaves_inv_keys_alone(client):
    from invincible.core.identity import ApiKeyStore

    made, _ = await register_account(client, "keyed@example.com")
    engine = app.state.engine
    record = await ApiKeyStore(engine).create(made.json()["id"],
                                              label="survivor")

    service = UserService(engine)
    user = await service.get_by_email("keyed@example.com")
    await service.reset_password(user["id"], "another-pass-1")

    resolved = await ApiKeyStore(engine).resolve(record["raw"])
    assert resolved is not None  # password realm never touches keys


async def test_reset_password_unknown_user(client):
    import pytest

    from invincible.core.accounts import AccountError

    service = UserService(app.state.engine)

    with pytest.raises(AccountError):
        await service.reset_password(999_999, "whatever-pass")


async def test_cli_reset_password_end_to_end(client, monkeypatch):
    """The real operator flow: register through HTTP, reset through the
    Click CLI against the same database, log in with the generated
    password."""
    await register_account(client, "cli@example.com")
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["users", "reset-password", "cli@example.com", "--generate"])
    assert result.exit_code == 0, result.output
    assert "Password reset for cli@example.com" in result.output

    match = re.search(r"New password \(shown once\): (\S+)", result.output)
    assert match is not None
    generated = match.group(1)

    # Old password is dead, the generated one logs in.
    old = await _login(client, "cli@example.com", "longenough1")
    assert old.status_code == 401
    new = await _login(client, "cli@example.com", generated)
    assert new.status_code == 200


async def test_cli_reset_password_unknown_email(client, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["users", "reset-password", "ghost@example.com", "--generate"])
    assert result.exit_code != 0
    assert "No account with email" in result.output
