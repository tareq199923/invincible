# tests/test_provider_registry.py
import asyncio
import os

import httpx
import pytest
import yaml

from invincible.core.provider_registry import ProviderRegistry, ProviderRegistryError
from tests.conftest import default_providers

run = asyncio.run


def provider_entry(name="delta", tier=9, model="delta-model", **extra):
    entry = {
        "name": name,
        "tier": tier,
        "base_url": f"https://{name}.example.com/v1",
        "api_key_env": f"{name.upper()}_API_KEY",
        "model_id": model,
    }
    entry.update(extra)
    return entry


@pytest.fixture
def registry_file(tmp_path):
    return str(tmp_path / "providers.user.yaml")


def read_file(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_read_only_mode_serves_seed_and_refuses_mutations():
    seed = {"providers": default_providers()}
    registry = ProviderRegistry(file_path=None, seed_config=seed)
    assert len(registry.list()) == len(default_providers())
    with pytest.raises(ProviderRegistryError, match="read-only"):
        run(registry.add(provider_entry()))
    with pytest.raises(ProviderRegistryError, match="read-only"):
        run(registry.set_routing("pinned"))


def test_copy_on_first_use_seeds_missing_file(registry_file):
    seed = {"providers": default_providers()}
    ProviderRegistry(file_path=registry_file, seed_config=seed)
    assert os.path.isfile(registry_file)
    stored = read_file(registry_file)
    assert [p["name"] for p in stored["providers"]] == [
        p["name"] for p in default_providers()
    ]
    assert stored["routing"]["mode"] == "auto"
    # A fresh instance reads the same state back.
    reloaded = ProviderRegistry(file_path=registry_file, seed_config=seed)
    assert [p["name"] for p in reloaded.list()] == [
        p["name"] for p in default_providers()
    ]


def test_add_rejects_invalid_and_duplicate_entries(registry_file):
    registry = ProviderRegistry(
        file_path=registry_file, seed_config={"providers": default_providers()}
    )
    first = default_providers()[0]

    bad = provider_entry(name="bad")
    del bad["base_url"]
    with pytest.raises(ValueError, match="base_url"):
        run(registry.add(bad))

    with pytest.raises(ProviderRegistryError, match="already exists"):
        run(registry.add(dict(first)))

    # Neither failed change may reach the file.
    assert [p["name"] for p in read_file(registry_file)["providers"]] == [
        p["name"] for p in default_providers()
    ]


def test_add_update_persist_roundtrip(registry_file):
    registry = ProviderRegistry(
        file_path=registry_file, seed_config={"providers": default_providers()}
    )
    added = run(registry.add(provider_entry(max_context=12345)))
    assert added["max_context"] == 12345

    updated = run(
        registry.update("delta", {"tier": 1, "aliases": ["fresh"]})
    )
    assert updated["tier"] == 1 and updated["aliases"] == ["fresh"]

    stored = {p["name"]: p for p in read_file(registry_file)["providers"]}
    assert stored["delta"]["tier"] == 1
    assert stored["delta"]["max_context"] == 12345


def test_update_rename_forbidden_and_unknown_errors(registry_file):
    registry = ProviderRegistry(
        file_path=registry_file, seed_config={"providers": default_providers()}
    )
    with pytest.raises(ProviderRegistryError, match="Renaming"):
        run(registry.update("alpha", {"name": "renamed"}))
    with pytest.raises(ProviderRegistryError, match="Unknown provider"):
        run(registry.update("ghost", {"tier": 2}))


def test_enable_disable_roundtrip(registry_file):
    registry = ProviderRegistry(
        file_path=registry_file, seed_config={"providers": default_providers()}
    )
    disabled = run(registry.disable("beta"))
    assert disabled.get("enabled") is False

    enabled_again = run(registry.enable("beta"))
    assert "enabled" not in enabled_again  # default-on needs no field

    # The file was rewritten intact by both operations.
    stored_names = [p["name"] for p in read_file(registry_file)["providers"]]
    assert stored_names == [p["name"] for p in default_providers()]


def test_remove_and_routing_reference_guard(registry_file):
    registry = ProviderRegistry(
        file_path=registry_file, seed_config={"providers": default_providers()}
    )
    names = [p["name"] for p in default_providers()]
    run(
        registry.set_routing(
            "chain",
            chain=[{"provider": names[0], "model": "m1"}],
        )
    )
    with pytest.raises(ProviderRegistryError, match="routing.chain"):
        run(registry.remove(names[0]))

    run(registry.set_routing("auto"))
    run(registry.remove(names[0]))
    assert names[0] not in [p["name"] for p in registry.list()]


def test_set_routing_validation(registry_file):
    registry = ProviderRegistry(
        file_path=registry_file, seed_config={"providers": default_providers()}
    )
    run = __import__("asyncio").run
    with pytest.raises(ValueError, match="'mode' must be one of"):
        run(registry.set_routing("chaos"))
    with pytest.raises(ValueError, match="requires a 'pinned' mapping"):
        run(registry.set_routing("pinned"))
    with pytest.raises(ValueError, match="unknown provider"):
        run(registry.set_routing("pinned", pinned={"provider": "ghost", "model": "m"}))
    routing = run(
        registry.set_routing(
            "pinned",
            pinned={
                "provider": default_providers()[0]["name"],
                "model": "some-model",
            },
        )
    )
    assert routing["mode"] == "pinned"


def test_snapshot_isolation(registry_file):
    registry = ProviderRegistry(
        file_path=registry_file, seed_config={"providers": default_providers()}
    )
    snapshot = registry.list()
    snapshot[0]["tier"] = 999
    assert registry.list()[0]["tier"] != 999


def test_connectivity_probe_with_mock_transport(registry_file, monkeypatch):
    registry = ProviderRegistry(
        file_path=registry_file,
        seed_config={"providers": default_providers()},
    )
    first = default_providers()[0]
    monkeypatch.setenv(first["api_key_env"], "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    report = run(
        registry.test(first["name"], client=httpx.AsyncClient(transport=transport))
    )
    assert report["ok"] is True and report["status"] == 200
    assert isinstance(report["latency_ms"], int)


def test_connectivity_probe_reports_upstream_failure(registry_file, monkeypatch):
    registry = ProviderRegistry(
        file_path=registry_file,
        seed_config={"providers": default_providers()},
    )
    first = default_providers()[0]
    monkeypatch.setenv(first["api_key_env"], "test-key")

    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    report = run(
        registry.test(first["name"], client=httpx.AsyncClient(transport=transport))
    )
    assert report["ok"] is False and report["status"] == 403


def test_connectivity_probe_unknown_provider_and_missing_key(
    registry_file, monkeypatch
):
    registry = ProviderRegistry(
        file_path=registry_file,
        seed_config={"providers": default_providers()},
    )
    monkeypatch.delenv(default_providers()[0]["api_key_env"], raising=False)

    unknown = run(registry.test("ghost"))
    assert unknown["ok"] is False and "Unknown" in unknown["detail"]

    no_key = run(registry.test(default_providers()[0]["name"]))
    assert no_key["ok"] is False and "not set" in no_key["detail"]
