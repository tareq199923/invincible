# tests/test_oauth_consent_subject.py
"""Phase 2: OAuth consent is self-service - any logged-in account
approves its own clients, and the token subject is THAT user.

Gates:

- A valid ``invincible_session`` cookie opens the consent page (there is
  no other login step) and shows the approving identity; approving
  issues codes/tokens whose subject is THAT user (proved end-to-end: an
  MCP write under that bearer lands on a session row owned by the
  dashboard user).
- A plain self-registered account - not the first human on the instance
  - approves fine; there is no operator role anymore.
- Signature-valid cookies whose ``session_version`` no longer matches
  (password changed) or whose user row is gone are treated like
  forgeries: redirect to /login on GET, 401 on POST.
"""
import json
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


async def _flow_as_session_user(client, email):
    """Register a dashboard account, register an OAuth client, and return
    (uid, authorize params) - the session cookie is already set."""
    registered, _ = await register_account(client, email)
    assert registered.status_code == 201, registered.text
    uid = registered.json()["id"]
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    return uid, params, verifier, client_id, redirect_uri


async def _seed_prior_human(email="prior-human@example.com") -> None:
    """Make the instance 'already inhabited' so the NEXT registration is
    a plain non-first human. Raw SQL on purpose."""
    async with app.state.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (email, created_at)"
                 " VALUES (:e, 1.0)"),
            {"e": email},
        )


async def _token_subject(access_token: str) -> int | None:
    """The user a bearer token acts as (oauth_tokens stores hashes only)."""
    from invincible.core.oauth_store import token_hash

    async with app.state.engine.connect() as conn:
        return (await conn.execute(
            text("SELECT subject_user_id FROM oauth_tokens "
                 "WHERE token_hash = :h"),
            {"h": token_hash(access_token)},
        )).scalar()


async def _scalar(sql: str, **params):
    async with app.state.engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def test_session_cookie_opens_consent_without_login(client):
    uid, params, _, _, _ = await _flow_as_session_user(
        client, "consent-a@example.com")

    page = await client.get("/oauth/authorize",
                            params={k: v for k, v in params.items()})
    assert page.status_code == 200
    assert "Approving as" in page.text
    assert "consent-a@example.com" in page.text
    # No owner-secret login step exists anywhere on the page.
    assert "Owner secret" not in page.text


async def test_consent_stamps_the_dashboard_user_subject(client):
    uid, params, verifier, client_id, redirect_uri = (
        await _flow_as_session_user(client, "consent-c@example.com"))

    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 302, approved.text[:300]
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]

    exchange = await oauth_exchange(
        client, code, client_id, redirect_uri, verifier)
    assert exchange.status_code == 200, exchange.text
    assert await _token_subject(
        exchange.json()["access_token"]) == uid

    # The token acts as the dashboard user: an MCP write under it lands
    # on a session row owned by THAT user id.
    bearer = {"Authorization": f"Bearer {exchange.json()['access_token']}",
              "Content-Type": "application/json"}
    call = await client.post("/mcp", headers=bearer, content=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "task_state_set", "arguments": {
            "task_key": "subject-check", "session_id": "subject-check",
            "payload": "{\"v\": 1}"}},
    }))
    assert call.status_code == 200, call.text

    async with app.state.engine.connect() as conn:
        owner = (await conn.execute(text(
            "SELECT user_id FROM sessions "
            "WHERE client_session_id = 'subject-check'"))).scalar()
    assert owner == uid


async def test_plain_user_session_can_approve(client):
    """Self-service consent: a plain self-registered account (not the
    first human) approves its own client and becomes the token subject."""
    await _seed_prior_human()
    uid, params, verifier, client_id, redirect_uri = (
        await _flow_as_session_user(client, "plain-a@example.com"))

    page = await client.get("/oauth/authorize",
                            params={k: v for k, v in params.items()})
    assert page.status_code == 200
    assert "Approving as" in page.text

    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 302, approved.text[:300]
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
    exchange = await oauth_exchange(
        client, code, client_id, redirect_uri, verifier)
    assert exchange.status_code == 200, exchange.text
    assert await _token_subject(
        exchange.json()["access_token"]) == uid


async def test_version_mismatched_cookie_is_rejected(client):
    """A cookie orphaned by a password change (session_version bumped)
    is treated exactly like a forged one - never as a login (limit 14)."""
    uid, params, _, _, _ = await _flow_as_session_user(
        client, "stale-a@example.com")
    async with app.state.engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET session_version = session_version + 1 "
                 "WHERE id = :id"),
            {"id": uid},
        )

    page = await client.get("/oauth/authorize",
                            params={k: v for k, v in params.items()},
                            follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["location"].startswith("/login?next=")
    assert "Approving as" not in page.text

    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 401
    assert await _scalar("SELECT COUNT(*) FROM oauth_codes") == 0


async def test_deleted_user_cookie_is_rejected(client):
    """A session cookie for a deleted account must not open (or approve)
    consent - not even fall back to a 'user #N' identity."""
    uid, params, _, _, _ = await _flow_as_session_user(
        client, "stale-b@example.com")
    async with app.state.engine.begin() as conn:
        # FK order: audit rows and the default project reference the user.
        await conn.execute(
            text("DELETE FROM audit_log WHERE actor_user_id = :id"),
            {"id": uid},
        )
        await conn.execute(
            text("DELETE FROM projects WHERE user_id = :id"), {"id": uid})
        await conn.execute(
            text("DELETE FROM users WHERE id = :id"), {"id": uid})

    page = await client.get("/oauth/authorize",
                            params={k: v for k, v in params.items()},
                            follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["location"].startswith("/login?next=")
    assert f"user #{uid}" not in page.text

    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 401


async def test_two_users_each_own_their_clients(client):
    """Isolation: user A's consent does not leak to user B - the client
    row and token subjects stay per-account."""
    import httpx

    uid_a, params_a, verifier_a, client_id_a, redirect_uri = (
        await _flow_as_session_user(client, "pair-a@example.com"))
    approved = await client.post(
        "/oauth/authorize",
        data={**params_a, "action": "approve"},
        follow_redirects=False)
    code_a = parse_qs(
        urlparse(approved.headers["location"]).query)["code"][0]

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test") as other:
        uid_b, params_b, verifier_b, client_id_b, _ = (
            await _flow_as_session_user(other, "pair-b@example.com"))
        approved_b = await other.post(
            "/oauth/authorize",
            data={**params_b, "action": "approve"},
            follow_redirects=False)
        assert approved_b.status_code == 302

    assert uid_a != uid_b
    exchange_a = await oauth_exchange(
        client, code_a, client_id_a, redirect_uri, verifier_a)
    assert await _token_subject(
        exchange_a.json()["access_token"]) == uid_a

    async with app.state.engine.connect() as conn:
        owner_b = (await conn.execute(text(
            "SELECT owner_user_id FROM oauth_clients "
            "WHERE client_id = :c"), {"c": client_id_b})).scalar()
    assert owner_b == uid_b
