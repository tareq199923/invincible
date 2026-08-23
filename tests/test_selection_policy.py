# tests/test_selection_policy.py
"""Selection-policy tests (Phase 13.5): auto / pinned / chain.

Unit tests exercise attempt_order directly over synthetic snapshots;
router-integration tests prove the modes end-to-end through
Router(route_request) with a registry, including forced model ids and
pinned-failure surfacing.
"""
import httpx
import pytest

from invincible.core.provider_registry import ProviderRegistry
from invincible.core.router import AllProvidersFailedError, Router
from invincible.core.selection import (
    AUTO_ROUTING,
    PinnedUnavailableError,
    attempt_order,
    routing_from_config,
)
from tests.conftest import default_providers, provider_body

MESSAGES = [{"role": "user", "content": "hi"}]


def snap(*specs):
    """Build provider snapshots from terse tuples:
    (name, tier, model_id[, enabled][, aliases])."""
    out = []
    for spec in specs:
        name, tier, model = spec[0], spec[1], spec[2]
        entry = {
            "name": name,
            "tier": tier,
            "base_url": f"https://{name}.example.com/v1",
            "api_key_env": f"{name.upper()}_API_KEY",
            "model_id": model,
        }
        for extra in spec[3:]:
            if isinstance(extra, list):
                entry["aliases"] = extra
            elif extra is False:
                entry["enabled"] = False
        out.append(entry)
    return out


class NoCooldowns:
    def is_available(self, name):
        return True


# ---------------------------------------------------------------- unit


def test_auto_respects_tier_order_and_alias_hint():
    providers = snap(("b", 2, "m-b"), ("a", 1, "m-a"), ("c", 3, "m-c", ["fast"]))
    order = [
        p["name"] for p in attempt_order(providers, NoCooldowns(), AUTO_ROUTING, None)
    ]
    assert order == ["a", "b", "c"]

    hinted = [
        p["name"]
        for p in attempt_order(providers, NoCooldowns(), AUTO_ROUTING, "fast")
    ]
    assert hinted == ["c", "a", "b"]

    exact = [
        p["name"]
        for p in attempt_order(providers, NoCooldowns(), AUTO_ROUTING, "m-b")
    ]
    assert exact == ["b", "a", "c"]


def test_auto_excludes_disabled():
    providers = snap(("a", 1, "m-a"), ("b", 2, "m-b", False))
    order = [
        p["name"] for p in attempt_order(providers, NoCooldowns(), AUTO_ROUTING, None)
    ]
    assert order == ["a"]


def test_pinned_single_candidate_with_forced_model():
    providers = snap(("a", 1, "m-a"), ("b", 2, "m-b"))
    routing = routing_from_config(
        {"mode": "pinned", "pinned": {"provider": "b", "model": "forced-model"}}
    )
    candidates = attempt_order(providers, NoCooldowns(), routing, "m-a")
    assert len(candidates) == 1
    # Alias hint must be inert in pinned mode.
    assert candidates[0]["name"] == "b"
    assert candidates[0]["model_id"] == "forced-model"


def test_pinned_missing_or_disabled_raises():
    providers = snap(("a", 1, "m-a"), ("b", 2, "m-b", False))
    missing = routing_from_config(
        {"mode": "pinned", "pinned": {"provider": "ghost", "model": "m"}}
    )
    with pytest.raises(PinnedUnavailableError, match="ghost"):
        attempt_order(providers, NoCooldowns(), missing, None)

    disabled = routing_from_config(
        {"mode": "pinned", "pinned": {"provider": "b", "model": "m"}}
    )
    with pytest.raises(PinnedUnavailableError, match="disabled"):
        attempt_order(providers, NoCooldowns(), disabled, None)


def test_chain_overrides_models_and_skips_disabled():
    providers = snap(("a", 1, "m-a"), ("b", 2, "m-b", False), ("c", 3, "m-c"))
    routing = routing_from_config(
        {
            "mode": "chain",
            "chain": [
                {"provider": "a", "model": "chain-a"},
                {"provider": "b", "model": "chain-b"},  # disabled -> skipped
                {"provider": "c", "model": "chain-c"},
            ],
        }
    )
    candidates = attempt_order(providers, NoCooldowns(), routing, None)
    assert [(p["name"], p["model_id"]) for p in candidates] == [
        ("a", "chain-a"),
        ("c", "chain-c"),
    ]


# ------------------------------------------------- router integration


async def test_router_pinned_mode_only_attempts_target(tmp_path, monkeypatch):
    monkeypatch.setenv("BETA_API_KEY", "k-beta")
    path = str(tmp_path / "providers.user.yaml")
    registry = ProviderRegistry(
        file_path=path, seed_config={"providers": default_providers()}
    )
    await registry.set_routing(
        "pinned", pinned={"provider": "beta", "model": "beta-model"}
    )

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, request.read()))
        return httpx.Response(500, json={"error": "down"})

    router = Router(transport=httpx.MockTransport(handler), registry=registry)
    with pytest.raises(AllProvidersFailedError):
        await router.route_request(MESSAGES)
    # Exactly one upstream call: pinned failure never substitutes.
    assert [host for host, _ in calls] == ["beta.example.com"]
    assert b'"beta-model"' in calls[0][1] or b'beta-model' in calls[0][1]


async def test_router_chain_failover_uses_forced_models(tmp_path, monkeypatch):
    monkeypatch.setenv("BETA_API_KEY", "k-beta")
    monkeypatch.setenv("GAMMA_API_KEY", "k-gamma")
    path = str(tmp_path / "providers.user.yaml")
    registry = ProviderRegistry(
        file_path=path, seed_config={"providers": default_providers()}
    )
    await registry.set_routing(
        "chain",
        chain=[
            {"provider": "alpha", "model": "alpha-chain"},  # no key -> skipped
            {"provider": "beta", "model": "beta-chain"},  # 500s -> failover
            {"provider": "gamma", "model": "gamma-chain"},
        ],
    )

    seen_models = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.read())
        provider_name = body["model"].split("-")[0]
        seen_models.append(body["model"])
        if request.url.host.startswith("beta"):
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json=provider_body(provider_name))

    router = Router(transport=httpx.MockTransport(handler), registry=registry)
    result = await router.route_request(MESSAGES)
    assert result["choices"][0]["message"]["role"] == "assistant"
    assert seen_models == ["beta-chain", "gamma-chain"]


async def test_router_registry_disabled_provider_is_invisible(tmp_path, monkeypatch):
    monkeypatch.setenv("GAMMA_API_KEY", "k-gamma")
    path = str(tmp_path / "providers.user.yaml")
    registry = ProviderRegistry(
        file_path=path, seed_config={"providers": default_providers()}
    )
    await registry.disable("gamma")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=provider_body())

    router = Router(transport=httpx.MockTransport(handler), registry=registry)
    with pytest.raises(AllProvidersFailedError):
        await router.route_request(MESSAGES)
