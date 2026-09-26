# tests/test_dashboard_mcp.py
"""Phase 3: the /dashboard/mcp MCP-grants page (Q1: /mcp stays OAuth-only).

Gates: session-only page; the page lists OAuth clients this principal
owns with live active-token counts; revoking a client's tokens is
ownership-predicated (foreign, unknown, and legacy-era clients are
identical 404s) and actually kills the bearer's /mcp access; the page
documents the OAuth-only posture (no inv_ key acceptance).
"""
import re

from sqlalchemy import text

from invincible.core.oauth_store import OAuthStore
from invincible.main import app
from tests.conftest import (
    oauth_register,
    obtain_access_token,
    register_account,
)


async def logged_in(client, seq):
    registered, _ = await register_account(
        client, f"mcp-ui-{seq}@example.com")
    assert registered.status_code == 201, registered.text
    return registered.json()["id"]


async def test_page_requires_session(client):
    assert (await client.get("/dashboard/mcp")).status_code == 401


async def test_page_empty_state_with_connect_hint(client):
    await logged_in(client, 1)
    page = await client.get("/dashboard/mcp")
    assert page.status_code == 200
    assert "No MCP clients registered yet" in page.text
    # One-line connect hint with the server's own /mcp URL.
    assert "/mcp" in page.text
    assert "OAuth 2.1" in page.text
    # The long OAuth walkthrough is gone; the posture stays documented
    # in docs/MCP_PROTOCOL.md and docs/SECURITY.md.
    assert "Connecting a client" not in page.text
    assert "Registered clients" not in page.text


async def test_page_lists_client_and_active_tokens(client):
    await logged_in(client, 2)
    await obtain_access_token(client)  # registers client + mints a token

    page = await client.get("/dashboard/mcp")
    assert page.status_code == 200
    assert "test-client" in page.text
    # obtain_access_token mints an access AND a refresh token.
    assert re.search(r"<td>\s*2\s*</td>", page.text)


async def test_htmx_revocation_kills_mcp_bearer(client):
    await logged_in(client, 3)
    tokens = await obtain_access_token(client)
    client_id = tokens["client_id"]

    page = await client.get("/dashboard/mcp")
    assert f'hx-delete="/dashboard/mcp/clients/{client_id}/tokens"' in page.text

    htmx = await client.delete(
        f"/dashboard/mcp/clients/{client_id}/tokens",
        headers={"HX-Request": "true"})
    # Bare 204: hx-swap="delete" drops the row in place, no redirect.
    assert htmx.status_code == 204
    assert "HX-Redirect" not in htmx.headers
    assert ">Revoke</button>" in (await client.get("/dashboard/mcp")).text

    # The revoked bearer no longer opens /mcp.
    denied = await client.post("/mcp", headers={
        "Authorization": f"Bearer {tokens['access_token']}"},
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
    assert denied.status_code == 401

    page = await client.get("/dashboard/mcp")
    assert re.search(r"<td>\s*0\s*</td>", page.text)  # no active tokens


async def test_foreign_client_hidden_and_revocation_is_predicated(client):
    owner_a = await logged_in(client, 4)
    # User B registers a client and owns it (approved under B). B's
    # registration runs on a separate client so A's session cookie
    # survives.
    import httpx

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test") as other:
        registered_b, _ = await register_account(other, "mcp-ui-b@example.com")
    uid_b = registered_b.json()["id"]
    assert uid_b != owner_a
    client_id_b, _ = await oauth_register(client, name="b-client")
    await OAuthStore(app.state.engine).attach_owner(client_id_b, uid_b)

    page = await client.get("/dashboard/mcp")
    assert "b-client" not in page.text  # foreign clients are invisible

    foreign = await client.delete(
        f"/dashboard/mcp/clients/{client_id_b}/tokens")
    unknown = await client.delete(
        "/dashboard/mcp/clients/does-not-exist/tokens")
    for resp in (foreign, unknown):
        assert resp.status_code == 404
    assert foreign.json() == unknown.json()  # identical anti-enumeration


# --- legacy-era clients are nobody-manageable (HIGH-2 regression) ----------


async def _seed_legacy_clients() -> tuple[str, str]:
    """One client owned by the system local owner, one unowned row -
    both shapes predate Phase-5 ownership. Returns (owned_id,
    unowned_id)."""
    from invincible.core.db import ensure_local_owner

    engine = app.state.engine
    local_uid, _ = await ensure_local_owner(engine)
    async with engine.begin() as conn:
        owned_id = (await conn.execute(text(
            "INSERT INTO oauth_clients (client_id, client_name,"
            " redirect_uris, owner_user_id, created_at)"
            " VALUES ('legacy-owned', 'Legacy Owned',"
            " '[\"http://localhost:9/cb\"]', :u, 1.0) RETURNING client_id"
        ), {"u": local_uid})).scalar_one()
        unowned_id = (await conn.execute(text(
            "INSERT INTO oauth_clients (client_id, client_name,"
            " redirect_uris, owner_user_id, created_at)"
            " VALUES ('legacy-unowned', 'Legacy Unowned',"
            " '[\"http://localhost:9/cb\"]', NULL, 1.0) RETURNING client_id"
        ))).scalar_one()
    return owned_id, unowned_id


async def test_legacy_clients_are_nobody_manageable(client):
    """HIGH-2 (audit 2026-09-07) + Phase 2: the local-owner-era pools
    (the dormant system row's clients + unowned rows) are invisible and
    404-shaped for every user - there is no operator escape hatch
    anymore. The rows are harmless dormants, rendered for no one."""
    await logged_in(client, 5)
    owned_id, unowned_id = await _seed_legacy_clients()

    page = await client.get("/dashboard/mcp")
    assert page.status_code == 200
    assert "Legacy Owned" not in page.text
    assert "Legacy Unowned" not in page.text

    for client_id in (owned_id, unowned_id):
        denied = await client.delete(
            f"/dashboard/mcp/clients/{client_id}/tokens")
        unknown = await client.delete(
            "/dashboard/mcp/clients/does-not-exist/tokens")
        assert denied.status_code == 404
        assert denied.json() == unknown.json()  # anti-enumeration


async def test_user_still_manages_own_client(client):
    """No over-tightening: every user keeps full control of the client
    they own (listing + revoke)."""
    import httpx

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test") as plain:
        registered, _ = await register_account(plain,
                                               "mcp-plain-own@example.com")
        assert registered.status_code == 201
        client_id, _ = await oauth_register(plain, name="plain-client")
        await OAuthStore(app.state.engine).attach_owner(
            client_id, registered.json()["id"])

        page = await plain.get("/dashboard/mcp")
        assert page.status_code == 200
        assert "plain-client" in page.text

        revoked = await plain.delete(
            f"/dashboard/mcp/clients/{client_id}/tokens")
        assert revoked.status_code == 200
        assert revoked.json()["revoked"] >= 0


async def test_user_cannot_revoke_another_users_client(client):
    """Ownership stops at exactly your own clients: another USER's owned
    client stays invisible and 404-shaped."""
    await logged_in(client, 6)
    # A row for a distinct third-party user (raw SQL: no auth surface
    # needed, just a real owner_user_id that is not the caller).
    async with app.state.engine.begin() as conn:
        uid_c = (await conn.execute(text(
            "INSERT INTO users (email, created_at)"
            " VALUES ('mcp-third@example.com', 1.0) RETURNING id"
        ))).scalar_one()
    client_id_b, _ = await oauth_register(client, name="op-foreign-client")
    await OAuthStore(app.state.engine).attach_owner(client_id_b, int(uid_c))

    page = await client.get("/dashboard/mcp")
    assert "op-foreign-client" not in page.text
    denied = await client.delete(
        f"/dashboard/mcp/clients/{client_id_b}/tokens")
    assert denied.status_code == 404
