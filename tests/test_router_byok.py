# tests/test_router_byok.py
"""Platform Phase 9 PR-C: per-user candidate pool inside the single
failover loop (unit level, hermetic MockTransport).

Gates: a BYOK-scoped request's candidates come ENTIRELY from the passed
list (operator registry hosts never called); zero candidates raise the
clean NoCredentialsConfiguredError; failover still works across the
user's own set; cooldown state is scoped per-credential (same-label
providers never share it); an unusable credential skips like a missing
key; the legacy (no-byok-args) path is byte-for-byte the old behavior.
The request's ``model`` overrides each credential's stored default for
BYOK requests (applied to every failover candidate), while the operator
pool keeps model-as-ordering-hint only.
"""
import json

import httpx
import pytest

from invincible.core.router import NoCredentialsConfiguredError
from tests.conftest import provider_body, sse_body, stream_chunk


def byok_candidate(index, host, name="Mine"):
    return {
        "name": name,
        "tier": index + 1,
        "base_url": f"https://{host}/v1",
        "model_id": f"byok-model-{index}",
        "enabled": True,
        "health_id": f"byok:{index + 1}",
        "byok_credential_id": index + 1,
    }


async def accept_key(provider):
    return f"user-key-{provider['byok_credential_id']}"


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


def capturing(response):
    """Body-capturing MockTransport handler: records the parsed JSON body
    of each request, for asserting which model the gateway actually sent
    upstream. Same response semantics as :func:`counting`."""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    return calls, handler


async def test_byok_request_hits_only_user_providers(make_router):
    op_calls, op_handler = counting(provider_body("alpha"))
    by_calls, by_handler = counting(provider_body("mine"))
    router = make_router(handlers={
        "alpha.example.com": op_handler,
        "byok1.example.com": by_handler,
    })
    result = await router.route_request(
        [{"role": "user", "content": "hi"}],
        byok_candidates=[byok_candidate(0, "byok1.example.com")],
        byok_key_resolver=accept_key,
    )
    assert result["choices"][0]["message"]["content"] == "hello"
    assert by_calls and "byok1.example.com" in by_calls[0]
    assert op_calls == []


async def test_empty_byok_candidates_raise_clean_error(make_router):
    router = make_router(handlers={})
    with pytest.raises(NoCredentialsConfiguredError) as exc_info:
        await router.route_request(
            [{"role": "user", "content": "hi"}],
            byok_candidates=[],
            byok_key_resolver=accept_key,
        )
    # The message tells the user what to do, not that the gateway broke.
    assert "/dashboard/providers" in str(exc_info.value)


async def test_failover_across_user_providers(make_router):
    c1, h1 = counting(httpx.Response(429, json={"error": {}}))
    c2, h2 = counting(httpx.Response(500, json={"error": {}}))
    c3, h3 = counting(provider_body("mine3"))
    router = make_router(handlers={
        "u1.example.com": h1, "u2.example.com": h2, "u3.example.com": h3,
    })
    result = await router.route_request(
        [{"role": "user", "content": "hi"}],
        byok_candidates=[
            byok_candidate(0, "u1.example.com"),
            byok_candidate(1, "u2.example.com"),
            byok_candidate(2, "u3.example.com"),
        ],
        byok_key_resolver=accept_key,
    )
    assert result["choices"][0]["message"]["content"] == "hello"
    assert len(c1) == 1 and len(c2) == 1 and len(c3) == 1


async def test_same_label_credentials_do_not_share_cooldown(make_router):
    """Provider A 429s (cooldown recorded); provider B with the SAME
    display name but a different health_id must still be attempted -
    cooldowns are scoped per credential, not per label."""
    c1, h1 = counting(httpx.Response(429, json={"error": {}}))
    c2, h2 = counting(provider_body("second"))
    router = make_router(handlers={
        "u1.example.com": h1, "u2.example.com": h2,
    })
    result = await router.route_request(
        [{"role": "user", "content": "hi"}],
        byok_candidates=[
            byok_candidate(0, "u1.example.com", name="Same Label"),
            byok_candidate(1, "u2.example.com", name="Same Label"),
        ],
        byok_key_resolver=accept_key,
    )
    assert result["choices"][0]["message"]["content"] == "hello"
    assert len(c1) == 1 and len(c2) == 1


async def test_unresolvable_credential_skips_like_missing_key(make_router):
    async def flaky_resolver(provider):
        if provider["byok_credential_id"] == 1:
            return None  # undecryptable/vanished row shape
        return "k2"

    c1, h1 = counting(provider_body("first"))
    c2, h2 = counting(provider_body("second"))
    router = make_router(handlers={
        "u1.example.com": h1, "u2.example.com": h2,
    })
    result = await router.route_request(
        [{"role": "user", "content": "hi"}],
        byok_candidates=[
            byok_candidate(0, "u1.example.com"),
            byok_candidate(1, "u2.example.com"),
        ],
        byok_key_resolver=flaky_resolver,
    )
    assert result["choices"][0]["message"]["content"] == "hello"
    assert c1 == []  # skipped before any request was made
    assert len(c2) == 1


async def test_legacy_request_uses_operator_pool_unchanged(make_router):
    op_calls, op_handler = counting(provider_body("alpha"))
    by_calls, by_handler = counting(provider_body("mine"))
    router = make_router(handlers={
        "alpha.example.com": op_handler,
        "byok1.example.com": by_handler,
    })
    result = await router.route_request([{"role": "user", "content": "hi"}])
    assert result["choices"][0]["message"]["content"] == "hello"
    assert len(op_calls) == 1
    assert by_calls == []


async def test_streaming_routes_through_user_provider(make_router):
    by_calls, by_handler = counting(httpx.Response(200, text=sse_body(
        stream_chunk("mine", {"content": "hey"}),
        stream_chunk("mine", {}, finish_reason="stop"),
    )))
    router = make_router(handlers={"byok1.example.com": by_handler})
    first, tail = await router.stream_open(
        [{"role": "user", "content": "hi"}],
        byok_candidates=[byok_candidate(0, "byok1.example.com")],
        byok_key_resolver=accept_key,
    )
    chunks = [first] + [chunk async for chunk in tail]
    text = "".join(
        (c["choices"][0]["delta"] or {}).get("content") or ""
        for c in chunks if c and c.get("choices")
    )
    assert "hey" in text
    assert len(by_calls) == 1


# --- per-request model override -------------------------------------------------


async def test_byok_model_override_reaches_upstream_payload(make_router):
    """The request's model wins over the credential's stored default:
    the upstream payload and route info both carry the requested model."""
    calls, handler = capturing(provider_body("mine"))
    router = make_router(handlers={"byok1.example.com": handler})
    result, info = await router.route_request_detailed(
        [{"role": "user", "content": "hi"}],
        model="custom-model-x",
        byok_candidates=[byok_candidate(0, "byok1.example.com")],
        byok_key_resolver=accept_key,
    )
    assert result["choices"][0]["message"]["content"] == "hello"
    assert calls[0]["model"] == "custom-model-x"
    assert info["model_id"] == "custom-model-x"


async def test_byok_without_model_uses_stored_default(make_router):
    """No model in the request = the credential's stored default serves
    it, exactly as before the override existed."""
    calls, handler = capturing(provider_body("mine"))
    router = make_router(handlers={"byok1.example.com": handler})
    _result, info = await router.route_request_detailed(
        [{"role": "user", "content": "hi"}],
        byok_candidates=[byok_candidate(0, "byok1.example.com")],
        byok_key_resolver=accept_key,
    )
    assert calls[0]["model"] == "byok-model-0"
    assert info["model_id"] == "byok-model-0"


async def test_byok_model_override_applies_across_failover(make_router):
    """Apply-to-all: a failover candidate that never saw the requested
    model still gets it (first host 404s, second one serves)."""
    c1, h1 = capturing(httpx.Response(404, json={"error": {}}))
    c2, h2 = capturing(provider_body("second"))
    router = make_router(handlers={
        "u1.example.com": h1, "u2.example.com": h2,
    })
    result, info = await router.route_request_detailed(
        [{"role": "user", "content": "hi"}],
        model="custom-model-x",
        byok_candidates=[
            byok_candidate(0, "u1.example.com"),
            byok_candidate(1, "u2.example.com"),
        ],
        byok_key_resolver=accept_key,
    )
    assert result["choices"][0]["message"]["content"] == "hello"
    assert c1[0]["model"] == "custom-model-x"
    assert c2[0]["model"] == "custom-model-x"
    assert info["model_id"] == "custom-model-x"


async def test_operator_pool_model_hint_does_not_override(make_router):
    """The legacy path keeps its historical semantics: the model hint
    may reorder candidates but never replaces the operator-configured
    model_id in the upstream payload."""
    calls, handler = capturing(provider_body("alpha"))
    router = make_router(handlers={"alpha.example.com": handler})
    _result, info = await router.route_request_detailed(
        [{"role": "user", "content": "hi"}],
        model="custom-model-x",
    )
    assert calls[0]["model"] == "alpha-model"
    assert info["model_id"] == "alpha-model"
