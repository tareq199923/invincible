# tests/test_dashboard_mcp.py
"""Phase 3: the /dashboard/mcp MCP-grants page (Q1: /mcp stays OAuth-only).

Gates: session-only page; the page lists OAuth clients this principal
may manage (own + unowned) with live active-token counts; revoking a
client's tokens is ownership-predicated (foreign and unknown 404s are
identical) and actually kills the bearer's /mcp access; the page
documents the OAuth-only posture (no inv_ key acceptance).
"""
import re

from sqlalchemy import text

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


# --- operator-gated legacy pools (HIGH-2 regression) -------------------------------


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


async def test_plain_user_cannot_see_or_revoke_legacy_clients(client):
    """HIGH-2 (audit 2026-09-07): any dashboard user used to see and
    revoke the local owner's / unowned MCP clients. Plain users now get
    the 404-shaped denial, identical to an unknown client."""
    from tests.conftest import register_account

    # A deliberately NON-operator account. Fresh-table first
    # registrations bootstrap as operator (MEDIUM-1 pattern), so demote
    # explicitly to assert the plain-user behavior.
    registered, _ = await register_account(client, "mcp-plain@example.com")
    assert registered.status_code == 201
    demote_uid = registered.json()["id"]
    async with app.state.engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET role = 'user' WHERE id = :id"),
            {"id": demote_uid},
        )
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


async def test_operator_still_manages_legacy_clients(client):
    """The operator escape hatch survives the fix: legacy pools (local
    owner's + unowned) stay listed and revocable from an operator
    session."""
    await logged_in(client, 5)  # registers + promotes to operator
    owned_id, unowned_id = await _seed_legacy_clients()

    page = await client.get("/dashboard/mcp")
    assert page.status_code == 200
    assert "Legacy Owned" in page.text
    assert "Legacy Unowned" in page.text

    for client_id in (owned_id, unowned_id):
        resp = await client.delete(
            f"/dashboard/mcp/clients/{client_id}/tokens")
        assert resp.status_code == 200
        assert resp.json()["revoked"] >= 0


async def test_plain_user_still_manages_own_client(client):
    """No over-tightening: a plain user keeps full control of the client
    they own (listing + revoke)."""
    import httpx

    # Plain user registers and owns a client; demote the first-human
    # bootstrap so this really is a non-operator session.
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test") as plain:
        registered, _ = await register_account(plain,
                                               "mcp-plain-own@example.com")
        assert registered.status_code == 201
        async with app.state.engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET role = 'user' WHERE id = :id"),
                {"id": registered.json()["id"]},
            )
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


async def test_operator_cannot_revoke_another_users_client(client):
    """Operator privilege stops at the legacy pools: another USER's
    owned client stays invisible and 404-shaped even for operators."""
    await logged_in(client, 6)
    # A row for a distinct third-party user (raw SQL: no auth surface
    # needed, just a real owner_user_id that is neither the operator
    # nor the local owner).
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
