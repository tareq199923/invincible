import json
import logging

import httpx

from invincible.main import _warn_if_gateway_open, app
from tests.conftest import default_providers, provider_body, sse_body, stream_chunk

MESSAGES = [{"role": "user", "content": "hi"}]
AUTH = {"Authorization": "Bearer test-gateway-key"}


async def _events(response):
    return [
        event[len("data: "):]
        for event in response.text.split("\n\n")
        if event.startswith("data: ") and not event.startswith("data: [DONE]")
    ]


def test_gateway_open_warns_loudly_when_key_unset(caplog, monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING):
        _warn_if_gateway_open()
    assert any(
        "UNAUTHENTICATED" in record.message for record in caplog.records
    )


def test_gateway_open_warns_nothing_when_key_set(caplog, monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    with caplog.at_level(logging.WARNING):
        _warn_if_gateway_open()
    assert not any(
        "UNAUTHENTICATED" in record.message for record in caplog.records
    )


async def test_health_check(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


async def test_chat_completion_success(client, router_setter):
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={"messages": MESSAGES}
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_streaming_true_returns_event_stream(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                content=sse_body(stream_chunk("alpha", {"role": "assistant"})),
            )
        }
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


async def test_streaming_chunks_emitted_incrementally(client, router_setter):
    chunks = [
        stream_chunk("alpha", {"role": "assistant"}),
        stream_chunk("alpha", {"content": "Hel"}),
        stream_chunk("alpha", {"content": "lo!"}),
        stream_chunk("alpha", {}, finish_reason="stop"),
    ]
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, content=sse_body(*chunks))}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.status_code == 200
    payloads = [json.loads(event) for event in await _events(response)]
    assert [
        p["choices"][0]["delta"] for p in payloads
    ] == [
        {"role": "assistant"},
        {"content": "Hel"},
        {"content": "lo!"},
        {},
    ]
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


async def test_streaming_ends_with_done(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                content=sse_body(stream_chunk("alpha", {"content": "hi"})),
            )
        }
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.text.endswith("data: [DONE]\n\n")


async def test_streaming_auth_enforced(client):
    response = await client.post(
        "/v1/chat/completions", json={"messages": MESSAGES, "stream": True}
    )
    assert response.status_code == 401


async def test_stream_false_returns_json(client, router_setter):
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES, "stream": False},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == alpha_body


async def test_streaming_failover_before_first_chunk(client, router_setter):
    router = router_setter(
        handlers={
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(
                200,
                content=sse_body(
                    stream_chunk("beta", {"role": "assistant"}),
                    stream_chunk("beta", {"content": "hi"}),
                    stream_chunk("beta", {}, finish_reason="stop"),
                ),
            ),
        }
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.status_code == 200
    payloads = [json.loads(event) for event in await _events(response)]
    assert payloads[0]["model"] == "beta-model"
    assert not router.health_tracker.is_available("alpha")


async def test_streaming_all_providers_fail_returns_503(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(500),
            "gamma.example.com": httpx.Response(429),
        }
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "gateway_error"


class _FailingStream(httpx.AsyncByteStream):
    def __init__(self, prefix: bytes):
        self._prefix = prefix

    async def __aiter__(self):
        yield self._prefix
        raise httpx.StreamError("connection dropped mid-stream")

    async def aclose(self):
        pass


async def test_streaming_midstream_error_terminates_cleanly(client, router_setter):
    stream = _FailingStream(
        sse_body(stream_chunk("alpha", {"role": "assistant"}), done=False).encode()
    )
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/event-stream"},
            )
        }
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.status_code == 200
    assert "data: " in response.text
    assert '"error"' in response.text
    assert not response.text.endswith("data: [DONE]\n\n")


async def test_streamed_tool_calls_are_persisted(client, router_setter):
    """Streamed tool_call fragments are reassembled into the persisted
    assistant turn, matching what a non-streaming upstream would return."""
    chunks = [
        stream_chunk("alpha", {"role": "assistant"}),
        stream_chunk(
            "alpha",
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ]
            },
        ),
        stream_chunk(
            "alpha",
            {
                "tool_calls": [
                    {"index": 0, "function": {"arguments": '{"city": "Par'}}
                ]
            },
        ),
        stream_chunk(
            "alpha",
            {"tool_calls": [{"index": 0, "function": {"arguments": 'is"}'}}]},
        ),
        stream_chunk("alpha", {}, finish_reason="tool_calls"),
    ]
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, content=sse_body(*chunks))}
    )
    await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Session-Id": "tool-stream"},
        json={"messages": MESSAGES, "stream": True},
    )

    history = await app.state.sessions.load("tool-stream")
    assistant = [m for m in history if m["role"] == "assistant"]
    assert len(assistant) == 1
    message = assistant[0]
    assert message["content"] is None
    assert message["tool_calls"] == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }
    ]


async def test_streamed_parallel_tool_calls_persist_in_index_order(
    client, router_setter
):
    """Fragments for several tool calls interleave by index; the persisted
    turn lists them in ascending index order with complete arguments."""
    chunks = [
        stream_chunk("alpha", {"role": "assistant"}),
        stream_chunk(
            "alpha",
            {
                "tool_calls": [
                    {"index": 0, "id": "call_a", "function": {"name": "f1"}},
                    {"index": 1, "id": "call_b", "function": {"name": "f2"}},
                ]
            },
        ),
        stream_chunk(
            "alpha",
            {
                "tool_calls": [
                    {"index": 1, "function": {"arguments": '{"x":1}'}},
                    {"index": 0, "function": {"arguments": '{"y":2}'}},
                ]
            },
        ),
    ]
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, content=sse_body(*chunks))}
    )
    await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Session-Id": "parallel-tools"},
        json={"messages": MESSAGES, "stream": True},
    )

    history = await app.state.sessions.load("parallel-tools")
    assistant = [m for m in history if m["role"] == "assistant"][0]
    assert [t["id"] for t in assistant["tool_calls"]] == ["call_a", "call_b"]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"y":2}'
    assert assistant["tool_calls"][1]["function"]["arguments"] == '{"x":1}'


async def test_midstream_error_persists_partial_tool_turn(client, router_setter):
    """A stream that dies mid-flight still persists what accumulated, so the
    stored history matches the partial output the client received."""
    prefix_chunks = [
        stream_chunk("alpha", {"role": "assistant"}),
        stream_chunk(
            "alpha",
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_partial",
                        "function": {"name": "run", "arguments": '{"cmd"'},
                    }
                ]
            },
        ),
    ]
    stream = _FailingStream(sse_body(*prefix_chunks, done=False).encode())
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/event-stream"},
            )
        }
    )
    response = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Session-Id": "partial-tool-stream"},
        json={"messages": MESSAGES, "stream": True},
    )
    assert '"error"' in response.text

    history = await app.state.sessions.load("partial-tool-stream")
    assistant = [m for m in history if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] is None
    assert assistant[0]["tool_calls"][0]["id"] == "call_partial"
    assert assistant[0]["tool_calls"][0]["function"]["arguments"] == '{"cmd"'


async def test_missing_auth_returns_401(client):
    response = await client.post(
        "/v1/chat/completions", json={"messages": MESSAGES}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


async def test_invalid_auth_returns_401(client):
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong-key"},
        json={"messages": MESSAGES},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


async def test_x_api_key_auth_succeeds(client, router_setter):
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers={"x-api-key": "test-gateway-key"},
        json={"messages": MESSAGES},
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_bearer_priority_over_x_api_key(client, router_setter):
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-gateway-key",
            "x-api-key": "wrong-key",
        },
        json={"messages": MESSAGES},
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_valid_auth_succeeds(client, router_setter):
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES},
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_auth_optional_when_key_unset(client, router_setter, monkeypatch):
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    monkeypatch.delenv("GATEWAY_API_KEY")
    response = await client.post(
        "/v1/chat/completions", json={"messages": MESSAGES}
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_missing_messages_returns_422(client):
    response = await client.post("/v1/chat/completions", headers=AUTH, json={})
    assert response.status_code == 422


async def test_all_providers_fail_returns_503(client, router_setter):
    router_setter(handlers={"alpha.example.com": httpx.Response(429)})
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={"messages": MESSAGES}
    )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "gateway_error"


async def test_upstream_error_forwarded(client, router_setter):
    error_body = {"error": {"message": "bad request"}}
    router_setter(
        handlers={"alpha.example.com": httpx.Response(400, json=error_body)}
    )
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={"messages": MESSAGES}
    )
    assert response.status_code == 400
    assert response.json() == error_body


async def test_models_lists_configured_models(client):
    response = await client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [
        model["id"] for model in body["data"]
    ] == ["alpha-model", "beta-model", "gamma-model"]


async def test_models_order_follows_router_tier_order(client, router_setter):
    router_setter(
        providers=list(reversed(default_providers())),
        handlers={},
    )
    response = await client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert [
        model["id"] for model in response.json()["data"]
    ] == ["alpha-model", "beta-model", "gamma-model"]


async def test_models_empty_providers_returns_empty_list(client, router_setter):
    router_setter(providers=[], handlers={})
    response = await client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}


async def test_models_matches_openai_schema(client):
    response = await client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {"id": "alpha-model", "object": "model", "owned_by": "invincible"},
            {"id": "beta-model", "object": "model", "owned_by": "invincible"},
            {"id": "gamma-model", "object": "model", "owned_by": "invincible"},
        ],
    }


async def test_models_requires_auth(client):
    response = await client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


# --- Phase 6: model aliasing --------------------------------------------------


async def test_model_field_accepted_no_422(client, router_setter):
    """The OpenAI request body now accepts model (previously 422)."""
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=provider_body("alpha"))}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES, "model": "claude-sonnet-4"},
    )
    assert response.status_code == 200


async def test_model_alias_routes_to_preferred_provider(client, router_setter):
    """model: fast routes to the aliased provider (beta) instead of tier-1
    alpha - the endpoint surfaces the router's soft alias preference."""
    providers = default_providers()
    providers[1]["aliases"] = ["fast"]
    router_setter(
        providers=providers,
        handlers={
            "alpha.example.com": httpx.Response(503),
            "beta.example.com": httpx.Response(200, json=provider_body("beta")),
            "gamma.example.com": httpx.Response(503),
        },
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": MESSAGES, "model": "fast"},
    )
    assert response.status_code == 200
    assert response.json() == provider_body("beta")


async def test_models_lists_aliases_after_model_ids(client, router_setter):
    providers = default_providers()
    providers[1]["aliases"] = ["fast"]
    router_setter(providers=providers, handlers={})
    response = await client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert [
        model["id"] for model in response.json()["data"]
    ] == ["alpha-model", "beta-model", "gamma-model", "fast"]
