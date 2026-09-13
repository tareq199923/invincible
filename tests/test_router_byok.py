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
pool keeps model-as-ordering-hint only. Under that override, a 400 whose
body reads as "this model is not served here" skips to the next
credential (the credential's health is untouched); if every credential
rejects the model, the last provider's own error surfaces instead of
the generic exhaustion 503.
"""
import json

import httpx
import pytest

from invincible.core.router import (
    NoCredentialsConfiguredError,
    UpstreamClientError,
)
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


# --- model-not-served 400s under a model override --------------------------------
#
# Providers disagree about the status code for "we do not have that
# model" (404, 400, even 403). A 400 that reads as a model-availability
# complaint must skip to the next credential - surfacing it mid-chain
# killed Codex sessions with a misleading "not a valid model ID" error
# whenever the credential that actually served the model was in cooldown
# and a bystander credential answered first.


async def test_model_not_served_400_fails_over(make_router):
    """A credential that 400s 'not a valid model ID' under a model
    override is skipped, not surfaced: the next credential serves the
    model and the request succeeds."""
    c1, h1 = capturing(httpx.Response(400, json={
        "error": {"message": "custom-model-x is not a valid model ID",
                  "type": "invalid_request_error"},
    }))
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
    assert len(c1) == 1 and len(c2) == 1
    assert info["model_id"] == "custom-model-x"


async def test_model_not_served_400_streaming_fails_over(make_router):
    """The streaming transport applies the same skip: a 400
    model-not-served body on the first credential falls through to the
    second one's live SSE stream."""
    c1, h1 = capturing(httpx.Response(400, json={
        "error": {"message": "custom-model-x is not a valid model ID"},
    }))
    c2, h2 = capturing(httpx.Response(200, text=sse_body(
        stream_chunk("second", {"content": "hey"}),
        stream_chunk("second", {}, finish_reason="stop"),
    )))
    router = make_router(handlers={
        "u1.example.com": h1, "u2.example.com": h2,
    })
    first, tail = await router.stream_open(
        [{"role": "user", "content": "hi"}],
        model="custom-model-x",
        byok_candidates=[
            byok_candidate(0, "u1.example.com"),
            byok_candidate(1, "u2.example.com"),
        ],
        byok_key_resolver=accept_key,
    )
    chunks = [first] + [chunk async for chunk in tail]
    text = "".join(
        (c["choices"][0]["delta"] or {}).get("content") or ""
        for c in chunks if c and c.get("choices")
    )
    assert "hey" in text
    assert len(c1) == 1 and len(c2) == 1


async def test_model_not_served_all_candidates_surfaces_provider_error(
    make_router,
):
    """When EVERY credential rejects the model, the last provider's own
    error surfaces (not the generic exhaustion 503) - the client still
    learns the model exists nowhere in the chain."""
    c1, h1 = capturing(httpx.Response(400, json={
        "error": {"message": "custom-model-x is not a valid model ID"},
    }))
    c2, h2 = capturing(httpx.Response(400, json={
        "error": {"message": "Model custom-model-x not found"},
    }))
    router = make_router(handlers={
        "u1.example.com": h1, "u2.example.com": h2,
    })
    with pytest.raises(UpstreamClientError) as exc_info:
        await router.route_request(
            [{"role": "user", "content": "hi"}],
            model="custom-model-x",
            byok_candidates=[
                byok_candidate(0, "u1.example.com"),
                byok_candidate(1, "u2.example.com"),
            ],
            byok_key_resolver=accept_key,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.body["error"]["message"] == (
        "Model custom-model-x not found"
    )


async def test_model_not_served_400_does_not_damage_health(make_router):
    """The skip is a no-fault one: the credential stays healthy, so a
    later request for its OWN stored model still routes to it first
    (tier order) instead of landing in cooldown."""
    c1 = []

    def h1(request):
        body = json.loads(request.content)
        c1.append(body["model"])
        if body["model"] == "other-model":
            return httpx.Response(400, json={
                "error": {"message":
                          "other-model is not a valid model ID"},
            })
        return httpx.Response(200, json=provider_body("first"))

    c2, h2 = counting(provider_body("second"))
    router = make_router(handlers={
        "u1.example.com": h1, "u2.example.com": h2,
    })
    # Turn 1: the override model only exists on u2; u1 rejects it.
    result = await router.route_request(
        [{"role": "user", "content": "hi"}],
        model="other-model",
        byok_candidates=[
            byok_candidate(0, "u1.example.com"),
            byok_candidate(1, "u2.example.com"),
        ],
        byok_key_resolver=accept_key,
    )
    assert result["choices"][0]["message"]["content"] == "hello"
    assert len(c2) == 1
    # Turn 2: no override - u1's stored default must win (attempted
    # again despite the turn-1 rejection).
    result = await router.route_request(
        [{"role": "user", "content": "again"}],
        byok_candidates=[
            byok_candidate(0, "u1.example.com"),
            byok_candidate(1, "u2.example.com"),
        ],
        byok_key_resolver=accept_key,
    )
    assert result["choices"][0]["message"]["content"] == "hello"
    # u1 served BOTH turns: no cooldown damage from the 400 skip.
    assert c1 == ["other-model", "byok-model-0"]
    assert len(c2) == 1


async def test_model_not_served_400_without_override_surfaces(make_router):
    """Without a model override the credential sent its OWN stored model,
    so a model-not-served 400 means a misconfigured credential: it must
    surface verbatim, not silently skip to the next one."""
    c1, h1 = capturing(httpx.Response(400, json={
        "error": {"message": "byok-model-0 is not a valid model ID"},
    }))
    c2, h2 = capturing(provider_body("second"))
    router = make_router(handlers={
        "u1.example.com": h1, "u2.example.com": h2,
    })
    with pytest.raises(UpstreamClientError) as exc_info:
        await router.route_request(
            [{"role": "user", "content": "hi"}],
            byok_candidates=[
                byok_candidate(0, "u1.example.com"),
                byok_candidate(1, "u2.example.com"),
            ],
            byok_key_resolver=accept_key,
        )
    assert exc_info.value.status_code == 400
    assert len(c1) == 1 and c2 == []


async def test_unrelated_400_under_override_still_surfaces(make_router):
    """Only model-availability 400s skip: a request-shape 400 under an
    override still surfaces from the first credential (it would fail
    everywhere, so trying the chain just burns quota)."""
    c1, h1 = capturing(httpx.Response(400, json={
        "error": {"message": "messages[0]: content is required"},
    }))
    c2, h2 = capturing(provider_body("second"))
    router = make_router(handlers={
        "u1.example.com": h1, "u2.example.com": h2,
    })
    with pytest.raises(UpstreamClientError):
        await router.route_request(
            [{"role": "user", "content": "hi"}],
            model="custom-model-x",
            byok_candidates=[
                byok_candidate(0, "u1.example.com"),
                byok_candidate(1, "u2.example.com"),
            ],
            byok_key_resolver=accept_key,
        )
    assert len(c1) == 1 and c2 == []
