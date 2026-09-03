# tests/test_admin_api.py
"""Management-surface authz and CRUD (Phase 13.5).

The conftest `client` builds app.state directly (no lifespan), so these
tests attach their own ProviderRegistry - mirroring what the lifespan
wires in production.

The INVINCIBLE_ADMIN_KEY realm was retired: management now authenticates
through the operator account realm (operator-role session cookie or the
operator's own inv_ API key), the same realm as the dashboard.
"""
import httpx

from invincible.core.provider_registry import ProviderRegistry
from invincible.core.router import Router
from invincible.main import app
from tests.conftest import default_providers, operator_session

GATEWAY = {"Authorization": "Bearer test-gateway-key"}


def make_registry(tmp_path):
    return ProviderRegistry(
        file_path=str(tmp_path / "providers.user.yaml"),
        seed_config={"providers": default_providers()},
    )


async def test_management_api_fail_closed_without_owner_secret(
    client, monkeypatch
):
    """No INVINCIBLE_OWNER_SECRET - no account sessions, so nothing is
    manageable (same fail-closed posture the old admin key kept)."""
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    resp = await client.get("/api/v1/providers")
    assert resp.status_code == 503
    assert "INVINCIBLE_OWNER_SECRET" in resp.json()["detail"]["error"]["message"]


async def test_management_requires_operator_credentials(
    client, monkeypatch, tmp_path
):
    """No credential -> 401; gateway key (a chat credential) -> 401."""
    app.state.registry = make_registry(tmp_path)

    no_key = await client.get("/api/v1/providers")
    assert no_key.status_code == 401

    forbidden = await client.get("/api/v1/providers", headers=GATEWAY)
    assert forbidden.status_code == 401


async def test_plain_user_session_is_forbidden(client, monkeypatch, tmp_path):
    """A logged-in non-operator account is told why: 403, not 401."""
    from tests.conftest import register_account

    app.state.registry = make_registry(tmp_path)
    # Fresh users table: the FIRST registration bootstraps as operator.
    await operator_session(client, email="first-op@example.com")
    # A second, plain account - logged in last so the cookie is its own.
    await register_account(client, "plain@example.com")
    login = await client.post(
        "/auth/login", json={"email": "plain@example.com",
                             "password": "longenough1"})
    assert login.status_code == 200
    resp = await client.get("/api/v1/providers")
    assert resp.status_code == 403


async def test_operator_session_can_manage_providers(
    client, monkeypatch, tmp_path
):
    app.state.registry = make_registry(tmp_path)
    await operator_session(client)

    ok = await client.get("/api/v1/providers")
    assert ok.status_code == 200
    assert ok.json()["providers"]


async def test_operator_inv_key_authorizes_scripts(client, monkeypatch, tmp_path):
    """Terminal use: an inv_ API key owned by an operator account, with
    no session cookie at all (a bare script-style client)."""
    import httpx as _httpx

    from invincible.core.identity import ApiKeyStore

    app.state.registry = make_registry(tmp_path)
    uid = await operator_session(client)
    record = await ApiKeyStore(app.state.engine).create(uid, label="ops")
    headers = {"Authorization": f"Bearer {record['raw']}"}

    async with _httpx.AsyncClient(
        transport=_httpx.ASGITransport(app=app), base_url="http://test"
    ) as bare:
        resp = await bare.get("/api/v1/providers", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["providers"]


async def test_provider_crud_roundtrip(client, monkeypatch, tmp_path):
    await operator_session(client)
    registry = make_registry(tmp_path)
    app.state.registry = registry

    entry = {
        "name": "zeta",
        "tier": 9,
        "base_url": "https://zeta.example.com/v1",
        "api_key_env": "ZETA_API_KEY",
        "model_id": "zeta-model",
    }
    created = await client.post("/api/v1/providers", json=entry)
    assert created.status_code == 201

    listing = await client.get("/api/v1/providers")
    names = [p["name"] for p in listing.json()["providers"]]
    assert "zeta" in names

    patched = await client.patch(
        "/api/v1/providers/zeta", json={"tier": 2}
    )
    assert patched.json()["provider"]["tier"] == 2

    deleted = await client.delete("/api/v1/providers/zeta")
    assert deleted.json()["removed"] == "zeta"


async def test_enable_disable_endpoints(client, monkeypatch, tmp_path):
    await operator_session(client)
    registry = make_registry(tmp_path)
    app.state.registry = registry

    disabled = await client.post("/api/v1/providers/beta/disable")
    assert disabled.status_code == 200
    assert any(
        p["name"] == "beta" and p.get("enabled") is False
        for p in registry.list()
    )

    enabled = await client.post("/api/v1/providers/beta/enable")
    assert enabled.status_code == 200
    assert all(
        p.get("enabled", True) for p in registry.list()
    )


async def test_routing_get_put(client, monkeypatch, tmp_path):
    await operator_session(client)
    registry = make_registry(tmp_path)
    app.state.registry = registry

    current = await client.get("/api/v1/routing")
    assert current.json()["mode"] == "auto"

    put = await client.put(
        "/api/v1/routing",
        json={
            "mode": "chain",
            "chain": [
                {"provider": "alpha", "model": "m-a"},
                {"provider": "beta", "model": "m-b"},
            ],
        },
    )
    assert put.status_code == 200
    assert put.json()["mode"] == "chain"

    bad = await client.put(
        "/api/v1/routing",
        json={"mode": "pinned"},  # missing pinned block
    )
    assert bad.status_code == 400


async def test_disable_via_admin_changes_next_chat_route(
    client, router_setter, monkeypatch, tmp_path
):
    await operator_session(client)
    registry = make_registry(tmp_path)
    app.state.registry = registry
    await registry.disable("alpha")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}
                ]
            },
        )

    router_setter.routers.append(
        Router(transport=httpx.MockTransport(handler), registry=registry)
    )
    app.state.router = router_setter.routers[-1]

    resp = await client.post(
        "/v1/chat/completions",
        headers=GATEWAY,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.headers["x-invincible-provider"] == "beta"
