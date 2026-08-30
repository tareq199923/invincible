# tests/test_oauth_consent_subject.py
"""Phase 5 (Q3: yes): OAuth consent stamps the dashboard user.

Gates: a valid ``invincible_session`` cookie alone opens the consent page
(no owner-secret login step) and shows the approving identity; approving
issues codes/tokens whose subject is THAT user (proved end-to-end: an
MCP task_state_set under that bearer lands on a session row owned by the
dashboard user); the owner-secret flow is unchanged; no cookies still
shows the login form.
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
