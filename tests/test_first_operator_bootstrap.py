# tests/test_first_operator_bootstrap.py
"""First-human bootstrap: on opted-in deployments the first registered
account on a fresh instance becomes an operator.

The self-hosted model is one person per instance - the person who ran
``inv setup`` must be able to govern their own machine (approve MCP
clients) without a terminal step. MEDIUM-1 (2026-09-07 audit) made the
bootstrap opt-in: it fires when no owner secret is configured, or when
INVINCIBLE_ALLOW_FIRST_OPERATOR=1 (the ``client`` fixture sets the flag,
mirroring what setup writes into fresh .env files). Gates:

- first password registration -> operator (audited with the role);
- first GitHub registration -> operator too (same _insert path);
- any later registration -> plain user;
- the bootstrap only fires when NO earlier non-system human exists;
- with the flag absent and a secret set, first registration is a PLAIN
  user (the hosted-deploy posture - a stranger winning the race gets
  nothing);
- with no secret configured at all, the bootstrap still fires (bare
  self-host preserved);
- end-to-end: the first user can register a client, approve it, and
  receive tokens stamped with their own subject - no promote step.
"""
from urllib.parse import parse_qs, urlparse

from sqlalchemy import text

from invincible.main import app
from tests.conftest import (
    authorize_params,
    oauth_exchange,
    oauth_register,
    pkce_pair,
    register_account,
)


async def _role_of(uid: int) -> str:
    async with app.state.engine.connect() as conn:
        return (await conn.execute(
            text("SELECT role FROM users WHERE id = :id"),
            {"id": uid})).scalar()


async def _seed_prior_human(email="prior@example.com") -> int:
    """Raw-SQL human row; bypasses the bootstrap on purpose."""
    async with app.state.engine.begin() as conn:
        return (await conn.execute(text(
            "INSERT INTO users (email, created_at)"
            " VALUES (:e, 1.0) RETURNING id"),
            {"e": email})).scalar_one()


async def test_first_password_registration_is_operator(client):
    registered, _ = await register_account(client, "first@example.com")
    assert registered.status_code == 201, registered.text
    uid = registered.json()["id"]
    assert await _role_of(uid) == "operator"
    # The grant is visible in the audit trail.
    async with app.state.engine.connect() as conn:
        meta = (await conn.execute(text(
            "SELECT meta FROM audit_log WHERE action = 'auth.registered'"
        ))).scalar()
    assert meta["role"] == "operator"


async def test_second_registration_is_plain_user(client):
    first, _ = await register_account(client, "first@example.com")
    second, _ = await register_account(client, "second@example.com")
    assert await _role_of(first.json()["id"]) == "operator"
    assert await _role_of(second.json()["id"]) == "user"


async def test_inhabited_instance_never_bootstraps(client):
    """Any pre-existing human suppresses the bootstrap: the registrant is
    joining someone else's instance, not claiming a fresh one."""
    prior = await _seed_prior_human()
    registered, _ = await register_account(client, "joiner@example.com")
    assert await _role_of(prior) == "user"
    assert await _role_of(registered.json()["id"]) == "user"


async def test_secret_set_flag_absent_first_registration_is_plain_user(
    client, monkeypatch
):
    """MEDIUM-1 regression: the hosted-deploy posture. With an owner
    secret configured and the flag absent, a stranger winning the
    registration race on a fresh instance lands a plain user account -
    never operator (approval of MCP clients / provider management stays
    out of reach; elevation is `invincible users promote`)."""
    monkeypatch.delenv("INVINCIBLE_ALLOW_FIRST_OPERATOR", raising=False)
    registered, _ = await register_account(client, "race@example.com")
    assert registered.status_code == 201, registered.text
    uid = registered.json()["id"]
    assert await _role_of(uid) == "user"
    async with app.state.engine.connect() as conn:
        meta = (await conn.execute(
            text("SELECT meta FROM audit_log WHERE action = 'auth.registered'")
        )).scalar()
    assert meta["role"] == "user"
    # The gate is the flag, not order-of-registration: a SECOND account
    # still stays user, and enabling the flag late changes nothing for
    # accounts already registered (no retroactive promotion, none
    # expected - only the first-human-on-fresh-instance path applies).
    second, _ = await register_account(client, "second@example.com")
    assert await _role_of(second.json()["id"]) == "user"


async def test_no_secret_bootstraps_without_the_flag(
    client, monkeypatch
):
    """Bare self-host: no owner secret configured (and no flag) still
    bootstraps - a deployment without the browser-session surface is
    unambiguously single-person, so the terminal-free bootstrap is
    safe and preserved. Exercised at the service layer: with no secret
    the HTTP register endpoint 503s (sessions disabled), which is the
    same posture a bare self-host would face."""
    monkeypatch.delenv("INVINCIBLE_ALLOW_FIRST_OPERATOR", raising=False)
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    from invincible.core.accounts import UserService

    user = await UserService(app.state.engine).register(
        "bare@example.com", "longenough1")
    assert user["role"] == "operator"


async def test_github_first_registration_is_operator(pg_engine, monkeypatch):
    from invincible.core.accounts import UserService

    # The flag gate applies to the GitHub path too (same _insert).
    monkeypatch.setenv("INVINCIBLE_ALLOW_FIRST_OPERATOR", "1")
    service = UserService(pg_engine)
    user = await service.register_without_password("gh-first@example.com")
    assert user["role"] == "operator"
    second = await service.register("pw-second@example.com", "longenough1")
    assert second["role"] == "user"


async def test_first_user_approves_without_any_promotion(client):
    """The bump, fixed end to end: fresh instance, register, connect an
    MCP client - approval works with zero terminal steps, and the token
    acts as that first user."""
    registered, _ = await register_account(client, "founder@example.com")
    uid = registered.json()["id"]

    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)

    page = await client.get("/oauth/authorize",
                            params={k: v for k, v in params.items()})
    assert page.status_code == 200
    assert "Approving as" in page.text
    assert "founder@example.com" in page.text

    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 302, approved.text[:300]
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]

    exchange = await oauth_exchange(client, code, client_id, redirect_uri,
                                    verifier)
    assert exchange.status_code == 200, exchange.text

    from invincible.core.oauth_store import token_hash

    async with app.state.engine.connect() as conn:
        subject = (await conn.execute(text(
            "SELECT subject_user_id FROM oauth_tokens "
            "WHERE token_hash = :h"),
            {"h": token_hash(exchange.json()["access_token"])})).scalar()
    assert subject == uid
