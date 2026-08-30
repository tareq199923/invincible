# tests/test_account_keys_ui.py
"""Phase 2: API-key generate/revoke UI on /account.

Gates: session-only page; the browser form POST renders the page with
the raw key shown EXACTLY ONCE (never in a URL, never on a later
render); JSON POST keeps the 201-record wire shape; Revoke uses the
HTMX 204 row delete and is idempotent; revoked keys render without a
revoke button.
"""
import re
from itertools import count

import httpx

from invincible.main import app
from tests.conftest import register_account

_email_seq = count(1)


async def logged_in(client):
    registered, _ = await register_account(
        client, f"keys-ui-{next(_email_seq)}@example.com")
    assert registered.status_code == 201, registered.text


async def test_page_has_generate_form_and_empty_state(client):
    await logged_in(client)
    page = await client.get("/account")
    assert page.status_code == 200
    assert 'action="/api-keys"' in page.text
    assert 'name="label"' in page.text
    assert "Generate API key" in page.text
    assert "No API keys yet" in page.text


async def test_form_generate_shows_raw_key_exactly_once(client):
    await logged_in(client)
    made = await client.post("/api-keys", data={"label": "from the UI"})
    assert made.status_code == 200
    assert "Copy your new API key now" in made.text
    # Extract the raw key from the one-time banner.
    match = re.search(r'new-key-raw">([^<]+)</code>', made.text)
    assert match is not None, "raw key missing from the one-time banner"
    raw = match.group(1)
    assert raw.startswith("inv_")
    assert "from the UI" in made.text

    # A fresh render never shows the raw key again.
    page = await client.get("/account")
    assert raw not in page.text
    assert "from the UI" in page.text
    assert "revoke-key" in page.text
    assert 'hx-delete="/api-keys/' in page.text


async def test_json_generate_keeps_201_record_shape(client):
    await logged_in(client)
    made = await client.post("/api-keys", json={"label": "cli"})
    assert made.status_code == 201
    record = made.json()
    assert record["raw"].startswith("inv_")
    assert record["label"] == "cli"
    assert "<html" not in made.text.lower()


async def test_htmx_revoke_removes_row_and_is_idempotent(client):
    await logged_in(client)
    made = await client.post("/api-keys", json={"label": "tmp"})
    key_id = made.json()["id"]
    raw = made.json()["raw"]

    page = await client.get("/account")
    assert f'hx-delete="/api-keys/{key_id}"' in page.text

    htmx = await client.delete(
        f"/api-keys/{key_id}", headers={"HX-Request": "true"})
    assert htmx.status_code == 204

    # Idempotent: revoking an already-revoked key still 204s for HTMX.
    again = await client.delete(
        f"/api-keys/{key_id}", headers={"HX-Request": "true"})
    assert again.status_code == 204

    page = await client.get("/account")
    assert f'hx-delete="/api-keys/{key_id}"' not in page.text
    assert "revoked" in page.text
    assert raw not in page.text


async def test_revoked_key_stops_authorizing(client):
    await logged_in(client)
    made = await client.post("/api-keys", json={"label": "short-lived"})
    raw = made.json()["raw"]
    headers = {"Authorization": f"Bearer {raw}"}

    # A cookie-less client proves the KEY alone was authorizing.
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test") as fresh:
        assert (await fresh.get(
            "/api-keys", headers=headers)).status_code == 200

    key_id = made.json()["id"]
    await client.delete(f"/api-keys/{key_id}",
                        headers={"HX-Request": "true"})
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test") as fresh:
        assert (await fresh.get(
            "/api-keys", headers=headers)).status_code == 401
