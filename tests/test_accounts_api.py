# tests/test_accounts_api.py
"""Phase 3 account surface over HTTP.

The acceptance walk (register -> login -> project -> key -> chat ->
revoke -> 401), realm separation (cookies never open /v1/*; MCP bearers
and the gateway key never manage accounts), enumeration-safe login,
scoped lockouts, ownership predicates across users, the full device
pairing flow issuing working credentials, and the GitHub login flow
against a mocked GitHub.
"""
import secrets

import httpx
import pytest

from invincible.core.accounts import GitHubOAuth
from invincible.main import app
from tests.conftest import (
    login_account,
    provider_body,
    register_account,
)

PASSWORD = "longenough1"


def _fresh_client():
    """A second browser (clean cookie jar) against the same app."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


def _github_handler(tokens=None):
    """MockTransport emulating github.com + api.github.com for one login."""
    issued = tokens or {"gho_test": {"id": 555, "login": "octcat",
                                     "email": "gh@example.com",
                                     "emails": [
                                         {"email": "gh@example.com",
                                          "primary": True,
                                          "verified": True}]}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            import json

            body = json.loads(request.read().decode())
            if not body.get("code"):
                return httpx.Response(
                    200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": "gho_test"})
        if request.url.path == "/user":
            info = issued["gho_test"]
            return httpx.Response(200, json={
                "id": info["id"], "login": info["login"],
                "email": info["email"]})
        if request.url.path == "/user/emails":
            info = issued["gho_test"]
            return httpx.Response(200, json=info["emails"])
        return httpx.Response(500)

    return handler


@pytest.fixture
def github_enabled(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("INVINCIBLE_GITHUB_CLIENT_SECRET", "test-secret")
    transports = []

    def _install(mapping):
        GitHubOAuth.default_transport = httpx.MockTransport(
            _github_handler(mapping))
        transports.append(GitHubOAuth.default_transport)
        return GitHubOAuth.default_transport

    yield _install
    GitHubOAuth.default_transport = None


async def _me(client):
    return await client.get("/auth/me")


# --- acceptance walk ---------------------------------------------------------------


async def test_full_acceptance_walk(client, router_setter):
    # 1. register (auto-login)
    made = await client.post(
        "/auth/register", json={"email": "walk@example.com",
                                "password": PASSWORD})
    assert made.status_code == 201, made.text
    assert made.json()["project_id"] > 0

    # 2. explicit login from a clean cookie jar
    fresh = _fresh_client()
    try:
        logged = await login_account(fresh, "walk@example.com", PASSWORD)
        assert logged.status_code == 200, logged.text
        assert "invincible_session" in fresh.cookies

        me = await fresh.get("/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == "walk@example.com"
        assert me.json()["kind"] == "session"

        # 3. project CRUD
        project = await fresh.post("/projects", json={"name": "Client Work"})
        assert project.status_code == 201, project.text
        listing = await fresh.get("/projects")
        names = [p["name"] for p in listing.json()["projects"]]
        assert "personal" in names and "Client Work" in names

        # 4. mint an API key (raw shown exactly once)
        created_key = await fresh.post("/api-keys", json={"label": "cli"})
        assert created_key.status_code == 201
        raw_key = created_key.json()["raw"]

        # 5. chat with the key through the tiered failover router
        router_setter({
            "alpha.example.com": httpx.Response(
                200, json=provider_body("alpha", "hi from alpha")),
        })
        chat = await fresh.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"model": "alpha-model",
                  "messages": [{"role": "user", "content": "hello"}]},
        )
        assert chat.status_code == 200, chat.text
        assert chat.json()["choices"][0]["message"]["content"] == \
            "hi from alpha"

        # the chat landed under this user's session listing
        sessions = await fresh.get("/sessions")
        assert len(sessions.json()["sessions"]) == 1

        # 6. revoke -> the key stops working immediately
        key_id = created_key.json()["id"]
        revoked = await fresh.delete(f"/api-keys/{key_id}")
        assert revoked.status_code == 200 and revoked.json()["revoked"] is True
        dead = await fresh.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"model": "alpha-model",
                  "messages": [{"role": "user", "content": "again"}]},
        )
        assert dead.status_code == 401
    finally:
        await fresh.aclose()


# --- registration & login ----------------------------------------------------------


async def test_register_validation_and_duplicates(client):
    ok = await register_account(client)
    assert ok[0].status_code == 201
    dup = await register_account(client)
    assert dup[0].status_code == 409
    assert dup[0].json()["error"]["code"] == "duplicate_email"

    weak = await client.post("/auth/register",
                             json={"email": "w@e.com", "password": "short"})
    assert weak.status_code == 400
    bad_email = await client.post("/auth/register",
                                  json={"email": "nope", "password": PASSWORD})
    assert bad_email.status_code == 400


async def test_login_is_enumeration_safe_and_locks_out(client):
    await register_account(client, "known@example.com")

    unknown = await client.post(
        "/auth/login", json={"email": "ghost@example.com",
                             "password": "wrongpassword"})
    wrong = await client.post(
        "/auth/login", json={"email": "known@example.com",
                             "password": "wrongpassword"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()

    for _ in range(5):
        await client.post("/auth/login", json={
            "email": "known@example.com", "password": "wrongpassword"})
    locked = await client.post(
        "/auth/login", json={"email": "known@example.com",
                             "password": PASSWORD})
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "locked_out"


async def test_logout_clears_session(client):
    await register_account(client, "bye@example.com")
    assert (await _me(client)).status_code == 200
    out = await client.post("/auth/logout")
    assert out.status_code == 200
    assert (await _me(client)).status_code == 401


async def test_me_requires_session(client):
    assert (await _me(client)).status_code == 401


# --- projects ------------------------------------------------------------------------


async def test_project_surface_ownership_and_archive(client):
    await register_account(client, "alice@example.com")
    default_listing = (await client.get("/projects")).json()["projects"]
    personal = next(p for p in default_listing if p["is_default"])

    # the default project cannot be archived
    denied = await client.post(f"/projects/{personal['id']}/archive")
    assert denied.status_code == 409

    made = await client.post("/projects", json={"name": "Archive Me"})
    renamed = await client.request(
        "PATCH", f"/projects/{made.json()['id']}",
        json={"name": "Renamed"})
    assert renamed.status_code == 200
    archived = await client.post(f"/projects/{made.json()['id']}/archive")
    assert archived.status_code == 200
    visible = (await client.get("/projects")).json()["projects"]
    assert all(p["name"] != "Renamed" for p in visible)
    everything = (
        await client.get("/projects?include_archived=true")).json()["projects"]
    assert any(p["name"] == "Renamed" and p["archived_at"]
               for p in everything)


async def test_projects_never_leak_across_users(client, router_setter):
    await register_account(client, "p1@example.com")
    made = await client.post("/projects", json={"name": "Alice Only"})
    alice_project = made.json()["id"]

    # brand-new cookie jar = user two
    other = _fresh_client()
    try:
        await register_account(other, "p2@example.com")
        archive = await other.post(f"/projects/{alice_project}/archive")
        assert archive.status_code == 404
        rename = await other.patch(
            f"/projects/{alice_project}", json={"name": "stolen"})
        assert rename.status_code == 404
        names = [p["name"] for p in
                 (await other.get("/projects")).json()["projects"]]
        assert "Alice Only" not in names
    finally:
        await other.aclose()


# --- API keys & realms ----------------------------------------------------------------


async def test_api_keys_list_never_shows_raw(client):
    await register_account(client, "keys@example.com")
    made = await client.post("/api-keys", json={"label": "one"})
    raw = made.json()["raw"]
    listed = (await client.get("/api-keys")).json()["api_keys"]
    assert len(listed) == 1
    assert listed[0]["prefix"] in raw
    assert all(k.get("raw") is None for k in listed)


async def test_mcp_bearer_cannot_manage_api_keys(client, bearer_headers):
    """No session cookie anywhere in this jar - the MCP bearer alone must
    not manage accounts."""
    anon = _fresh_client()
    try:
        response = await anon.post("/api-keys", json={},
                                   headers=bearer_headers)
        assert response.status_code == 401
    finally:
        await anon.aclose()


async def test_gateway_key_cannot_manage_accounts(client):
    response = await client.post(
        "/api-keys", json={}, headers={
            "Authorization": "Bearer test-gateway-key"})
    assert response.status_code == 401


async def test_api_keys_revoke_scoped_to_owner(client, router_setter):
    await register_account(client, "k1@example.com")
    made = await client.post("/api-keys", json={})
    key_id = made.json()["id"]

    other = _fresh_client()
    try:
        await register_account(other, "k2@example.com")
        steal = await other.delete(f"/api-keys/{key_id}")
        assert steal.status_code == 404
    finally:
        await other.aclose()
    mine = await client.delete(f"/api-keys/{key_id}")
    assert mine.json()["revoked"] is True


async def test_browser_cookie_never_authorizes_chat(client, router_setter):
    """Realm separation: a live session cookie must be worthless on /v1/*."""
    await register_account(client, "cookie@example.com")
    router_setter({})
    chat = await client.post(
        "/v1/chat/completions",
        json={"model": "alpha-model",
              "messages": [{"role": "user", "content": "hello"}]},
    )
    assert chat.status_code == 401


# --- device pairing -------------------------------------------------------------


async def test_device_flow_issues_working_credentials(
    client, router_setter, monkeypatch
):
    monkeypatch.setattr("invincible.endpoints.accounts.DEFAULT_POLL_INTERVAL", 0)
    await register_account(client, "dev@device.example")

    started = await client.post("/auth/device/code")
    assert started.status_code == 200, started.text
    payload = started.json()
    assert len(payload["user_code"]) == 8
    assert payload["device_code"] and payload["expires_in"] > 0

    # polling before approval stays pending
    poll_form = {"grant_type":
                 "urn:ietf:params:oauth:grant-type:device_code",
                 "device_code": payload["device_code"]}
    pending = await client.post("/auth/device/token", data=poll_form)
    assert pending.status_code == 400
    assert pending.json()["error"] == "authorization_pending"

    # approval requires a logged-in browser session; here we are logged
    # in, so the page renders with the Approve/Deny forms.
    page = await client.get(f"/auth/devices/{payload['user_code']}")
    assert page.status_code == 200 and "Approve" in page.text

    approved = await client.post(
        f"/auth/devices/{payload['user_code']}/approve")
    assert approved.status_code == 200

    done = await client.post("/auth/device/token", data=poll_form)
    assert done.status_code == 200, done.text
    raw_key = done.json()["access_token"]
    assert raw_key.startswith("inv_")

    router_setter({
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha")),
    })
    chat = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "alpha-model",
              "messages": [{"role": "user", "content": "paired!"}]},
    )
    assert chat.status_code == 200

    # single-winner claim: replaying the grant yields nothing
    replay = await client.post("/auth/device/token", data=poll_form)
    assert replay.status_code == 400
    assert replay.json()["error"] == "expired_token"


async def test_device_deny_path(client, monkeypatch):
    monkeypatch.setattr("invincible.endpoints.accounts.DEFAULT_POLL_INTERVAL", 0)
    await register_account(client, "deny@device.example")
    payload = (await client.post("/auth/device/code")).json()
    await client.post(f"/auth/devices/{payload['user_code']}/deny")
    result = await client.post(
        "/auth/device/token",
        data={"device_code": payload["device_code"],
              "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    assert result.status_code == 403
    assert result.json()["error"] == "access_denied"


async def test_device_approval_requires_login(client, monkeypatch):
    monkeypatch.setattr("invincible.endpoints.accounts.DEFAULT_POLL_INTERVAL", 0)
    payload = (await client.post("/auth/device/code")).json()

    anon = _fresh_client()
    try:
        page = await anon.get(f"/auth/devices/{payload['user_code']}")
        assert page.status_code == 401
        approve = await anon.post(
            f"/auth/devices/{payload['user_code']}/approve")
        assert approve.status_code == 401
        # nothing was granted
        result = await anon.post(
            "/auth/device/token",
            data={"device_code": payload["device_code"],
                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        assert result.json()["error"] == "authorization_pending"
    finally:
        await anon.aclose()


# --- GitHub login -------------------------------------------------------------


def _extract_state(location: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(location).query)["state"][0]


async def test_github_register_new_account(client, github_enabled):
    github_enabled(None)
    login_redirect = await client.get("/auth/github/login",
                                      follow_redirects=False)
    assert login_redirect.status_code == 302
    assert login_redirect.headers["location"].startswith(
        "https://github.com/login/oauth/authorize")
    state = _extract_state(login_redirect.headers["location"])

    callback = await client.get(
        f"/auth/github/callback?code=good-code&state={state}",
        follow_redirects=True)
    assert callback.status_code == 200

    me = await _me(client)
    assert me.status_code == 200
    assert me.json()["email"] == "gh@example.com"


async def test_github_links_existing_verified_email(client, github_enabled):
    await register_account(client, "gh@example.com")
    original_id = (await _me(client)).json()["id"]
    await client.post("/auth/logout")

    github_enabled(None)
    redirect = await client.get("/auth/github/login", follow_redirects=False)
    state = _extract_state(redirect.headers["location"])
    await client.get(
        f"/auth/github/callback?code=good-code&state={state}",
        follow_redirects=True)

    me = await _me(client)
    assert me.status_code == 200
    # linked onto the EXISTING account, not a second user
    assert me.json()["id"] == original_id


async def test_github_rejects_unverified_email_only(client, github_enabled):
    mapping = {"gho_test": {"id": 777, "login": "shady",
                            "email": None,
                            "emails": [
                                {"email": "shady@example.com",
                                 "primary": True, "verified": False}]}}
    github_enabled(mapping)
    redirect = await client.get("/auth/github/login", follow_redirects=False)
    state = _extract_state(redirect.headers["location"])
    callback = await client.get(
        f"/auth/github/callback?code=good-code&state={state}",
        follow_redirects=False)
    assert callback.headers["location"].startswith("/login?github_error=1")
    assert (await _me(client)).status_code == 401


async def test_github_state_mismatch_rejected(client, github_enabled):
    github_enabled(None)
    await client.get("/auth/github/login", follow_redirects=False)
    forged = await client.get(
        f"/auth/github/callback?code=good-code&state={secrets.token_urlsafe(16)}",
        follow_redirects=False)
    assert forged.headers["location"].startswith("/login?github_error=1")
    assert (await _me(client)).status_code == 401


async def test_github_identity_conflict_rejected(client, github_enabled):
    """Once an account owns GitHub identity X, identity Y claiming the same
    verified email must never attach silently."""
    github_enabled(None)  # links gh id 555 -> gh@example.com
    redirect = await client.get("/auth/github/login", follow_redirects=False)
    state = _extract_state(redirect.headers["location"])
    await client.get(
        f"/auth/github/callback?code=good-code&state={state}")

    # a SECOND GitHub account re-using the same verified email
    conflicting = {
        "gho_test": {"id": 999, "login": "imposter",
                     "email": "gh@example.com",
                     "emails": [{"email": "gh@example.com",
                                 "primary": True, "verified": True}]}}
    GitHubOAuth.default_transport = httpx.MockTransport(_github_handler(conflicting))
    try:
        redirect2 = await client.get("/auth/github/login",
                                     follow_redirects=False)
        state2 = _extract_state(redirect2.headers["location"])
        denied = await client.get(
            f"/auth/github/callback?code=good-code&state={state2}",
            follow_redirects=False)
        assert denied.headers["location"].startswith("/login?github_error=1")
        me = await _me(client)
        # still the ORIGINAL user (id 555's account), not the imposter
        assert me.json()["email"] == "gh@example.com"
    finally:
        GitHubOAuth.default_transport = None


async def test_github_disabled_without_credentials(client, monkeypatch):
    monkeypatch.delenv("INVINCIBLE_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("INVINCIBLE_GITHUB_CLIENT_SECRET", raising=False)
    response = await client.get("/auth/github/login")
    assert response.status_code == 503
