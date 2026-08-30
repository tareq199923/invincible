# tests/test_oauth_consent_subject.py
"""Phase 5 (Q3: yes) + the operator gate: OAuth consent stamps the
dashboard user - but only an operator-role account may approve.

Gates:

- A valid ``invincible_session`` cookie for an OPERATOR account opens the
  consent page (no owner-secret login step) and shows the approving
  identity; approving issues codes/tokens whose subject is THAT user
  (proved end-to-end: an MCP write under that bearer lands on a session
  row owned by the dashboard user).
- A plain self-registered account is refused with 403 on both GET and
  POST - registration alone must never mint host-shell MCP tokens. The
  refusal is audited (``oauth.consent_forbidden``), and no code exists.
- Signature-valid cookies whose ``session_version`` no longer matches
  (password changed) or whose user row is gone are treated like
  forgeries: login page on GET, 401 on POST.
- The owner-secret flow still resolves to the system *local* owner, and
  a browser holding both cookies approves as the dashboard user.
"""
import json
from urllib.parse import parse_qs, urlparse

from sqlalchemy import text

from invincible.core.db import LOCAL_OWNER_EMAIL
from invincible.core.oauth_store import token_hash
from invincible.main import app
from tests.conftest import (
    authorize_params,
    oauth_approve,
    oauth_exchange,
    oauth_login,
    oauth_register,
    pkce_pair,
    promote_operator,
    register_account,
)


async def _flow_as_session_user(client, email, promote=True):
    """Register a dashboard account, register an OAuth client, and return
    (uid, authorize params) - the session cookie is already set."""
    registered, _ = await register_account(client, email)
    assert registered.status_code == 201, registered.text
    uid = registered.json()["id"]
    if promote:
        await promote_operator(uid)
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    return uid, params, verifier, client_id, redirect_uri


async def _token_subject(access_token: str) -> int | None:
    """The user a bearer token acts as (oauth_tokens stores hashes only)."""
    async with app.state.engine.connect() as conn:
        return (await conn.execute(
            text("SELECT subject_user_id FROM oauth_tokens "
                 "WHERE token_hash = :h"),
            {"h": token_hash(access_token)},
        )).scalar()


async def _scalar(sql: str, **params):
    async with app.state.engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def test_session_cookie_opens_consent_without_owner_login(client):
    uid, params, _, _, _ = await _flow_as_session_user(
        client, "consent-a@example.com")

    page = await client.get("/oauth/authorize",
                            params={k: v for k, v in params.items()})
    assert page.status_code == 200
    assert "Approving as" in page.text
    assert "consent-a@example.com" in page.text
    # No owner-secret login step: the session cookie IS the login.
    assert "Owner secret" not in page.text


async def test_no_cookies_still_shows_owner_login(client):
    _, params, _, _, _ = await _flow_as_session_user(
        client, "consent-b@example.com")
    # Strip the session cookie: anonymous browser sees the login form.
    client.cookies.delete("invincible_session")
    page = await client.get("/oauth/authorize",
                            params={k: v for k, v in params.items()})
    assert page.status_code == 200
    assert "Owner secret" in page.text


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
    access_token = exchange.json()["access_token"]

    # The token acts as the dashboard user: an MCP write under it lands
    # on a session row owned by THAT user id.
    bearer = {"Authorization": f"Bearer {access_token}",
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


async def test_plain_user_session_is_refused(client):
    """Self-registration alone must never reach host-shell tokens: a
    valid session on a non-operator account gets 403 on both the consent
    page and the approve POST, with an audit row and zero codes issued."""
    uid, params, _, _, _ = await _flow_as_session_user(
        client, "plain-a@example.com", promote=False)

    page = await client.get("/oauth/authorize",
                            params={k: v for k, v in params.items()})
    assert page.status_code == 403
    assert "not permitted" in page.text
    assert "Approving as" not in page.text

    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 403

    assert await _scalar("SELECT COUNT(*) FROM oauth_codes") == 0
    assert await _scalar(
        "SELECT actor_user_id FROM audit_log "
        "WHERE action = 'oauth.consent_forbidden'") == uid


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
                            params={k: v for k, v in params.items()})
    assert page.status_code == 200
    assert "Owner secret" in page.text  # login page, not the consent page
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
                            params={k: v for k, v in params.items()})
    assert page.status_code == 200
    assert "Owner secret" in page.text
    assert f"user #{uid}" not in page.text

    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 401


async def test_owner_secret_flow_stamps_local_owner(client):
    """The pre-Phase-5 headless path: owner-secret login + approval
    resolves to the system local owner (now an operator by seed/0008)."""
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    login = await oauth_login(client, params)
    assert login.status_code == 302, login.text[:300]
    location = await oauth_approve(client, params)
    code = parse_qs(urlparse(location).query)["code"][0]
    exchange = await oauth_exchange(client, code, client_id, redirect_uri,
                                    verifier)
    assert exchange.status_code == 200, exchange.text

    owner_id = await _scalar(
        "SELECT id FROM users WHERE email = :e", e=LOCAL_OWNER_EMAIL)
    assert await _token_subject(
        exchange.json()["access_token"]) == owner_id


async def test_both_cookies_session_identity_wins(client):
    """A browser holding both invincible_owner and a (operator-role)
    invincible_session cookie approves AS the dashboard user."""
    uid, params, verifier, client_id, redirect_uri = (
        await _flow_as_session_user(client, "both-a@example.com"))
    login = await oauth_login(client, params)  # adds the owner cookie
    assert login.status_code == 302, login.text[:300]

    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False)
    assert approved.status_code == 302, approved.text[:300]
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
    exchange = await oauth_exchange(client, code, client_id, redirect_uri,
                                    verifier)
    assert exchange.status_code == 200, exchange.text
    assert await _token_subject(exchange.json()["access_token"]) == uid
