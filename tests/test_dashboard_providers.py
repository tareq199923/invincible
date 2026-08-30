# tests/test_dashboard_providers.py
"""Phase 9 PR-D: the /dashboard/providers Providers page.

Gates: session-only realm (anon 401, inv_ keys 401, 503 fail-closed
without the credential key); the catalog renders one connect card per
CATALOG entry with a connected/not-connected state; a browser form
connect round-trips through a 303 redirect and the raw key never
appears in any later render; the HTMX Test button flips the stored
status against a fake upstream; Remove uses the HTMX 204 row delete.
"""
from itertools import count

import httpx
import pytest
from cryptography.fernet import Fernet

from invincible.core.accounts import SESSION_COOKIE
from invincible.core.identity import ApiKeyStore
from invincible.core.provider_catalog import CATALOG
from invincible.main import app
from tests.conftest import register_account

RAW_KEY = "gsk_live_abcdefgh1234567890"
_email_seq = count(1)


@pytest.fixture
def credential_key(monkeypatch):
    monkeypatch.setenv(
        "INVINCIBLE_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def no_credential_key(monkeypatch):
    """Hermetic "key unset": main.py's import-time load_dotenv() pulls the
    developer's real .env into the test process, so a locally generated
    key must be forced out explicitly (not merely relied on to be absent)."""
    monkeypatch.delenv("INVINCIBLE_CREDENTIAL_KEY", raising=False)


async def logged_in(client):
    # Unique address per call: registration state must never collide
    # across tests or runs, whatever the DB truncation semantics.
    registered, _ = await register_account(
        client, f"byok-ui-{next(_email_seq)}@example.com")
    assert registered.status_code == 201, registered.text
    return registered.json()["id"]


def _upstream_transport(status_code=200):
    calls = []

    def handler(request):
        calls.append({
            "url": str(request.url),
            "authorization": request.headers.get("authorization"),
        })
        return httpx.Response(status_code, json={"data": []})

    return httpx.MockTransport(handler), calls


# --- realm / fail-closed gates ------------------------------------------------


async def test_page_requires_session(credential_key, client):
    assert (await client.get("/dashboard/providers")).status_code == 401


async def test_inv_api_key_never_authorizes_providers_page(
    credential_key, client
):
    uid = await logged_in(client)
    key = await ApiKeyStore(app.state.engine).create(uid, label="t")
    client.cookies.delete(SESSION_COOKIE)
    headers = {"Authorization": f"Bearer {key['raw']}"}
    assert (await client.get(
        "/dashboard/providers", headers=headers)).status_code == 401


async def test_page_fail_closed_without_credential_key(
    no_credential_key, client
):
    await logged_in(client)
    assert (await client.get("/dashboard/providers")).status_code == 503


# --- catalog rendering ----------------------------------------------------------


async def test_page_renders_one_card_per_catalog_entry(credential_key, client):
    await logged_in(client)
    page = await client.get("/dashboard/providers")
    assert page.status_code == 200
    for key, entry in CATALOG.items():
        assert f'name="catalog_key" value="{key}"' in page.text
        assert entry["label"] in page.text
        assert f'value="{entry["base_url"]}"' in page.text
        assert f'value="{entry["model_id"]}"' in page.text
    # Nothing connected yet: every card shows the not-connected state.
    assert page.text.count("Not connected") == len(CATALOG)
    assert "Add a custom provider" in page.text


async def test_nav_links_providers(credential_key, client):
    await logged_in(client)
    page = await client.get("/dashboard")
    assert '<a href="/dashboard/providers">Providers</a>' in page.text
    # Q1 decided 2026-08-30: /mcp stays OAuth-only, but the management
    # page ships (Phase 3), so the nav entry exists now.
    assert '<a href="/dashboard/mcp">MCP</a>' in page.text


async def test_connected_card_flips_state(credential_key, client):
    await logged_in(client)
    made = await client.post("/providers/mine", json={
        "provider_name": "My Groq", "catalog_key": "groq",
        "api_key": RAW_KEY})
    assert made.status_code == 201, made.text
    page = await client.get("/dashboard/providers")
    # groq flips to connected; the rest stay open.
    assert "Connected" in page.text
    assert page.text.count("Not connected") == len(CATALOG) - 1
    assert "My Groq" in page.text

# --- browser connect round-trip -----------------------------------------------


async def test_form_connect_redirects_and_never_echoes_key(
    credential_key, client
):
    await logged_in(client)
    made = await client.post("/providers/mine", data={
        "provider_name": "Groq via form",
        "catalog_key": "groq",
        "api_key": RAW_KEY,
    })
    assert made.status_code == 303
    assert made.headers["location"] == "/dashboard/providers?connected=1"

    # The browser follows the redirect target, which carries the flag.
    page = await client.get(made.headers["location"])
    assert page.status_code == 200
    assert "Provider connected." in page.text
    assert "Groq via form" in page.text
    # The submitted raw key string is absent from every later render.
    assert RAW_KEY not in page.text


async def test_form_connect_invalid_url_rejected(credential_key, client):
    await logged_in(client)
    made = await client.post("/providers/mine", data={
        "provider_name": "Evil", "base_url": "http://10.0.0.5/v1",
        "model_id": "m", "api_key": RAW_KEY,
    })
    assert made.status_code == 400


# --- remove / test actions ------------------------------------------------------


async def test_remove_uses_htmx_row_delete(credential_key, client):
    await logged_in(client)
    made = await client.post("/providers/mine", json={
        "provider_name": "My Groq", "catalog_key": "groq",
        "api_key": RAW_KEY})
    cred_id = made.json()["id"]

    page = await client.get("/dashboard/providers")
    assert f'hx-delete="/providers/mine/{cred_id}"' in page.text
    assert 'hx-target="closest tr"' in page.text

    htmx = await client.delete(
        f"/providers/mine/{cred_id}", headers={"HX-Request": "true"})
    assert htmx.status_code == 204
    listed = await client.get("/providers/mine")
    assert listed.json()["count"] == 0


async def test_test_button_flashes_ok(credential_key, client):
    await logged_in(client)
    made = await client.post("/providers/mine", json={
        "provider_name": "My Groq", "catalog_key": "groq",
        "api_key": RAW_KEY})
    cred_id = made.json()["id"]

    page = await client.get("/dashboard/providers")
    assert f'hx-post="/providers/mine/{cred_id}/test"' in page.text
    assert "status-untested" in page.text

    transport, calls = _upstream_transport(200)
    app.state.byok_http_client = httpx.AsyncClient(transport=transport)
    try:
        report = await client.post(
            f"/providers/mine/{cred_id}/test",
            headers={"HX-Request": "true"})
    finally:
        await app.state.byok_http_client.aclose()
        app.state.byok_http_client = None
    assert report.status_code == 204
    assert (report.headers["HX-Redirect"]
            == "/dashboard/providers?tested=ok")
    assert calls[0]["authorization"] == f"Bearer {RAW_KEY}"

    page = await client.get("/dashboard/providers?tested=ok")
    assert "Connection test passed." in page.text
    assert "status-ok" in page.text
    assert RAW_KEY not in page.text


async def test_test_button_flashes_failed(credential_key, client):
    await logged_in(client)
    made = await client.post("/providers/mine", json={
        "provider_name": "My Groq", "catalog_key": "groq",
        "api_key": RAW_KEY})
    cred_id = made.json()["id"]

    transport, _ = _upstream_transport(401)
    app.state.byok_http_client = httpx.AsyncClient(transport=transport)
    try:
        report = await client.post(
            f"/providers/mine/{cred_id}/test",
            headers={"HX-Request": "true"})
    finally:
        await app.state.byok_http_client.aclose()
        app.state.byok_http_client = None
    assert report.status_code == 204
    assert (report.headers["HX-Redirect"]
            == "/dashboard/providers?tested=failed")

    page = await client.get("/dashboard/providers?tested=failed")
    assert "Connection test failed" in page.text
    assert "status-failed" in page.text


# --- JSON wire shape untouched ---------------------------------------------------


async def test_json_connect_keeps_201_row_shape(credential_key, client):
    await logged_in(client)
    made = await client.post("/providers/mine", json={
        "provider_name": "My Groq", "catalog_key": "groq",
        "api_key": RAW_KEY})
    assert made.status_code == 201
    row = made.json()
    assert row["provider_name"] == "My Groq"
    assert row["catalog_key"] == "groq"
    assert row["status"] == "untested"
    assert RAW_KEY not in made.text
