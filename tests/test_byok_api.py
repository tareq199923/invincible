# tests/test_byok_api.py
"""Platform Phase 9 PR-B: connect/list/test/remove API.

Gates: fail-closed 503 without INVINCIBLE_CREDENTIAL_KEY; session-only
realm (anon 401, inv_ keys 401 everywhere); ownership-predicated access
with identical foreign/unknown 404s; masked-key display never leaks the
full secret; the SSRF guard runs on custom creates and on every test
call; catalog constants skip the guard only while unedited; audit rows
carry metadata only.
"""
import json

import httpx
import pytest
from cryptography.fernet import Fernet

from invincible.core.accounts import SESSION_COOKIE
from invincible.core.identity import ApiKeyStore
from invincible.main import app
from tests.conftest import register_account

RAW_KEY = "gsk_live_abcdefgh1234567890"


@pytest.fixture
def credential_key(monkeypatch):
    monkeypatch.setenv(
        "INVINCIBLE_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def public_dns(monkeypatch):
    """Hermetic fake DNS: any dotted host resolves to a public address."""
    import invincible.core.url_safety as url_safety

    monkeypatch.setattr(
        url_safety, "_default_resolve",
        lambda host: ["93.184.216.34"])


async def logged_in(client, email="byok@example.com"):
    registered, _ = await register_account(client, email)
    assert registered.status_code == 201, registered.text
    return registered.json()["id"]


async def connect(client, **overrides):
    body = {
        "provider_name": "My Groq",
        "catalog_key": "groq",
        "api_key": RAW_KEY,
    }
    body.update(overrides)
    body = {k: v for k, v in body.items() if v is not None}
    return await client.post("/providers/mine", json=body)


# --- realm / fail-closed gates ---------------------------------------------


async def test_surfaces_fail_closed_without_credential_key(client):
    await logged_in(client, "closed@example.com")
    assert (await client.get("/providers/mine")).status_code == 503
    assert (await client.post(
        "/providers/mine", json={"provider_name": "x", "api_key": "k"})
        ).status_code == 503
    client.cookies.delete(SESSION_COOKIE)
    assert (await client.get("/providers/mine")).status_code == 503  # even anon


async def test_surfaces_require_session(credential_key, client):
    assert (await client.get("/dashboard/providers")).status_code == 401
    assert (await client.get("/providers/mine")).status_code == 401
    posted = await client.post(
        "/providers/mine", json={"provider_name": "x", "api_key": "k"})
    assert posted.status_code == 401
    assert (await client.post(
        "/providers/mine/999/test")).status_code == 401
    assert (await client.delete("/providers/mine/999")).status_code == 401


async def test_inv_api_key_never_authorizes_byok_surface(
    credential_key, client
):
    uid = await logged_in(client, "keyed@example.com")
    key = await ApiKeyStore(app.state.engine).create(uid, label="t")
    client.cookies.delete(SESSION_COOKIE)
    headers = {"Authorization": f"Bearer {key['raw']}"}
    assert (await client.get(
        "/dashboard/providers", headers=headers)).status_code == 401
    assert (await client.get(
        "/providers/mine", headers=headers)).status_code == 401
    assert (await client.post(
        "/providers/mine", headers=headers,
        json={"provider_name": "x", "api_key": "k"})).status_code == 401
    assert (await client.post(
        "/providers/mine/999/test", headers=headers)).status_code == 401
    assert (await client.delete(
        "/providers/mine/999", headers=headers)).status_code == 401


# --- connect / list / masking -----------------------------------------------


async def test_connect_catalog_and_list_masks_key(credential_key, client):
    await logged_in(client)
    made = await connect(client)
    assert made.status_code == 201, made.text
    row = made.json()
    assert row["provider_name"] == "My Groq"
    assert row["catalog_key"] == "groq"
    assert row["base_url"] == "https://api.groq.com/openai/v1"
    assert row["status"] == "untested"
    # Masked hint matches the first-3 + last-4 convention; the raw key and
    # ciphertext never appear anywhere in the response.
    assert row["key_masked"] == "gsk…7890"
    assert RAW_KEY not in made.text
    assert "encrypted_api_key" not in made.text
    assert "api_key" not in made.text

    listed = await client.get("/providers/mine")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert RAW_KEY not in listed.text


async def test_duplicate_provider_name_rejected(credential_key, client):
    await logged_in(client)
    assert (await connect(client)).status_code == 201
    dup = await connect(client)
    assert dup.status_code == 400
    assert "already connected" in dup.json()["detail"]["error"]["message"]


async def test_page_renders_masked_key_only(credential_key, client):
    await logged_in(client)
    await connect(client)
    page = await client.get("/dashboard/providers")
    assert page.status_code == 200
    assert "gsk…7890" in page.text
    assert RAW_KEY not in page.text
    assert "https://api.groq.com/openai/v1" in page.text


# --- SSRF on create ----------------------------------------------------------


@pytest.mark.parametrize("base_url", [
    "http://api.example.com/v1",
    "https://127.0.0.1/v1",
    "https://10.0.0.5/v1",
    "https://192.168.1.1/v1",
    "https://169.254.169.254/v1",
    "https://[::1]/v1",
    "https://[fc00::1]/v1",
    "https://localhost/v1",
    "https://myprovider/v1",
])
async def test_custom_create_rejects_unsafe_urls(
    credential_key, client, base_url
):
    await logged_in(client)
    made = await connect(client, catalog_key=None, provider_name="Custom",
                         base_url=base_url, model_id="m")
    assert made.status_code == 400, made.text
    assert "base URL rejected" in made.json()["detail"]["error"]["message"]


async def test_custom_public_url_accepted(
    credential_key, client, public_dns
):
    await logged_in(client)
    made = await connect(
        client, catalog_key=None, provider_name="Custom",
        base_url="https://api.example.com/v1", model_id="my-model")
    assert made.status_code == 201, made.text
    assert made.json()["catalog_key"] is None


async def test_edited_catalog_url_is_validated_like_custom(
    credential_key, client
):
    """A catalog entry whose URL was edited away from the constant loses
    the operator-supplied exemption."""
    await logged_in(client)
    made = await connect(client, base_url="https://127.0.0.1/v1")
    assert made.status_code == 400


async def test_catalog_constant_skips_dns_check(
    credential_key, client, monkeypatch
):
    """Catalog base URLs are operator constants: connecting one must not
    require DNS at all (the resolver here explodes if consulted)."""
    import invincible.core.url_safety as url_safety

    def _no_dns(host):
        raise AssertionError(f"DNS consulted for {host}")

    monkeypatch.setattr(url_safety, "_default_resolve", _no_dns)
    await logged_in(client)
    assert (await connect(client)).status_code == 201


# --- ownership isolation ------------------------------------------------------


async def test_foreign_and_unknown_ids_identical_404(
    credential_key, client
):
    owner_uid = await logged_in(client, "owner@example.com")
    made = await connect(client)
    cred_id = made.json()["id"]

    other_uid = await logged_in(client, "other@example.com")
    assert other_uid != owner_uid
    foreign = await client.post(f"/providers/mine/{cred_id}/test")
    unknown = await client.post("/providers/mine/999999/test")
    foreign_del = await client.delete(f"/providers/mine/{cred_id}")
    unknown_del = await client.delete("/providers/mine/999999")
    for resp in (foreign, unknown, foreign_del, unknown_del):
        assert resp.status_code == 404
    assert foreign.json() == unknown.json()
    assert foreign_del.json() == unknown_del.json()
    # The owner's row is untouched by every foreign attempt.
    listed = await client.get("/providers/mine")
    assert listed.json()["count"] == 0


# --- test endpoint --------------------------------------------------------------


def _upstream_transport(status_code=200):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url),
                      "authorization": request.headers.get("Authorization")})
        return httpx.Response(
            status_code,
            json={"data": [{"id": "m"}]} if status_code == 200 else {},
        )

    return httpx.MockTransport(handler), calls


async def test_probe_success_updates_status(
    credential_key, client, public_dns
):
    await logged_in(client)
    made = await connect(
        client, catalog_key=None, provider_name="Mock",
        base_url="https://mockprov.test/v1", model_id="mock-model")
    cred_id = made.json()["id"]
    transport, calls = _upstream_transport(200)
    app.state.byok_http_client = httpx.AsyncClient(transport=transport)
    try:
        report = await client.post(f"/providers/mine/{cred_id}/test")
    finally:
        await app.state.byok_http_client.aclose()
        app.state.byok_http_client = None

    assert report.status_code == 200, report.text
    body = report.json()
    assert body["ok"] is True
    assert body["credential_status"] == "ok"
    assert set(body) == {"ok", "status", "latency_ms", "detail",
                         "credential_status"}
    assert calls and calls[0]["url"] == "https://mockprov.test/v1/models"
    # The DECRYPTED user key went upstream as the bearer credential.
    assert calls[0]["authorization"] == f"Bearer {RAW_KEY}"

    listed = await client.get("/providers/mine")
    assert listed.json()["providers"][0]["status"] == "ok"
    assert RAW_KEY not in report.text


async def test_probe_failure_marks_failed(
    credential_key, client, public_dns
):
    await logged_in(client)
    made = await connect(
        client, catalog_key=None, provider_name="Mock",
        base_url="https://mockprov.test/v1", model_id="mock-model")
    cred_id = made.json()["id"]
    transport, _ = _upstream_transport(401)
    app.state.byok_http_client = httpx.AsyncClient(transport=transport)
    try:
        report = await client.post(f"/providers/mine/{cred_id}/test")
    finally:
        await app.state.byok_http_client.aclose()
        app.state.byok_http_client = None
    assert report.status_code == 200
    assert report.json()["ok"] is False
    assert report.json()["credential_status"] == "failed"
    listed = await client.get("/providers/mine")
    assert listed.json()["providers"][0]["status"] == "failed"


async def test_probe_url_rebound_to_private_is_blocked(
    credential_key, client, monkeypatch
):
    """DNS rebind between add and test: the guard re-checks on every use."""
    import invincible.core.url_safety as url_safety

    await logged_in(client)
    monkeypatch.setattr(
        url_safety, "_default_resolve", lambda host: ["93.184.216.34"])
    made = await connect(
        client, catalog_key=None, provider_name="Evil",
        base_url="https://evil.example.com/v1", model_id="m")
    cred_id = made.json()["id"]
    monkeypatch.setattr(
        url_safety, "_default_resolve", lambda host: ["10.0.0.1"])
    report = await client.post(f"/providers/mine/{cred_id}/test")
    assert report.status_code == 400
    assert "base URL rejected" in report.json()["detail"]["error"]["message"]
    listed = await client.get("/providers/mine")
    assert listed.json()["providers"][0]["status"] == "failed"


# --- delete + audit hygiene -------------------------------------------------------


async def test_delete_removes_row_and_audits_metadata_only(
    credential_key, client
):
    await logged_in(client)
    made = await connect(client)
    cred_id = made.json()["id"]

    deleted = await client.delete(f"/providers/mine/{cred_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    htmx = await client.delete(
        f"/providers/mine/{cred_id}", headers={"HX-Request": "true"})
    assert htmx.status_code == 404  # already gone; identical unknown shape

    listed = await client.get("/providers/mine")
    assert listed.json()["count"] == 0

    rows = await app.state.audit_log.recent(limit=50)
    actions = [(r["action"], r["actor_kind"]) for r in rows]
    assert ("byok.credential.created", "user") in actions
    assert ("byok.credential.deleted", "user") in actions
    created = next(r for r in rows if r["action"] == "byok.credential.created")
    assert created["resource_id"] == str(cred_id)
    # Metadata only: no key material, no base_url, no ciphertext anywhere.
    blob = json.dumps(rows, default=str)
    assert RAW_KEY not in blob
    assert "api.groq.com" not in blob
    assert "encrypted" not in blob
    assert created["meta"]["provider_name"] == "My Groq"
