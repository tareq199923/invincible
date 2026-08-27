# tests/test_chat_byok.py
"""Platform Phase 9 PR-C: the BYOK routing split at the chat endpoints.

A registered user's inv_ API key (api_key realm) routes ONLY through
that user's connected credentials - the operator's shared registry
providers are provably never called (counting MockTransport handlers).
Zero connected credentials fail fast with a clean 400. The legacy
gateway-key flow is completely unaffected. Both /v1/chat/completions
and /v1/messages are covered.
"""
import httpx
import pytest
from cryptography.fernet import Fernet

from invincible.core.credential_store import ByokCredentialStore
from invincible.core.identity import ApiKeyStore
from invincible.main import app
from tests.conftest import (
    provider_body,
    register_account,
    sse_body,
    stream_chunk,
)

OPERATOR_HOSTS = ("alpha.example.com", "beta.example.com", "gamma.example.com")
U1, U2, U3 = "u1.example.com", "u2.example.com", "u3.example.com"


@pytest.fixture(autouse=True)
def credential_key(monkeypatch):
    # Realistic operator config: the master key is configured, so the
    # fail-closed gate passes and encryption works in store.create.
    monkeypatch.setenv(
        "INVINCIBLE_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """The per-attempt key resolver re-validates stored URLs; fake DNS so
    the mock hosts never hit a real resolver."""
    import invincible.core.url_safety as url_safety

    monkeypatch.setattr(
        url_safety, "_default_resolve", lambda host: ["93.184.216.34"])


def counting(response):
    """Counting MockTransport handler: records URLs, serves ``response``
    (an httpx.Response as-is, or a dict wrapped as a 200 JSON body)."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    return calls, handler


def transport_handlers():
    """Counting handlers for operator + BYOK mock hosts. Tests override
    individual entries to inject failure statuses."""
    handlers = {}
    counters = {}
    for host in (*OPERATOR_HOSTS, U1, U2, U3):
        calls, handler = counting(provider_body(host.split(".")[0]))
        handlers[host] = handler
        counters[host] = calls
    return handlers, counters


async def byok_user(client, email, credential_count=1):
    """Register an account, mint its inv_ API key, and connect N mock
    providers (u1..uN) through the real encrypted store."""
    registered, _ = await register_account(client, email)
    uid = registered.json()["id"]
    key = await ApiKeyStore(app.state.engine).create(uid, label="t")
    store = ByokCredentialStore(app.state.engine)
    for i in range(credential_count):
        await store.create(
            user_id=uid,
            provider_name=f"Mine{i + 1}",
            model_id=f"u{i + 1}-model",
            base_url=f"https://u{i + 1}.example.com/v1",
            api_key=f"user-key-{i + 1}",
        )
    return uid, key["raw"]


def chat_headers(raw_key):
    return {"Authorization": f"Bearer {raw_key}"}


CHAT_BODY = {"messages": [{"role": "user", "content": "hi"}]}


def assert_operator_pool_untouched(counters):
    for host in OPERATOR_HOSTS:
        assert counters[host] == [], f"operator host {host} was called"


async def _real_byok_chat(client, raw_key, **overrides):
    body = {**CHAT_BODY, "model": "u1-model", **overrides}
    return await client.post(
        "/v1/chat/completions", json=body, headers=chat_headers(raw_key))


async def test_byok_chat_routes_only_through_user_providers(
    client, router_setter
):
    handlers, counters = transport_handlers()
    router_setter(handlers)
    _uid, raw = await byok_user(client, "chat2@example.com",
                                credential_count=1)
    resp = await _real_byok_chat(client, raw)
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "hello"
    assert resp.headers["x-invincible-provider"] == "Mine1"
    assert_operator_pool_untouched(counters)
    assert len(counters[U1]) == 1


async def test_zero_credentials_fail_fast_clean_400(client, router_setter):
    handlers, counters = transport_handlers()
    router_setter(handlers)
    registered, _ = await register_account(client, "empty@example.com")
    key = await ApiKeyStore(app.state.engine).create(
        registered.json()["id"], label="t")

    resp = await _real_byok_chat(client, key["raw"])
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "No AI provider" in error["message"]
    assert "/dashboard/providers" in error["message"]
    assert error["type"] == "invalid_request_error"
    assert_operator_pool_untouched(counters)


async def test_sole_provider_401_fails_clean_without_operator_fallback(
    client, router_setter
):
    handlers, counters = transport_handlers()
    c401, h401 = counting(httpx.Response(401, json={"error": {}}))
    handlers[U1] = h401
    counters[U1] = c401
    router_setter(handlers)
    _uid, raw = await byok_user(client, "down2@example.com",
                                credential_count=1)
    resp = await _real_byok_chat(client, raw)
    assert resp.status_code == 503, resp.text
    assert_operator_pool_untouched(counters)
    assert len(counters[U1]) == 1  # tried exactly once, then clean failure


async def test_failover_across_user_providers(client, router_setter):
    handlers, counters = transport_handlers()
    c429, h429 = counting(httpx.Response(429, json={"error": {}}))
    handlers[U1] = h429
    counters[U1] = c429
    c500, h500 = counting(httpx.Response(500, json={"error": {}}))
    handlers[U2] = h500
    counters[U2] = c500
    router_setter(handlers)
    _uid, raw = await byok_user(client, "fo@example.com", credential_count=3)

    resp = await _real_byok_chat(client, raw)
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-invincible-provider"] == "Mine3"
    assert len(counters[U1]) == 1 and len(counters[U2]) == 1
    assert len(counters[U3]) == 1
    assert_operator_pool_untouched(counters)


async def test_legacy_gateway_key_completely_unaffected(client, router_setter):
    handlers, counters = transport_handlers()
    router_setter(handlers)
    resp = await client.post(
        "/v1/chat/completions", json=CHAT_BODY,
        headers={"Authorization": "Bearer test-gateway-key"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-invincible-provider"] == "alpha"
    assert len(counters[OPERATOR_HOSTS[0]]) == 1
    for host in (U1, U2, U3):
        assert counters[host] == []


async def test_streaming_routes_through_user_provider(client, router_setter):
    handlers, counters = transport_handlers()
    calls, handler = counting(httpx.Response(200, text=sse_body(
        stream_chunk("u1", {"content": "hey"}),
        stream_chunk("u1", {}, finish_reason="stop"),
    )))
    handlers[U1] = handler
    counters[U1] = calls
    router_setter(handlers)
    _uid, raw = await byok_user(client, "streamy@example.com",
                                credential_count=1)

    resp = await _real_byok_chat(client, raw, stream=True)
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers["content-type"]
    assert "hey" in resp.text
    assert_operator_pool_untouched(counters)
    assert len(counters[U1]) == 1


async def test_anthropic_messages_routes_through_user_provider(
    client, router_setter
):
    handlers, counters = transport_handlers()
    router_setter(handlers)
    _uid, raw = await byok_user(client, "anth@example.com",
                                credential_count=1)
    resp = await client.post(
        "/v1/messages",
        json={"model": "u1-model", "max_tokens": 16,
              "messages": [{"role": "user", "content": "hi"}]},
        headers=chat_headers(raw))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "hello"
    assert_operator_pool_untouched(counters)


async def test_anthropic_zero_credentials_fail_fast_clean_400(
    client, router_setter
):
    handlers, counters = transport_handlers()
    router_setter(handlers)
    registered, _ = await register_account(client, "anthempty@example.com")
    key = await ApiKeyStore(app.state.engine).create(
        registered.json()["id"], label="t")
    resp = await client.post(
        "/v1/messages",
        json={"model": "anything", "max_tokens": 16,
              "messages": [{"role": "user", "content": "hi"}]},
        headers=chat_headers(key["raw"]))
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "No AI provider" in body["error"]["message"]
    assert_operator_pool_untouched(counters)
