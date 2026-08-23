# invincible/core/provider_registry.py
"""Provider configuration as mutable domain state (Phase 13.5).

The Registry owns the provider list and the routing block; the Router is a
consumer that takes a snapshot per request. Two modes:

- ``file_path`` set: that YAML file is authoritative and writable. If it
  does not exist yet it is seeded atomically from ``seed_config``
  (copy-on-first-use), so enabling management only means setting
  INVINCIBLE_PROVIDERS_FILE.
- ``file_path`` None: read-only in-memory mode over ``seed_config``
  (the packaged providers.yaml). Every mutation raises
  :class:`ProviderRegistryError` - the packaged copy is never rewritten,
  and without an explicit file there is nothing safe to write to.

Readers receive deep-copied snapshots; mutations build a new list and swap
it in under an asyncio lock, so in-flight requests observe a consistent
world and are never affected mid-flight. Persistence is atomic
(tmp file + ``os.replace``) and best-effort failures propagate to the
caller so an admin API can report them.
"""
import asyncio
import copy
import logging
import os
import time

import httpx
import yaml

from invincible.core.config import (
    auth_headers,
    auth_params,
    resolve_timeout,
    validate_providers_config,
    validate_routing_config,
)
from invincible.core.settings import settings

logger = logging.getLogger("invincible.registry")

DEFAULT_MODELS_PATH = "/models"


class ProviderRegistryError(Exception):
    """Registry misuse: mutation in read-only mode, unknown provider,
    invalid entry, or a rename/removal that would break references."""


def _empty_config() -> dict:
    return {"providers": []}


class ProviderRegistry:
    def __init__(self, file_path: str | None = None, seed_config: dict | None = None):
        self.file_path = file_path
        self._lock = asyncio.Lock()
        if file_path is None:
            source = copy.deepcopy(seed_config or _empty_config())
            self._providers = source.get("providers", [])
            self._routing = source.get("routing") or {"mode": "auto"}
            self._validate_state(self._routing)
            return

        if os.path.isfile(file_path):
            self._load_file(file_path)
        else:
            # Copy-on-first-use: seed the operator's file from whatever the
            # app was started with (packaged config or explicit override).
            source = copy.deepcopy(seed_config or _empty_config())
            self._providers = source.get("providers", [])
            self._routing = source.get("routing") or {"mode": "auto"}
            self._validate_state(self._routing)
            self._persist()

    # ------------------------------------------------------------------
    # Loading / validation / persistence

    def _load_file(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        try:
            config = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValueError(f"Malformed provider registry file {path}: {exc}") from exc
        if not isinstance(config, dict):
            raise ValueError(f"Provider registry file {path} must be a YAML mapping")
        self._providers = config.get("providers", [])
        self._routing = config.get("routing") or {"mode": "auto"}
        self._validate_state(self._routing)

    def _validate_state(self, routing: dict) -> None:
        validate_providers_config({"providers": self._providers})
        validate_routing_config(routing, {p["name"] for p in self._providers})

    def _persist(self) -> None:
        """Atomic whole-file rewrite. Caller holds the lock."""
        data = {"providers": self._providers, "routing": self._routing}
        tmp_path = f"{self.file_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.replace(tmp_path, self.file_path)

    def _find(self, name: str) -> dict | None:
        return next((p for p in self._providers if p["name"] == name), None)

    async def _mutate(self, mutate) -> None:
        """Run ``mutate(working_copy)`` against a copy, validate, swap, persist.

        Read-only mode (no file) refuses every mutation. ``mutate`` may
        raise ProviderRegistryError/ValueError to abort with no state
        change. The swap only happens after validation passes.
        """
        if self.file_path is None:
            raise ProviderRegistryError(
                "This registry is read-only (packaged configuration). Set "
                "INVINCIBLE_PROVIDERS_FILE to enable provider management."
            )
        async with self._lock:
            working = [copy.deepcopy(p) for p in self._providers]
            routing = copy.deepcopy(self._routing)
            mutate(working, routing)
            validate_providers_config({"providers": working})
            validate_routing_config(routing, {p["name"] for p in working})
            self._providers = working
            self._routing = routing
            try:
                self._persist()
            except Exception as exc:
                logger.error(
                    "Failed to persist provider registry to %s: %s",
                    self.file_path, exc,
                )
                raise ProviderRegistryError(
                    f"Change validated but could not be persisted to "
                    f"{self.file_path}: {exc}"
                ) from exc

    # ------------------------------------------------------------------
    # Reads (lock-free: swaps replace whole structures)

    def list(self) -> list[dict]:
        """Deep-copy snapshot of all providers, tier-sorted."""
        providers = copy.deepcopy(self._providers)
        providers.sort(key=lambda p: p["tier"])
        return providers

    def get(self, name: str) -> dict | None:
        provider = self._find(name)
        return copy.deepcopy(provider) if provider else None

    def routing(self) -> dict:
        return copy.deepcopy(self._routing)

    # ------------------------------------------------------------------
    # Mutations

    async def add(self, entry: dict) -> dict:
        if not isinstance(entry, dict):
            raise ProviderRegistryError("Provider entry must be a mapping")

        def mutate(working, _routing):
            if any(p["name"] == entry.get("name") for p in working):
                raise ProviderRegistryError(
                    f"Provider '{entry.get('name')}' already exists"
                )
            working.append(copy.deepcopy(entry))

        await self._mutate(mutate)
        return self.get(entry["name"])

    async def update(self, name: str, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ProviderRegistryError("Update patch must be a mapping")
        if "name" in patch and patch["name"] != name:
            raise ProviderRegistryError(
                "Renaming a provider is not supported; remove and re-add it"
            )

        def mutate(working, _routing):
            target = next((p for p in working if p["name"] == name), None)
            if target is None:
                raise ProviderRegistryError(f"Unknown provider '{name}'")
            target.update(copy.deepcopy(patch))

        await self._mutate(mutate)
        return self.get(name)

    async def remove(self, name: str) -> None:
        def mutate(working, routing):
            if not any(p["name"] == name for p in working):
                raise ProviderRegistryError(f"Unknown provider '{name}'")
            referenced = []
            if routing.get("mode") == "pinned" and routing.get("pinned", {}).get(
                "provider"
            ) == name:
                referenced.append("routing.pinned")
            for index, step in enumerate(routing.get("chain") or []):
                if step.get("provider") == name:
                    referenced.append(f"routing.chain[{index}]")
            if referenced:
                raise ProviderRegistryError(
                    f"Provider '{name}' is referenced by {', '.join(referenced)}; "
                    "update the routing configuration first"
                )
            working[:] = [p for p in working if p["name"] != name]

        await self._mutate(mutate)

    async def set_enabled(self, name: str, enabled: bool) -> dict:
        def mutate(working, _routing):
            target = next((p for p in working if p["name"] == name), None)
            if target is None:
                raise ProviderRegistryError(f"Unknown provider '{name}'")
            if enabled:
                target.pop("enabled", None)
            else:
                target["enabled"] = False

        await self._mutate(mutate)
        return self.get(name)

    async def enable(self, name: str) -> dict:
        return await self.set_enabled(name, True)

    async def disable(self, name: str) -> dict:
        return await self.set_enabled(name, False)

    async def set_routing(
        self,
        mode: str = "auto",
        pinned: dict | None = None,
        chain: list[dict] | None = None,
    ) -> dict:
        """Replace the routing block wholesale (PUT semantics): switching
        modes never leaves stale pinned/chain blocks behind."""

        def mutate(_working, routing):
            routing.clear()
            routing["mode"] = mode
            if pinned is not None:
                routing["pinned"] = copy.deepcopy(pinned)
            if chain is not None:
                routing["chain"] = copy.deepcopy(chain)

        await self._mutate(mutate)
        return self.routing()

    # ------------------------------------------------------------------
    # Connectivity probe

    async def test(self, name: str, client: httpx.AsyncClient | None = None) -> dict:
        """Probe a provider's models endpoint with its real credentials.

        A read-only GET - no tokens are generated, so testing burns no
        quota. Never raises for an unhealthy provider; the report carries
        the outcome instead.
        """
        provider = self.get(name)
        if provider is None:
            return {
                "ok": False, "status": None, "latency_ms": None,
                "detail": f"Unknown provider '{name}'",
            }
        api_key = settings.provider_api_key(provider["api_key_env"])
        if not api_key:
            return {
                "ok": False, "status": None, "latency_ms": None,
                "detail": f"API key env '{provider['api_key_env']}' is not set",
            }

        url = f"{provider['base_url']}{DEFAULT_MODELS_PATH}"
        owns_client = client is None
        client = client or httpx.AsyncClient()
        started = time.monotonic()
        try:
            resp = await client.get(
                url,
                headers=auth_headers(provider, api_key),
                params=auth_params(provider, api_key),
                timeout=resolve_timeout(provider),
            )
            latency_ms = round((time.monotonic() - started) * 1000)
            detail = "" if resp.status_code == 200 else f"HTTP {resp.status_code}"
            return {
                "ok": resp.status_code == 200,
                "status": resp.status_code,
                "latency_ms": latency_ms,
                "detail": detail,
            }
        except httpx.RequestError as e:
            latency_ms = round((time.monotonic() - started) * 1000)
            return {
                "ok": False, "status": None, "latency_ms": latency_ms,
                "detail": type(e).__name__,
            }
        finally:
            if owns_client:
                await client.aclose()
