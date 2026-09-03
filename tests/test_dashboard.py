# tests/test_dashboard.py
"""Phase 5 PR-5A: /dashboard overview page.

Covers the cookie-realm gate (anonymous 401), the vendored HTMX asset,
empty states, count-card math over seeded rows (projects, sessions,
revoked-key exclusion), and cross-user invisibility of the
recent-sessions table.
"""
import re

from invincible.main import app
from tests.conftest import register_account


def card_count(html: str, name: str) -> int:
    match = re.search(
        rf'data-card="{name}"><span class="num">(\d+)<', html)
    assert match is not None, f"card {name} missing from page"
    return int(match.group(1))


async def test_dashboard_requires_session(client):
    anon = await client.get("/dashboard")
    assert anon.status_code == 401


async def test_vendored_htmx_asset_served(client):
    resp = await client.get("/static/htmx.min.js")
    assert resp.status_code == 200
    assert b"2.0.4" in resp.content


async def test_base_template_links_htmx_for_all_pages(client):
    page = await client.get("/login")
    assert "/static/htmx.min.js" in page.text


async def test_dashboard_renders_empty_state(client):
    made, _ = await register_account(client, "empty@example.com")
    assert made.status_code == 201
    page = await client.get("/dashboard")
    assert page.status_code == 200
    assert "empty@example.com" in page.text
    assert "No sessions yet." in page.text
    # Default project exists at registration; nothing else seeded.
    assert card_count(page.text, "projects") == 1
    assert card_count(page.text, "sessions") == 0
    assert card_count(page.text, "api-keys") == 0


async def test_dashboard_counts_seeded_rows(client):
    made, _ = await register_account(client, "seeded@example.com")
    body = made.json()
    uid, pid = body["id"], body["project_id"]
    store = app.state.sessions
    await store.append("sess-alpha",
                       [{"role": "user", "content": "hi"}],
                       user_id=uid, project_id=pid)
    await store.append("sess-beta",
                       [{"role": "user", "content": "yo"}],
                       user_id=uid, project_id=pid)
    assert (await client.post(
        "/projects", json={"name": "side"})).status_code == 201
    first_key = (await client.post(
        "/api-keys", json={"label": "cli"})).json()
    second_key = (await client.post(
        "/api-keys", json={"label": "tmp"})).json()
    revoke = await client.delete(f"/api-keys/{second_key['id']}")
    assert revoke.status_code == 200
    assert revoke.json()["revoked"] is True

    page = await client.get("/dashboard")
    assert page.status_code == 200
    assert card_count(page.text, "projects") == 2
    assert card_count(page.text, "sessions") == 2
    # Revoked keys never inflate the active count.
    assert card_count(page.text, "api-keys") == 1
    assert "sess-alpha" in page.text
    assert "sess-beta" in page.text
    assert first_key["prefix"] not in page.text  # raw prefixes never render


async def test_dashboard_isolated_per_user(client):
    made_a, _ = await register_account(client, "owner@example.com")
    body_a = made_a.json()
    await app.state.sessions.append(
        "private-alpha", [{"role": "user", "content": "secret"}],
        user_id=body_a["id"], project_id=body_a["project_id"])
    own = await client.get("/dashboard")
    assert "private-alpha" in own.text

    # Registering user B replaces the session cookie; B's dashboard must
    # show none of A's rows.
    await register_account(client, "other@example.com")
    theirs = await client.get("/dashboard")
    assert theirs.status_code == 200
    assert "private-alpha" not in theirs.text
    assert card_count(theirs.text, "sessions") == 0


async def test_recent_sessions_cap_at_ten(client):
    made, _ = await register_account(client, "many@example.com")
    body = made.json()
    store = app.state.sessions
    for i in range(12):
        await store.append(f"s-{i}", [{"role": "user", "content": "x"}],
                           user_id=body["id"], project_id=body["project_id"])
    page = await client.get("/dashboard")
    assert card_count(page.text, "sessions") == 12
    assert page.text.count("client-session-row") == 10


# --- guided setup (Omniroute-style onboarding) ---------------------------------


async def test_setup_page_requires_session(client):
    anon = await client.get("/dashboard/setup")
    assert anon.status_code == 401


async def test_setup_page_shows_both_steps_pending(client):
    await register_account(client, "fresh@example.com")
    page = await client.get("/dashboard/setup")
    assert page.status_code == 200
    assert "Connect a provider" in page.text
    assert "Create your API key" in page.text
    # No key yet -> the copy-paste config block is withheld.
    assert "ANTHROPIC_BASE_URL" not in page.text
    # The dashboard carries the matching first-run signpost.
    overview = await client.get("/dashboard")
    assert "You're 2 steps away" in overview.text


async def test_setup_page_unlocks_config_once_key_exists(
    client, monkeypatch
):
    from cryptography.fernet import Fernet
    import invincible.core.url_safety as url_safety
    from invincible.core.credential_store import ByokCredentialStore

    monkeypatch.setenv(
        "INVINCIBLE_CREDENTIAL_KEY",
        Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(
        url_safety, "_default_resolve", lambda host: ["93.184.216.34"])

    made, _ = await register_account(client, "ready@example.com")
    uid = made.json()["id"]
    await ByokCredentialStore(app.state.engine).create(
        user_id=uid, provider_name="Test Pool",
        model_id="alpha-model",
        base_url="https://alpha.example.com/v1",
        api_key="user-key")

    key = (await client.post("/api-keys", json={"label": "setup"})).json()
    page = await client.get("/dashboard/setup")
    assert page.status_code == 200
    # Both steps done -> config blocks render, completion banner shows.
    assert "You're all set" in page.text
    assert "ANTHROPIC_BASE_URL" in page.text
    assert "OPENAI_BASE_URL" in page.text
    assert "/mcp" in page.text
    # The RAW key never renders on this page - only Account shows it once.
    assert key["raw"] not in page.text
    # And the dashboard signpost is gone once setup is complete.
    overview = await client.get("/dashboard")
    assert "You're 2 steps away" not in overview.text


async def test_setup_signpost_clears_with_key_only(client):
    """A key without a provider still cannot chat - the signpost stays."""
    await register_account(client, "half@example.com")
    await client.post("/api-keys", json={"label": "only-key"})
    overview = await client.get("/dashboard")
    assert "You're 2 steps away" in overview.text
