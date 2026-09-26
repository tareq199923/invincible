# tests/test_dashboard_providers.py
"""Phase 9 PR-D: the /dashboard/providers Providers page.

Gates: session-only realm (anon 401, inv_ keys 401, 503 fail-closed
without the credential key); the page renders a single custom connect
form (no catalog cards); a browser form connect round-trips through a
303 redirect and the raw key never appears in any later render; the
HTMX Test button flips the stored status against a fake upstream;
Remove uses the HTMX 204 row delete.
"""
from itertools import count

import httpx
import pytest
from cryptography.fernet import Fernet

from invincible.core.accounts import SESSION_COOKIE
from invincible.core.identity import ApiKeyStore
from invincible.core.user_settings_store import UserSettingsStore
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


# --- connect form rendering -----------------------------------------------------


async def test_page_renders_single_custom_connect_form(credential_key, client):
    await logged_in(client)
    page = await client.get("/dashboard/providers")
    assert page.status_code == 200
    assert "Connect a provider" in page.text
    # One generic form: name, base URL, default model, key. No catalog
    # cards and no catalog_key prefill inputs.
    assert 'action="/providers/mine"' in page.text
    assert 'name="provider_name"' in page.text
    assert 'name="base_url"' in page.text
    assert 'name="model_id"' in page.text
    assert 'name="api_key"' in page.text
    assert 'name="catalog_key"' not in page.text
    assert "Not connected" not in page.text


async def test_nav_links_providers(credential_key, client):
    await logged_in(client)
    page = await client.get("/dashboard/chat")
    # Sidebar nav (ui overhaul 2026-09): attribute order/labels changed,
    # the link targets are the contract.
    assert 'href="/dashboard/providers"' in page.text
    # Q1 decided 2026-08-30: /mcp stays OAuth-only, but the management
    # page ships (Phase 3), so the nav entry exists now.
    assert 'href="/dashboard/mcp"' in page.text


async def test_connected_provider_appears_in_table(credential_key, client):
    await logged_in(client)
    made = await client.post("/providers/mine", json={
        "provider_name": "My Groq", "catalog_key": "groq",
        "api_key": RAW_KEY})
    assert made.status_code == 201, made.text
    page = await client.get("/dashboard/providers")
    # The connection lands in the connected-providers table; the
    # connect form stays a single generic form.
    assert "My Groq" in page.text
    assert 'name="catalog_key"' not in page.text

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
    # T0-2: success flashes render green (banner-ok), amber stays
    # warning-only.
    assert 'class="banner-ok"' in page.text
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


async def test_test_button_swaps_row_in_place_ok(credential_key, client):
    await logged_in(client)
    made = await client.post("/providers/mine", json={
        "provider_name": "My Groq", "catalog_key": "groq",
        "api_key": RAW_KEY})
    cred_id = made.json()["id"]

    page = await client.get("/dashboard/providers")
    assert f'hx-post="/providers/mine/{cred_id}/test"' in page.text
    assert "status-untested" in page.text
    # T0-3: the Test button targets its own row for an outerHTML swap.
    assert 'hx-target="closest tr"' in page.text
    assert 'hx-swap="outerHTML"' in page.text

    transport, calls = _upstream_transport(200)
    app.state.byok_http_client = httpx.AsyncClient(transport=transport)
    try:
        report = await client.post(
            f"/providers/mine/{cred_id}/test",
            headers={"HX-Request": "true"})
    finally:
        await app.state.byok_http_client.aclose()
        app.state.byok_http_client = None
    assert report.status_code == 200
    # The response is the re-rendered <tr>: updated badge, swap wiring
    # intact for the next test, and never the raw key.
    assert "status-ok" in report.text
    assert 'hx-swap="outerHTML"' in report.text
    assert RAW_KEY not in report.text
    assert calls[0]["authorization"] == f"Bearer {RAW_KEY}"

    page = await client.get("/dashboard/providers?tested=ok")
    assert "Connection test passed." in page.text
    assert "status-ok" in page.text
    assert RAW_KEY not in page.text


async def test_test_button_swaps_row_in_place_failed(credential_key, client):
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
    assert report.status_code == 200
    assert "status-failed" in report.text
    assert RAW_KEY not in report.text

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


# --- Phase 1: provider ordering + routing mode + request settings ----------------


@pytest.fixture
def public_dns(monkeypatch):
    """Custom base URLs go through the SSRF guard (DNS-resolving); fake it
    so the mock hosts never touch a real resolver - also for the per-use
    re-check on the chat path."""
    import invincible.core.url_safety as url_safety

    monkeypatch.setattr(
        url_safety, "_default_resolve", lambda host: ["93.184.216.34"])


async def _connect_custom(client, name, model_id):
    made = await client.post("/providers/mine", json={
        "provider_name": name,
        "base_url": f"https://{name.lower()}.example.com/v1",
        "model_id": model_id,
        "api_key": RAW_KEY,
    })
    assert made.status_code == 201, made.text
    return made.json()


async def test_move_swaps_order_and_next_request_routes_first(
    credential_key, public_dns, client, router_setter
):
    """▲ on Second swaps the stored order; the swapped <tbody> re-renders
    in the new order, and the user's NEXT chat request now starts from
    the moved-up provider (auto mode = sort order)."""
    uid = await logged_in(client)
    await _connect_custom(client, "First", "first-model")
    second = await _connect_custom(client, "Second", "second-model")

    page = await client.get("/dashboard/providers")
    assert f'hx-post="/providers/mine/{second["id"]}/move"' in page.text

    moved = await client.post(
        f"/providers/mine/{second['id']}/move",
        data={"direction": "up"}, headers={"HX-Request": "true"})
    assert moved.status_code == 200
    # The re-rendered tbody lists Second before First now.
    assert moved.text.index("Second") < moved.text.index("First")
    listed = await client.get("/providers/mine")
    assert [p["provider_name"] for p in listed.json()["providers"]] == [
        "Second", "First"]

    # The next request routes through the moved-up provider first (no
    # request model, so nothing reorders the auto-mode tier order).
    calls = {}

    def handler(host):
        def h(request):
            calls.setdefault(host, []).append(str(request.url))
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "hello"}}]})
        return h

    router_setter({
        "first.example.com": handler("first"),
        "second.example.com": handler("second"),
    })
    key = await ApiKeyStore(app.state.engine).create(uid, label="t")
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key['raw']}"},
        json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-invincible-provider"] == "Second"
    assert resp.headers["x-invincible-model"] == "second-model"
    assert len(calls.get("second", [])) == 1
    assert calls.get("first") is None  # never needed - no failover


async def test_move_redirects_without_htmx_and_404s_foreign_ids(
    credential_key, public_dns, client
):
    # The foreign user is registered FIRST: /auth/register auto-logs-in
    # and would otherwise switch this client's session away from the
    # user under test.
    other, _ = await register_account(client, "move-other@example.com")
    other_cred = await _connect_custom_for(client, other.json()["id"])

    await logged_in(client)
    first = await _connect_custom(client, "First", "first-model")
    # Plain (routing-form button) posts redirect back to the page.
    moved = await client.post(
        f"/providers/mine/{first['id']}/move", data={"direction": "up"})
    assert moved.status_code == 303
    assert moved.headers["location"] == "/dashboard/providers#routing"

    # Unknown and foreign ids are indistinguishable 404s. Foreign: the
    # other user's credential must not be movable from this session.
    for bad_id in (999999, other_cred["id"]):
        resp = await client.post(
            f"/providers/mine/{bad_id}/move", data={"direction": "up"})
        assert resp.status_code == 404
    # And a bogus direction is a 400, not a silent success.
    resp = await client.post(
        f"/providers/mine/{first['id']}/move", data={"direction": "sideways"})
    assert resp.status_code == 400


async def _connect_custom_for(client, uid):
    """Connect a credential directly through the store for a given user
    (the OTHER user in cross-user tests - no session juggling). The
    credential_key fixture has already set the encryption master key."""
    from invincible.core.credential_store import ByokCredentialStore

    return await ByokCredentialStore(app.state.engine).create(
        user_id=uid, provider_name="Other",
        model_id="other-model",
        base_url="https://other.example.com/v1",
        api_key="other-key-1234567890")


async def test_routing_form_saves_chain_and_prefills(
    credential_key, public_dns, client
):
    await logged_in(client)
    first = await _connect_custom(client, "First", "first-model")
    second = await _connect_custom(client, "Second", "second-model")

    saved = await client.post("/routing/mine", data={
        "mode": "chain",
        "chain_0_credential_id": second["id"],
        "chain_0_model": "kimi-step",
        "chain_1_credential_id": first["id"],
        "chain_1_model": "glm-step",
    })
    assert saved.status_code == 303, saved.text
    assert saved.headers["location"] == "/dashboard/providers?routing_saved=1"

    stored = await client.get("/routing/mine")
    assert stored.status_code == 200
    assert stored.json()["routing"] == {"mode": "chain", "chain": [
        {"credential_id": second["id"], "model": "kimi-step"},
        {"credential_id": first["id"], "model": "glm-step"},
    ]}

    # The page re-renders the saved chain: banner, checked mode radio,
    # and the stored step models pre-filled (not the stored defaults).
    page = await client.get("/dashboard/providers?routing_saved=1")
    assert "Routing saved." in page.text
    assert 'value="chain"' in page.text
    assert 'value="kimi-step"' in page.text
    assert 'value="glm-step"' in page.text
    assert 'value="first-model"' not in page.text


async def test_routing_save_validates_ownership_and_shape(
    credential_key, public_dns, client
):
    # Foreign user first (registration auto-logs-in and would switch the
    # session away from the user under test).
    other, _ = await register_account(client, "routing-other@example.com")
    other_cred = await _connect_custom_for(client, other.json()["id"])

    await logged_in(client)
    first = await _connect_custom(client, "First", "first-model")

    # Foreign credential id: rejected (and indistinguishable from stale).
    resp = await client.post("/routing/mine", data={
        "mode": "chain",
        "chain_0_credential_id": other_cred["id"],
        "chain_0_model": "x",
    })
    assert resp.status_code == 400
    # Empty chain.
    resp = await client.post("/routing/mine", data={"mode": "chain"})
    assert resp.status_code == 400
    # Blank step model.
    resp = await client.post("/routing/mine", data={
        "mode": "chain",
        "chain_0_credential_id": first["id"], "chain_0_model": "  "})
    assert resp.status_code == 400
    # Unknown mode.
    resp = await client.post("/routing/mine", data={"mode": "chaos"})
    assert resp.status_code == 400
    # Pinned without a provider.
    resp = await client.post("/routing/mine", data={
        "mode": "pinned", "pinned_model": "m"})
    assert resp.status_code == 400
    # Nothing was stored by the rejected attempts.
    assert (await client.get("/routing/mine")).json()["routing"] == {}


async def test_routing_json_body_round_trip(credential_key, public_dns, client):
    """JSON clients (scripts) get the same validation + a JSON response."""
    await logged_in(client)
    first = await _connect_custom(client, "First", "first-model")
    saved = await client.post("/routing/mine", json={
        "mode": "pinned",
        "pinned": {"credential_id": first["id"], "model": "pinned-model"},
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["routing"] == {
        "mode": "pinned",
        "pinned": {"credential_id": first["id"], "model": "pinned-model"},
    }
    assert (await client.get("/routing/mine")).json()["routing"] == (
        saved.json()["routing"])


async def test_settings_form_saves_overrides(client):
    """The tri-state request settings round-trip: on/off persist, default
    is omitted, 0 = no cap."""
    uid = await logged_in(client)
    saved = await client.post("/dashboard/settings", data={
        "memory": "off",
        "compression": "on",
        "continuity": "default",
        "relay": "",
        "history_max_turns": "0",
    })
    assert saved.status_code == 303, saved.text
    assert saved.headers["location"] == "/dashboard/settings?saved=1"
    stored = await UserSettingsStore(app.state.engine).overrides_for(uid)
    assert stored == {"memory": False, "compression": True,
                      "history_max_turns": 0}

    page = await client.get("/dashboard/settings?saved=1")
    assert page.status_code == 200
    assert "Request settings saved." in page.text
    # The saved values re-render as the selected options.
    assert '<option value="off" selected' in page.text
    assert '<option value="on" selected' in page.text
    assert 'value="0"' in page.text

    # "default" selections stay omitted on a re-save.
    await client.post("/dashboard/settings", data={
        "memory": "default", "compression": "on",
        "history_max_turns": ""})
    stored = await UserSettingsStore(app.state.engine).overrides_for(uid)
    assert stored == {"compression": True}


async def test_settings_rejects_bad_values(client):
    uid = await logged_in(client)
    resp = await client.post("/dashboard/settings", data={
        "history_max_turns": "not-a-number"})
    assert resp.status_code == 400
    # Nothing was stored by the rejected attempt.
    assert await UserSettingsStore(
        app.state.engine).overrides_for(uid) == {}
