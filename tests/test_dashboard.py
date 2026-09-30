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


def nav_inner(html: str, href: str) -> str:
    """The inside of one sidebar nav link.

    The 2026-09-30 icon pass put an inline svg before each label and
    wrapped the label in a span, so assertions that care about a link's
    content slice it out here. The href is part of the match: the link
    target stays the contract.
    """
    match = re.search(
        rf'<a class="nav-link[^"]*"\s+href="{re.escape(href)}">(.*?)</a>',
        html, re.DOTALL)
    assert match is not None, f"nav link for {href} missing from sidebar"
    return match.group(1)


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


async def test_dashboard_home_redirects_to_chat(client):
    await register_account(client, "empty@example.com")
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/chat"
    page = await client.get("/dashboard/chat")
    assert page.status_code == 200
    assert "/static/app.css" in page.text
    # The + is an inline icon now (2026-09-30 sidebar icons): the icon
    # and the words both have to be there for the button to read alike.
    assert "icon-plus" in page.text
    assert '<span class="nav-label">New chat</span>' in page.text
    assert 'id="side-history"' in page.text


async def test_sidebar_has_no_section_headings_but_keeps_every_link(client):
    """The sidebar groups by whitespace only (2026-09-30): the Workspace /
    Monitor / Account headings are gone, but not a single nav link is."""
    await register_account(client, "chrome@example.com")
    page = (await client.get("/dashboard/chat")).text
    assert page.count('class="side-section-label"') == 1  # only Chats remains
    for label in ("Workspace", "Monitor"):
        assert label not in page, label
    # the link stays - only the label's home moved (it is a span now,
    # sitting after the link's icon).
    assert nav_inner(page, "/account").endswith(
        '<span class="nav-label">Account</span>')
    for link in ("/dashboard/setup", "/dashboard/providers", "/dashboard/mcp",
                 "/dashboard/machines", "/dashboard/memory",
                 "/dashboard/tasks", "/dashboard/sessions",
                 "/dashboard/usage", "/dashboard/settings", "/account"):
        assert f'href="{link}"' in page, link
    # The per-chat ⋮ menu ships with the shell (empty list renders the
    # empty state instead, so the hooks are asserted on a seeded page).
    assert 'placeholder="Search chats…"' in page
    assert "/static/sidebar.js" in page


SIDEBAR_ICONS = [
    ("/dashboard/setup", "Get started", "play-circle"),
    ("/dashboard/providers", "Providers", "plug"),
    ("/dashboard/mcp", "MCP", "wrench"),
    ("/dashboard/machines", "Machines", "monitor"),
    ("/dashboard/memory", "Memories", "database"),
    ("/dashboard/tasks", "Tasks", "square-check"),
    ("/dashboard/sessions", "Sessions", "message-square"),
    ("/dashboard/usage", "Usage", "bar-chart"),
    ("/dashboard/settings", "Settings", "settings"),
    ("/account", "Account", "user"),
]


async def test_sidebar_renders_an_icon_for_every_nav_item(client):
    """Claude-style sidebar: a thin line icon to the left of every label.

    Pins what matters about the icons: each is one inline svg INSIDE the
    link, decorative and theme-blind (currentColor, aria-hidden, not
    focusable, 24x24 drawn at 18px), and never reused; the labels read
    exactly as before; and every href survived the icon pass.
    """
    await register_account(client, "iconnav@example.com")
    page = (await client.get("/dashboard/chat")).text
    names = [name for _, _, name in SIDEBAR_ICONS]
    assert len(set(names)) == len(names), "no icon may be used twice"
    for href, label, name in SIDEBAR_ICONS:
        inner = nav_inner(page, href)
        assert inner.count("<svg") == 1, href
        assert f'class="icon icon-{name}"' in inner, name
        for attr in ('viewBox="0 0 24 24"', 'width="18"', 'height="18"',
                     'fill="none"', 'stroke="currentColor"',
                     'stroke-width="1.75"', 'aria-hidden="true"',
                     'focusable="false"'):
            assert attr in inner, (name, attr)
        assert inner.endswith(f'<span class="nav-label">{label}</span>'), label


async def test_new_chat_button_has_a_plus_icon_and_keeps_its_label(client):
    """The literal '+' character became a plus icon; the words and the form
    target are untouched, and no stray '+' glyph is left behind."""
    await register_account(client, "plusnav@example.com")
    page = (await client.get("/dashboard/chat")).text
    match = re.search(
        r'<button[^>]*class="new-chat-btn"[^>]*>(.*?)</button>',
        page, re.DOTALL)
    assert match is not None, "New chat button missing from the sidebar"
    inner = match.group(1)
    assert inner.count("<svg") == 1
    assert 'class="icon icon-plus"' in inner
    assert 'aria-hidden="true"' in inner
    assert '<span class="nav-label">New chat</span>' in inner
    assert "+ New chat" not in page               # the old text glyph is gone
    assert 'action="/dashboard/chat/new"' in page  # still posts to new chat


async def test_dashboard_renders_empty_state(client):
    made, _ = await register_account(client, "empty@example.com")
    assert made.status_code == 201
    page = await client.get("/dashboard/overview")
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

    page = await client.get("/dashboard/overview")
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
    own = await client.get("/dashboard/overview")
    assert "private-alpha" in own.text

    # Registering user B replaces the session cookie; B's dashboard must
    # show none of A's rows.
    await register_account(client, "other@example.com")
    theirs = await client.get("/dashboard/overview")
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
    page = await client.get("/dashboard/overview")
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
    assert "model_providers.invincible" not in page.text
    # The dashboard carries the matching first-run signpost.
    overview = await client.get("/dashboard/overview")
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
    # The Codex snippet is the config.toml approach (the env-var recipe
    # alone doesn't configure the Codex CLI) and pre-fills the user's
    # first connected model.
    assert 'wire_api = "responses"' in page.text
    assert 'model = "alpha-model"' in page.text
    # T0-1: copy buttons carry their text in data-copy (no
    # regex-on-innerText, which copied the button label with the config).
    assert page.text.count('data-copy="export ANTHROPIC_BASE_URL=') == 1
    assert page.text.count('data-copy="OPENAI_BASE_URL=') == 1
    assert page.text.count('data-copy="model = &quot;alpha-model&quot;') == 1
    assert "innerText" not in page.text
    # The RAW key never renders on this page - only Account shows it once.
    assert key["raw"] not in page.text
    # And the dashboard signpost is gone once setup is complete.
    overview = await client.get("/dashboard/overview")
    assert "You're 2 steps away" not in overview.text


async def test_setup_signpost_clears_with_key_only(client):
    """A key without a provider still cannot chat - the signpost stays."""
    await register_account(client, "half@example.com")
    await client.post("/api-keys", json={"label": "only-key"})
    overview = await client.get("/dashboard/overview")
    assert "You're 2 steps away" in overview.text
