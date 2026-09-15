# tests/test_user_routing.py
"""Phase 1 self-service routing: the pure mapping layer and the stores.

``routing_config_from_user`` / ``chain_with_model_hint`` are pinned pure
(auto default, malformed JSON degrades to auto, chain mapping with drift
skip, model-hint float). ``ByokCredentialStore.reorder`` and
``UserSettingsStore`` are pinned on the real database: the order write
requires the exact owned id set, settings round-trip through the upsert,
and one user's routing/settings rows never influence another user's.
"""
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from invincible.core.credential_store import (
    ByokCredentialStore,
    UnknownCredentialError,
)
from invincible.core.selection import (
    AUTO_ROUTING,
    chain_with_model_hint,
)
from invincible.core.user_settings_store import (
    UserSettingsStore,
    routing_config_from_user,
)

ROWS = [
    {"id": 1, "provider_name": "One", "model_id": "m1"},
    {"id": 2, "provider_name": "Two", "model_id": "m2"},
]


# --- chain_with_model_hint (pure) -----------------------------------------------


def test_hint_floats_matching_step_to_front():
    chain = [
        {"credential_id": 1, "model": "glm"},
        {"credential_id": 2, "model": "kimi"},
        {"credential_id": 3, "model": "mistral"},
    ]
    floated = chain_with_model_hint(chain, "kimi")
    assert [s["model"] for s in floated] == ["kimi", "glm", "mistral"]


def test_hint_floats_duplicates_together_in_order():
    chain = [
        {"credential_id": 1, "model": "glm"},
        {"credential_id": 2, "model": "kimi"},
        {"credential_id": 3, "model": "glm"},
    ]
    floated = chain_with_model_hint(chain, "glm")
    assert [s["credential_id"] for s in floated] == [1, 3, 2]


def test_hint_without_match_returns_input_order():
    chain = [
        {"credential_id": 1, "model": "glm"},
        {"credential_id": 2, "model": "kimi"},
    ]
    assert chain_with_model_hint(chain, "unknown-model") == chain


def test_hint_none_or_empty_model_is_a_no_op():
    chain = [{"credential_id": 1, "model": "glm"}]
    assert chain_with_model_hint(chain, None) == chain
    assert chain_with_model_hint(chain, "") == chain


def test_hint_never_mutates_input():
    chain = [
        {"credential_id": 1, "model": "glm"},
        {"credential_id": 2, "model": "kimi"},
    ]
    chain_with_model_hint(chain, "kimi")
    assert chain == [
        {"credential_id": 1, "model": "glm"},
        {"credential_id": 2, "model": "kimi"},
    ]


# --- routing_config_from_user (pure) --------------------------------------------


def test_routing_config_empty_and_malformed_degrade_to_auto():
    assert routing_config_from_user({}, ROWS) == AUTO_ROUTING
    assert routing_config_from_user(None, ROWS) == AUTO_ROUTING
    assert routing_config_from_user("garbage", ROWS) == AUTO_ROUTING
    assert routing_config_from_user(["chain"], ROWS) == AUTO_ROUTING
    # Unknown mode is not a routing mode at all.
    assert routing_config_from_user({"mode": "weird"}, ROWS) == AUTO_ROUTING


def test_routing_config_chain_maps_credentials_and_skips_drift():
    routing = routing_config_from_user({
        "mode": "chain",
        "chain": [
            {"credential_id": 2, "model": "kimi"},
            # Drifted: credential 99 is not (or no longer) the user's.
            {"credential_id": 99, "model": "gone"},
            {"credential_id": 1, "model": "glm"},
        ],
    }, ROWS)
    assert routing.mode == "chain"
    assert [(s.provider, s.model) for s in routing.chain] == [
        ("Two", "kimi"), ("One", "glm")]


def test_routing_config_all_drift_chain_degrades_to_auto():
    routing = routing_config_from_user({
        "mode": "chain",
        "chain": [{"credential_id": 99, "model": "gone"}],
    }, ROWS)
    assert routing == AUTO_ROUTING


def test_routing_config_malformed_chain_never_raises_with_model():
    """A corrupt stored chain (non-dict steps) plus a model hint must
    degrade to auto, not raise on the request path."""
    routing = routing_config_from_user(
        {"mode": "chain", "chain": ["not a dict", 42, None]}, ROWS,
        model="kimi")
    assert routing == AUTO_ROUTING


def test_routing_config_malformed_steps_are_dropped():
    routing = routing_config_from_user({
        "mode": "chain",
        "chain": [
            "not a dict",
            {"credential_id": "1", "model": "string id"},
            {"credential_id": 1},  # no model
            {"credential_id": 1, "model": "  "},  # blank model
            {"credential_id": 1, "model": "glm"},
        ],
    }, ROWS)
    assert [(s.provider, s.model) for s in routing.chain] == [("One", "glm")]


def test_routing_config_chain_without_list_degrades_to_auto():
    assert routing_config_from_user(
        {"mode": "chain", "chain": "not-a-list"}, ROWS) == AUTO_ROUTING


def test_routing_config_pinned_maps_credential():
    routing = routing_config_from_user({
        "mode": "pinned",
        "pinned": {"credential_id": 2, "model": "flash"},
    }, ROWS)
    assert routing.mode == "pinned"
    assert routing.pinned.provider == "Two"
    assert routing.pinned.model == "flash"


def test_routing_config_pinned_drift_degrades_to_auto():
    assert routing_config_from_user({
        "mode": "pinned",
        "pinned": {"credential_id": 99, "model": "flash"},
    }, ROWS) == AUTO_ROUTING


def test_routing_config_applies_model_hint_to_chain():
    """The request's model floats the matching step to the front - the
    client's /model choice still picks the chain's entry point."""
    routing = routing_config_from_user({
        "mode": "chain",
        "chain": [
            {"credential_id": 1, "model": "glm"},
            {"credential_id": 2, "model": "kimi"},
        ],
    }, ROWS, model="kimi")
    assert [s.provider for s in routing.chain] == ["Two", "One"]
    # The hint reorders steps but never rewrites a step's own model.
    assert [s.model for s in routing.chain] == ["kimi", "glm"]


# --- stores (real database) -------------------------------------------------------


@pytest.fixture(autouse=True)
def credential_key(monkeypatch):
    monkeypatch.setenv(
        "INVINCIBLE_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))


async def _make_user(engine, email: str) -> int:
    async with engine.begin() as conn:
        return int((await conn.execute(text(
            "INSERT INTO users (email, created_at)"
            " VALUES (:e, 1.0) RETURNING id"
        ), {"e": email})).scalar_one())


async def _connect(engine, uid, name):
    return await ByokCredentialStore(engine).create(
        user_id=uid, provider_name=name, model_id=f"{name.lower()}-model",
        base_url=f"https://{name.lower()}.example.com/v1",
        api_key=f"key-{name.lower()}")


async def test_reorder_applies_the_new_order(pg_engine):
    uid = await _make_user(pg_engine, "reorder@example.com")
    a = await _connect(pg_engine, uid, "Aaa")
    b = await _connect(pg_engine, uid, "Bbb")
    c = await _connect(pg_engine, uid, "Ccc")
    await ByokCredentialStore(pg_engine).reorder(
        uid, [c["id"], a["id"], b["id"]])
    rows = await ByokCredentialStore(pg_engine).list_for_user(uid)
    assert [r["provider_name"] for r in rows] == ["Ccc", "Aaa", "Bbb"]


async def test_reorder_requires_the_exact_owned_id_set(pg_engine):
    uid_a = await _make_user(pg_engine, "strict-a@example.com")
    uid_b = await _make_user(pg_engine, "strict-b@example.com")
    a = await _connect(pg_engine, uid_a, "Aaa")
    b = await _connect(pg_engine, uid_a, "Bbb")
    foreign = await _connect(pg_engine, uid_b, "Fff")
    store = ByokCredentialStore(pg_engine)

    with pytest.raises(UnknownCredentialError):
        await store.reorder(uid_a, [])  # partial: dropped one of their own
    with pytest.raises(UnknownCredentialError):
        await store.reorder(uid_a, [a["id"], foreign["id"]])  # foreign id
    with pytest.raises(UnknownCredentialError):
        await store.reorder(uid_a, [a["id"], b["id"], foreign["id"]])
    with pytest.raises(UnknownCredentialError):
        await store.reorder(uid_b, [a["id"], b["id"]])  # someone else's set

    # Nothing was written by the rejected attempts.
    rows = await store.list_for_user(uid_a)
    assert [r["provider_name"] for r in rows] == ["Aaa", "Bbb"]


async def test_settings_round_trip_through_the_upsert(pg_engine):
    uid = await _make_user(pg_engine, "settings@example.com")
    store = UserSettingsStore(pg_engine)
    assert await store.get(uid) == {"routing": {}, "overrides": {}}

    chain = [{"credential_id": 7, "model": "kimi"}]
    await store.save_routing(uid, {"mode": "chain", "chain": chain})
    assert await store.routing_for(uid) == {
        "mode": "chain", "chain": chain}

    await store.save_overrides(uid, {"memory": False})
    assert await store.overrides_for(uid) == {"memory": False}
    # The overrides upsert must not clobber the routing column.
    assert (await store.routing_for(uid))["mode"] == "chain"
    # And a routing re-save must not clobber the overrides column.
    await store.save_routing(uid, {"mode": "auto"})
    assert await store.overrides_for(uid) == {"memory": False}
    assert await store.routing_for(uid) == {"mode": "auto"}


async def test_settings_and_routing_are_isolated_per_user(pg_engine):
    """One user's stored routing/settings never influence another user:
    B's config builds from B's rows, and a chain step referencing A's
    credential id (stale copy, drift) is silently dropped for B."""
    uid_a = await _make_user(pg_engine, "iso-a@example.com")
    uid_b = await _make_user(pg_engine, "iso-b@example.com")
    a_cred = await _connect(pg_engine, uid_a, "Aaa")
    b_cred = await _connect(pg_engine, uid_b, "Bbb")

    settings = UserSettingsStore(pg_engine)
    await settings.save_routing(uid_a, {
        "mode": "chain",
        "chain": [{"credential_id": a_cred["id"], "model": "glm"}]})
    await settings.save_overrides(uid_a, {"memory": False})

    # B starts clean regardless of A's rows.
    assert await settings.get(uid_b) == {"routing": {}, "overrides": {}}

    # B's routing build sees only B's credentials: A's step id drifts.
    b_rows = [{"id": b_cred["id"], "provider_name": "Bbb",
               "model_id": "bbb-model"}]
    routing = routing_config_from_user({
        "mode": "chain",
        "chain": [
            {"credential_id": a_cred["id"], "model": "glm"},  # A's row
            {"credential_id": b_cred["id"], "model": "kimi"},
        ],
    }, b_rows)
    assert [(s.provider, s.model) for s in routing.chain] == [("Bbb", "kimi")]
