# tests/test_admin_api.py
"""Management-surface authz and CRUD (Phase 13.5).

The conftest `client` builds app.state directly (no lifespan), so these
tests attach their own ProviderRegistry - mirroring what the lifespan
wires in production.
"""
import httpx

from invincible.core.provider_registry import ProviderRegistry
from invincible.core.router import Router
from invincible.main import app
from tests.conftest import default_providers

ADMIN = {"Authorization": "Bearer admin-secret"}
GATEWAY = {"Authorization": "Bearer test-gateway-key"}


def make_registry(tmp_path):
    return ProviderRegistry(
        file_path=str(tmp_path / "providers.user.yaml"),
        seed_config={"providers": default_providers()},
    )


async def test_management_api_fail_closed_without_admin_key(client, monkeypatch):
    monkeypatch.delenv("INVINCIBLE_ADMIN_KEY", raising=False)
    resp = await client.get("/api/v1/providers")
    assert resp.status_code == 503
    assert "INVINCIBLE_ADMIN_KEY" in resp.json()["detail"]["error"]["message"]


async def test_gateway_key_is_not_an_admin_credential(client, monkeypatch, tmp_path):
    """Holding GATEWAY_API_KEY must not authorize provider mutation."""
    monkeypatch.setenv("INVINCIBLE_ADMIN_KEY", "admin-secret")
    app.state.registry = make_registry(tmp_path)

    forbidden = await client.get("/api/v1/providers", headers=GATEWAY)
    assert forbidden.status_code == 401

    no_key = await client.get("/api/v1/providers")
    assert no_key.status_code == 401

    ok = await client.get("/api/v1/providers", headers=ADMIN)
    assert ok.status_code == 200


async def test_provider_crud_roundtrip(client, monkeypatch, tmp_path):
    monkeypatch.setenv("INVINCIBLE_ADMIN_KEY", "admin-secret")
    registry = make_registry(tmp_path)
    app.state.registry = registry

    entry = {
        "name": "zeta",
        "tier": 9,
        "base_url": "https://zeta.example.com/v1",
        "api_key_env": "ZETA_API_KEY",
        "model_id": "zeta-model",
    }
    created = await client.post("/api/v1/providers", headers=ADMIN, json=entry)
    assert created.status_code == 201

    listing = await client.get("/api/v1/providers", headers=ADMIN)
    names = [p["name"] for p in listing.json()["providers"]]
    assert "zeta" in names

    patched = await client.patch(
        "/api/v1/providers/zeta", headers=ADMIN, json={"tier": 2}
    )
    assert patched.json()["provider"]["tier"] == 2

    deleted = await client.delete("/api/v1/providers/zeta", headers=ADMIN)
    assert deleted.json()["removed"] == "zeta"


async def test_enable_disable_endpoints(client, monkeypatch, tmp_path):
    monkeypatch.setenv("INVINCIBLE_ADMIN_KEY", "admin-secret")
    registry = make_registry(tmp_path)
    app.state.registry = registry

    disabled = await client.post(
        "/api/v1/providers/beta/disable", headers=ADMIN
    )
    assert disabled.status_code == 200
    assert any(
        p["name"] == "beta" and p.get("enabled") is False
        for p in registry.list()
    )

    enabled = await client.post("/api/v1/providers/beta/enable", headers=ADMIN)
    assert enabled.status_code == 200
    assert all(
        p.get("enabled", True) for p in registry.list()
    )


async def test_routing_get_put(client, monkeypatch, tmp_path):
    monkeypatch.setenv("INVINCIBLE_ADMIN_KEY", "admin-secret")
    registry = make_registry(tmp_path)
    app.state.registry = registry

    current = await client.get("/api/v1/routing", headers=ADMIN)
    assert current.json()["mode"] == "auto"

    put = await client.put(
        "/api/v1/routing",
        headers=ADMIN,
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
        "/api/v1/routing", headers=ADMIN,
        json={"mode": "pinned"},  # missing pinned block
    )
    assert bad.status_code == 400


async def test_disable_via_admin_changes_next_chat_route(
    client, router_setter, monkeypatch, tmp_path
):
    monkeypatch.setenv("INVINCIBLE_ADMIN_KEY", "admin-secret")
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
