# tests/test_selection_policy.py
"""Selection-policy unit tests (Phase 13.5): auto / pinned / chain.

These exercise attempt_order directly over synthetic snapshots. The
router-integration tests that used the operator registry retired with it
(Phase 2): pinned/chain routing now exists only as per-user BYOK routing,
covered end-to-end by test_chat_byok.py's chain test, and the static-YAML
router the tests construct is auto-only.
"""
import pytest

from invincible.core.selection import (
    AUTO_ROUTING,
    PinnedUnavailableError,
    attempt_order,
    routing_from_config,
)

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
