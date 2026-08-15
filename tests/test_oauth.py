"""Tests for the self-hosted OAuth 2.1 + PKCE authorization server.

Covers: RFC 8414 / RFC 9728 discovery metadata, RFC 7591 dynamic client
registration, the owner-login cookie gate on /oauth/authorize, the full
authorize -> approve -> token exchange with real PKCE verification, single-use
codes, refresh-token rotation, revocation, and the /mcp bearer-token gate.
"""
import re
from urllib.parse import parse_qs, urlparse

from invincible.core.oauth_store import OAuthStore
from invincible.main import app
from tests.conftest import (
    TEST_REDIRECT_URI,
    authorize_params,
    oauth_approve,
    oauth_exchange,
    oauth_login,
    oauth_register,
    obtain_access_token,
    pkce_pair,
)

# --- discovery metadata ---


async def test_authorization_server_metadata_shape(client):
    response = await client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    data = response.json()
    assert data["issuer"] == "http://test"
    assert data["authorization_endpoint"] == "http://test/oauth/authorize"
    assert data["token_endpoint"] == "http://test/oauth/token"
    assert data["registration_endpoint"] == "http://test/oauth/register"
    assert data["revocation_endpoint"] == "http://test/oauth/revoke"
    assert data["response_types_supported"] == ["code"]
    assert data["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert data["code_challenge_methods_supported"] == ["S256"]
    assert data["token_endpoint_auth_methods_supported"] == ["none"]
    assert data["revocation_endpoint_auth_methods_supported"] == ["none"]


async def test_protected_resource_metadata_shape(client):
    response = await client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    data = response.json()
    assert data["resource"] == "http://test/mcp"
    assert data["canonical_uri"] == "http://test/mcp"
    assert data["authorization_servers"] == ["http://test"]


# --- dynamic client registration ---


async def test_register_creates_public_client(client):
    client_id, redirect_uri = await oauth_register(
        client, name="claude-connector"
    )
    assert client_id
    assert redirect_uri == TEST_REDIRECT_URI
    store: OAuthStore = app.state.oauth_store
    stored = await store.get_client(client_id)
    assert stored is not None
    assert stored["client_name"] == "claude-connector"
    assert stored["redirect_uris"] == [TEST_REDIRECT_URI]


async def test_register_rejects_missing_redirect_uris(client):
    response = await client.post("/oauth/register", json={})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


async def test_register_rejects_non_https_non_loopback_uri(client):
    response = await client.post(
        "/oauth/register",
        json={
            "redirect_uris": ["http://evil.example.com/callback"],
            "client_name": "x",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


async def test_register_rejects_non_object_body(client):
    response = await client.post("/oauth/register", json=[1, 2])
    assert response.status_code == 400


# --- owner-login gate on /oauth/authorize ---


async def test_authorize_without_session_cookie_shows_login_not_consent(client):
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    response = await client.get(
        f"/oauth/authorize?{'&'.join(f'{k}={v}' for k, v in params.items())}"
    )
    assert response.status_code == 200
    text = response.text
    assert "Owner secret" in text and 'name="owner_secret"' in text
    assert "Approve" not in text


async def test_wrong_owner_secret_sets_no_cookie(client):
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    response = await oauth_login(client, params, owner_secret="wrong-secret")
    assert response.status_code == 401
    assert "Incorrect owner secret" in response.text
    assert "set-cookie" not in response.headers


async def test_correct_owner_secret_sets_session_cookie(client):
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    response = await oauth_login(client, params)
    assert response.status_code == 302
    cookie = response.headers.get("set-cookie", "")
    assert "invincible_owner=" in cookie
    assert "HttpOnly" in cookie


async def test_owner_secret_unset_blocks_login(client, monkeypatch):
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    response = await oauth_login(client, params)
    assert response.status_code == 503
    assert "No owner secret is configured" in response.text


async def test_legacy_mcp_shared_secret_still_authenticates(client, monkeypatch):
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    monkeypatch.setenv("MCP_SHARED_SECRET", "legacy-secret")
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    response = await oauth_login(client, params, owner_secret="legacy-secret")
    assert response.status_code == 302


async def test_authorize_rejects_unregistered_client(client):
    verifier, challenge = pkce_pair()
    params = authorize_params("not-a-real-client", challenge)
    response = await client.get(
        f"/oauth/authorize?{'&'.join(f'{k}={v}' for k, v in params.items())}"
    )
    assert response.status_code == 400
    assert "Invalid or unregistered" in response.text


async def test_authorize_rejects_mismatched_redirect_uri(client):
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(
        client, redirect_uri="http://localhost:9999/callback"
    )
    params = authorize_params(client_id, challenge, redirect_uri)
    params["redirect_uri"] = "http://localhost:9999/other"
    response = await client.get(
        f"/oauth/authorize?{'&'.join(f'{k}={v}' for k, v in params.items())}"
    )
    assert response.status_code == 400
    assert "Invalid or unregistered" in response.text


# --- full PKCE flow ---


def _code_from_location(location):
    return parse_qs(urlparse(location).query)["code"][0]


async def test_full_flow_with_correct_verifier_succeeds(client):
    tokens = await obtain_access_token(client)
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    store: OAuthStore = app.state.oauth_store
    info = await store.validate_access(tokens["access_token"])
    assert info is not None
    assert info["client_id"] == tokens["client_id"]


async def test_wrong_verifier_rejected(client):
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    await oauth_login(client, params)
    location = await oauth_approve(client, params)
    code = _code_from_location(location)
    wrong_verifier, _ = pkce_pair()
    response = await oauth_exchange(
        client, code, client_id, redirect_uri, wrong_verifier
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_code_is_single_use(client):
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    await oauth_login(client, params)
    location = await oauth_approve(client, params)
    code = _code_from_location(location)
    first = await oauth_exchange(client, code, client_id, redirect_uri, verifier)
    assert first.status_code == 200
    second = await oauth_exchange(client, code, client_id, redirect_uri, verifier)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


async def test_expired_code_rejected(client):
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    store: OAuthStore = app.state.oauth_store
    code = await store.create_code(client_id, redirect_uri, challenge, ttl=0)
    response = await oauth_exchange(client, code, client_id, redirect_uri, verifier)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_deny_redirects_with_access_denied(client):
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    await oauth_login(client, params)
    location = await oauth_approve(client, params, deny=True)
    query = parse_qs(urlparse(location).query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["xyz-state"]


# --- consent page link construction (regression: doubled /oauth/authorize) ---


async def _consent_page_hrefs(client, params):
    """GET the rendered consent page and return its (approve, deny) hrefs."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = await client.get(f"/oauth/authorize?{query}")
    assert response.status_code == 200, response.text[:300]
    links = re.findall(r'<a href="([^"]+)"', response.text)
    assert len(links) == 2, links
    approve = next(link for link in links if "action=approve" in link)
    deny = next(link for link in links if "action=deny" in link)
    return approve, deny


async def test_consent_page_hrefs_contain_single_authorize_path(client):
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    await oauth_login(client, params)
    approve_href, deny_href = await _consent_page_hrefs(client, params)
    assert approve_href.count("/oauth/authorize?") == 1, approve_href
    assert deny_href.count("/oauth/authorize?") == 1, deny_href


async def test_live_approve_follows_rendered_href(client):
    """Follow the literal Approve link from the rendered HTML - the exact
    click the browser (e.g. the Claude app connector) would make."""
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    await oauth_login(client, params)
    approve_href, _ = await _consent_page_hrefs(client, params)
    response = await client.get(approve_href, follow_redirects=False)
    assert response.status_code == 302, response.text[:300]
    location = response.headers["location"]
    assert not location.startswith("/oauth/authorize"), location
    assert location.startswith(redirect_uri), location
    query = parse_qs(urlparse(location).query)
    assert query["code"], location
    assert query["state"] == ["xyz-state"]
    exchange = await oauth_exchange(
        client, query["code"][0], client_id, redirect_uri, verifier
    )
    assert exchange.status_code == 200, exchange.text


async def test_live_deny_follows_rendered_href(client):
    verifier, challenge = pkce_pair()
    client_id, redirect_uri = await oauth_register(client)
    params = authorize_params(client_id, challenge, redirect_uri)
    await oauth_login(client, params)
    _, deny_href = await _consent_page_hrefs(client, params)
    response = await client.get(deny_href, follow_redirects=False)
    assert response.status_code == 302, response.text[:300]
    location = response.headers["location"]
    assert not location.startswith("/oauth/authorize"), location
    assert location.startswith(redirect_uri), location
    query = parse_qs(urlparse(location).query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["xyz-state"]


# --- refresh tokens ---


async def test_refresh_rotates_and_invalidates_old(client):
    tokens = await obtain_access_token(client)
    old_refresh = tokens["refresh_token"]

    response = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": old_refresh},
    )
    assert response.status_code == 200
    rotated = response.json()
    assert rotated["refresh_token"] != old_refresh
    assert rotated["access_token"] != tokens["access_token"]

    replay = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": old_refresh},
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


# --- revocation ---


async def test_revoke_access_token_invalidates_it(client):
    tokens = await obtain_access_token(client)
    store: OAuthStore = app.state.oauth_store
    assert await store.revoke(tokens["access_token"]) is True
    assert await store.validate_access(tokens["access_token"]) is None
    response = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401


async def test_revoke_refresh_token_invalidates_it(client):
    tokens = await obtain_access_token(client)
    store: OAuthStore = app.state.oauth_store
    assert await store.revoke(tokens["refresh_token"]) is True
    response = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_revoke_unknown_token_still_200(client):
    response = await client.post("/oauth/revoke", data={"token": "not-a-token"})
    assert response.status_code == 200


# --- /mcp bearer gate ---


async def test_mcp_without_bearer_returns_401_with_www_authenticate(client):
    response = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert "Bearer" in challenge
    assert "/.well-known/oauth-protected-resource" in challenge


async def test_mcp_with_garbage_bearer_returns_401(client):
    response = await client.post(
        "/mcp",
        headers={"Authorization": "Bearer not-a-real-token"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401
    assert "oauth-protected-resource" in response.headers.get("www-authenticate", "")
