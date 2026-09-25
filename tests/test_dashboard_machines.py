# tests/test_dashboard_machines.py
"""H6c: the /dashboard/machines page — cookie realm only, per-user
inventory, empty state. Follows the test_dashboard_mcp.py patterns."""
from invincible.main import app
from tests.conftest import register_account


async def logged_in(client, seq):
    registered, _ = await register_account(
        client, f"machines-{seq}@example.com")
    assert registered.status_code == 201, registered.text
    return registered.json()["id"]


async def test_page_requires_session(client):
    assert (await client.get("/dashboard/machines")).status_code == 401


async def test_page_rejects_inv_keys(client):
    """inv_ API keys never reach the dashboard (realm separation)."""
    from invincible.endpoints.accounts import SESSION_COOKIE
    from tests.test_agent_endpoints import _mint_key, agent_headers

    _, key = await _mint_key(client)
    client.cookies.delete(SESSION_COOKIE)  # key alone, no session cookie
    resp = await client.get("/dashboard/machines",
                            headers=agent_headers(key))
    assert resp.status_code == 401


async def test_page_empty_state(client):
    await logged_in(client, 1)
    page = await client.get("/dashboard/machines")
    assert page.status_code == 200
    assert "No machines seen yet" in page.text
    assert "harness setup" in page.text


async def test_page_lists_own_machines(client):
    uid = await logged_in(client, 2)
    app.state.agent_registry.update_machine(uid, "m-1", {
        "machine_name": "laptop", "platform": "win",
        "capabilities": {"chrome": True, "docker": False},
    })
    page = await client.get("/dashboard/machines")
    assert page.status_code == 200
    assert "laptop" in page.text
    assert "m-1" in page.text
    assert "chrome" in page.text
    assert "online" in page.text


async def test_page_isolated_per_user(client):
    uid_a = await logged_in(client, 3)
    app.state.agent_registry.update_machine(uid_a, "m-a", {
        "machine_name": "alice-pc", "platform": "x",
        "capabilities": {}})
    mine = await client.get("/dashboard/machines")
    assert "alice-pc" in mine.text
    # Registering user B replaces the session cookie; B sees nothing.
    await logged_in(client, 4)
    theirs = await client.get("/dashboard/machines")
    assert "alice-pc" not in theirs.text
    assert "No machines seen yet" in theirs.text
