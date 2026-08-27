# tests/test_dual_realm.py
"""Dual-realm authentication acceptance (Platform Phase 1).

Pins the exact resolution order documented in endpoints/auth.py:

1. legacy ``GATEWAY_API_KEY`` -> system local owner;
2. unrevoked API key -> that user + default project;
3. unset gateway key -> fail-open anonymous local identity (behavior
   preserved from the single-tenant era);
4. otherwise 401.

Session placement is the observable: history must land under the owning
user's ownership triple, which is exactly what Phase 2's predicates will
enforce.

Phase 9 update: api_key-realm principals route ONLY through their own
connected credentials (never the operator pool). The keyed-principal
tests below connect one credential on the same mocked host so the
resolution/threading behavior they pin is still observable.
"""
import hashlib

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from invincible.core.credential_store import ByokCredentialStore
from invincible.core.db import (
    LOCAL_OWNER_EMAIL,
    LOCAL_PROJECT_NAME,
)
from invincible.core.identity import ApiKeyStore
from invincible.main import app

GATEWAY_KEY = "test-gateway-key"
AUTH = {"Authorization": f"Bearer {GATEWAY_KEY}"}
CHAT_BODY = {"messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture(autouse=True)
def _byok_env(monkeypatch):
    """Keyed principals need a usable BYOK environment: a master
    credential key and hermetic DNS for the per-attempt URL re-check."""
    import invincible.core.url_safety as url_safety

    monkeypatch.setenv(
        "INVINCIBLE_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(
        url_safety, "_default_resolve", lambda host: ["93.184.216.34"])


def provider_body():
    return {
        "id": "cmpl-x",
        "model": "alpha-model",
        "choices": [
            {"message": {"role": "assistant", "content": "hello"}}
        ],
    }


async def _session_owner(client, session_string: str) -> tuple | None:
    """(email, project_name) owning a client session string, via the
    storage joins - no store internals."""
    engine = app.state.engine
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT u.email, p.name FROM sessions s"
            " JOIN users u ON u.id = s.user_id"
            " JOIN projects p ON p.id = s.project_id"
            " WHERE s.client_session_id = :sid"
        ), {"sid": session_string})).first()
    return (row[0], row[1]) if row else None


async def _mint_key_for_new_user(client, email: str) -> str:
    """Insert a real user + default project, mint one API key, and
    connect one BYOK credential on the standard mock host (Phase 9:
    keyed principals chat only through their own connected providers);
    returns the raw token."""
    engine = app.state.engine
    async with engine.begin() as conn:
        uid = (await conn.execute(text(
            "INSERT INTO users (email, created_at)"
            " VALUES (:e, 1.0) RETURNING id"
        ), {"e": email})).scalar_one()
        await conn.execute(text(
            "INSERT INTO projects (user_id, name, is_default, created_at)"
            " VALUES (:u, 'personal', TRUE, 1.0)"
        ), {"u": uid})
    await ByokCredentialStore(engine).create(
        user_id=int(uid), provider_name="Test Pool",
        model_id="alpha-model",
        base_url="https://alpha.example.com/v1",
        api_key="user-key",
    )
    record = await app.state.api_keys.create(int(uid), label="test")
    return record["raw"]


async def test_legacy_gateway_key_maps_to_local_owner(router_setter, client):
    router_setter({"alpha.example.com": lambda r: __import__(
        "httpx").Response(200, json=provider_body())})
    resp = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Session-Id": "legacy-s"},
        json=CHAT_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert await _session_owner(client, "legacy-s") == (
        LOCAL_OWNER_EMAIL, LOCAL_PROJECT_NAME,
    )


async def test_api_key_resolves_to_its_user(router_setter, client):
    router_setter({"alpha.example.com": lambda r: __import__(
        "httpx").Response(200, json=provider_body())})
    raw = await _mint_key_for_new_user(client, "dev@example.com")
    resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {raw}",
            "X-Session-Id": "keyed-s",
        },
        json=CHAT_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert await _session_owner(client, "keyed-s") == (
        "dev@example.com", "personal",
    )


async def test_same_session_string_is_isolated_across_principals(
    router_setter, client,
):
    """The Phase 2 groundwork proof: two principals using the SAME client
    string get DISTINCT session rows - no cross-user bleed."""
    router_setter({"alpha.example.com": lambda r: __import__(
        "httpx").Response(200, json=provider_body())})
    raw = await _mint_key_for_new_user(client, "other@example.com")

    for headers in (
        {**AUTH, "X-Session-Id": "shared"},
        {"Authorization": f"Bearer {raw}", "X-Session-Id": "shared"},
    ):
        resp = await client.post(
            "/v1/chat/completions", headers=headers, json=CHAT_BODY,
        )
        assert resp.status_code == 200, resp.text

    engine = app.state.engine
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT u.email FROM sessions s"
            " JOIN users u ON u.id = s.user_id"
            " WHERE s.client_session_id = 'shared'"
            " ORDER BY u.email"
        ))).all()
    assert [r[0] for r in rows] == [
        LOCAL_OWNER_EMAIL, "other@example.com",
    ]


async def test_unknown_token_is_401_when_gateway_key_set(
    router_setter, client,
):
    router_setter({})
    for headers in (
        {"Authorization": "Bearer garbage"},
        {"x-api-key": "garbage"},
        {},  # missing token entirely
    ):
        resp = await client.post("/v1/chat/completions", headers=headers,
                                 json=CHAT_BODY)
        assert resp.status_code == 401, headers


async def test_revoked_api_key_is_401(router_setter, client):
    router_setter({})
    raw = await _mint_key_for_new_user(client, "gone@example.com")
    keys = await app.state.api_keys.list()
    await app.state.api_keys.revoke(keys[0]["id"])
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw}"},
        json=CHAT_BODY,
    )
    assert resp.status_code == 401


async def test_fail_open_local_mode_preserved(router_setter, client,
                                              monkeypatch):
    """Unset GATEWAY_API_KEY keeps the documented single-tenant behavior:
    requests succeed and land under the local owner as `anonymous`."""
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    router_setter({"alpha.example.com": lambda r: __import__(
        "httpx").Response(200, json=provider_body())})

    for headers in ({}, {"Authorization": "Bearer whatever"}):
        resp = await client.post(
            "/v1/chat/completions",
            headers={**headers, "X-Session-Id": "open-s"},
            json=CHAT_BODY,
        )
        assert resp.status_code == 200, (headers, resp.text)
    assert await _session_owner(client, "open-s") == (
        LOCAL_OWNER_EMAIL, LOCAL_PROJECT_NAME,
    )


async def test_fail_open_still_prefers_valid_api_keys(router_setter, client,
                                                      monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    router_setter({"alpha.example.com": lambda r: __import__(
        "httpx").Response(200, json=provider_body())})
    raw = await _mint_key_for_new_user(client, "openmode@example.com")
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw}",
                 "X-Session-Id": "open-keyed"},
        json=CHAT_BODY,
    )
    assert resp.status_code == 200
    assert await _session_owner(client, "open-keyed") == (
        "openmode@example.com", "personal",
    )


async def test_cross_realm_precedence_legacy_wins(router_setter, client):
    """The unambiguity pin: an API-key row whose sha256 equals the gateway
    key itself. Step 1 must win - the request resolves to the system local
    owner, never to the colliding key's user."""
    router_setter({"alpha.example.com": lambda r: __import__(
        "httpx").Response(200, json=provider_body())})
    await _mint_key_for_new_user(client, "collider@example.com")

    # Re-point the minted key's hash at the GATEWAY key's value.
    gateway_hash = hashlib.sha256(GATEWAY_KEY.encode()).hexdigest()
    engine = app.state.engine
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE api_keys SET key_hash = :h"
        ), {"h": gateway_hash})

    resp = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Session-Id": "collision-s"},
        json=CHAT_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert await _session_owner(client, "collision-s") == (
        LOCAL_OWNER_EMAIL, LOCAL_PROJECT_NAME,
    )


async def test_anthropic_endpoint_threads_principal(router_setter, client):
    from tests.conftest import sse_body, stream_chunk

    def alpha_stream(request):
        return __import__("httpx").Response(
            200,
            content=sse_body(stream_chunk("alpha", {"content": "yo"},
                                          finish_reason="stop")),
            headers={"content-type": "text/event-stream"},
        )

    router_setter({"alpha.example.com": alpha_stream})
    raw = await _mint_key_for_new_user(client, "claude@example.com")
    resp = await client.post(
        "/v1/messages?beta=true",
        headers={
            "Authorization": f"Bearer {raw}",
            "X-Session-Id": "anthropic-keyed",
        },
        json={
            "model": "claude-sonnet-4",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert await _session_owner(client, "anthropic-keyed") == (
        "claude@example.com", "personal",
    )


async def test_api_key_store_round_trip_via_fixture(client):
    """Sanity on fixture wiring: app.state.api_keys targets the same
    database the client fixture truncates between tests."""
    store = app.state.api_keys
    assert isinstance(store, ApiKeyStore)
