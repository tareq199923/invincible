# tests/test_dashboard_mcp.py
"""Phase 3: the /dashboard/mcp MCP-grants page (Q1: /mcp stays OAuth-only).

Gates: session-only page; the page lists OAuth clients this principal
may manage (own + unowned) with live active-token counts; revoking a
client's tokens is ownership-predicated (foreign and unknown 404s are
identical) and actually kills the bearer's /mcp access; the page
documents the OAuth-only posture (no inv_ key acceptance).
"""
import re

from invincible.core.oauth_store import OAuthStore
from invincible.main import app
from tests.conftest import (
    oauth_register,
    obtain_access_token,
    promote_operator,
    register_account,
)


async def logged_in(client, seq):
    registered, _ = await register_account(
        client, f"mcp-ui-{seq}@example.com")
    assert registered.status_code == 201, registered.text
    uid = registered.json()["id"]
    # This page manages MCP clients, so its user is an operator (the
    # role the consent flow requires); plain accounts get 403 there.
    await promote_operator(uid)
    return uid


async def test_page_requires_session(client):
    assert (await client.get("/dashboard/mcp")).status_code == 401


async def test_page_empty_state_documents_oauth_only(client):
    await logged_in(client, 1)
    page = await client.get("/dashboard/mcp")
    assert page.status_code == 200
    assert "No MCP clients registered yet" in page.text
    # Q1 posture is documented on the page itself.
    assert "not" in page.text and "inv_" in page.text
    assert "OAuth 2.1" in page.text


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
    assert htmx.status_code == 204
    assert (htmx.headers["HX-Redirect"] == "/dashboard/mcp?revoked=1")

    # The revoked bearer no longer opens /mcp.
    denied = await client.post("/mcp", headers={
        "Authorization": f"Bearer {tokens['access_token']}"},
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
    assert denied.status_code == 401

    page = await client.get("/dashboard/mcp?revoked=1")
    assert "All tokens for that client were revoked" in page.text
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
