# invincible/core/selection.py
"""Routing-mode selection: automatic / pinned / fallback-chain
(Phase 13.5).

Pure decision logic over a provider snapshot - no I/O, no HTTP. The
Router calls :func:`attempt_order` once per request to obtain its
candidate list; cooldown and missing-API-key skipping deliberately stay
inside the Router's attempt loop so timing and logging are identical in
every mode.

Semantics:

- **auto** - tier-sorted providers, optionally reordered by a soft alias
  preference from the client's ``model`` hint (the historical behavior).
- **pinned** - exactly one provider with one exact model id. Any failure
  surfaces verbatim to the caller; there is no silent substitution.
- **chain** - the listed provider/model pairs, in order, as an explicit
  fallback chain. Runtime drift (a chain entry whose provider was later
  disabled) simply skips that entry; save-time validation guarantees the
  entries existed when configured.

Candidates carry their final ``model_id`` - pinned/chain entries are
copies with ``model_id`` overridden - so downstream code (payload build,
logging, run records) needs no mode awareness.
"""
import copy
from dataclasses import dataclass


class PinnedUnavailableError(Exception):
    """A pinned target is not configured or is disabled. The Router turns
    this into AllProvidersFailedError so clients see a normal gateway
    failure instead of an internal error."""


@dataclass(frozen=True)
class PinnedRoute:
    provider: str
    model: str


@dataclass(frozen=True)
class RoutingConfig:
    mode: str = "auto"
    pinned: PinnedRoute | None = None
    chain: tuple[PinnedRoute, ...] = ()


AUTO_ROUTING = RoutingConfig()


def routing_from_config(config: dict) -> RoutingConfig:
    """Build typed routing from the registry's stored ``routing`` block."""
    mode = config.get("mode", "auto")
    pinned = None
    chain: tuple[PinnedRoute, ...] = ()
    if mode == "pinned" and config.get("pinned"):
        step = config["pinned"]
        pinned = PinnedRoute(step["provider"], step["model"])
    elif mode == "chain":
        chain = tuple(
            PinnedRoute(step["provider"], step["model"])
            for step in config.get("chain") or []
        )
    return RoutingConfig(mode=mode, pinned=pinned, chain=chain)


def attempt_order(
    providers: list[dict],
    health_tracker,
    routing: RoutingConfig,
    model_hint: str | None,
) -> list[dict]:
    """Ordered ``(provider, final_model_id-on-dict)`` candidates.

    ``health_tracker`` is accepted for interface symmetry today (candidate
    pruning by cooldown lives in the Router loop); disabled-via-registry
    providers are filtered here because they are a configuration fact, not
    a transient health state.
    """
    available = [p for p in providers if p.get("enabled", True)]

    if routing.mode == "pinned":
        target = next(
            (p for p in available if p["name"] == routing.pinned.provider), None
        )
        if target is None:
            raise PinnedUnavailableError(
                f"Pinned provider '{routing.pinned.provider}' is not "
                "configured or is disabled"
            )
        candidate = copy.deepcopy(target)
        candidate["model_id"] = routing.pinned.model
        return [candidate]

    if routing.mode == "chain":
        candidates = []
        for step in routing.chain:
            source = next((p for p in available if p["name"] == step.provider), None)
            if source is None:
                continue
            candidate = copy.deepcopy(source)
            candidate["model_id"] = step.model
            candidates.append(candidate)
        return candidates

    # auto
    ordered = sorted(available, key=lambda p: p["tier"])
    if model_hint:
        preferred = [
            p
            for p in ordered
            if model_hint == p.get("model_id")
            or model_hint in (p.get("aliases") or [])
        ]
        rest = [p for p in ordered if p not in preferred]
        ordered = preferred + rest
    return ordered
