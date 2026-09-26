import json

import httpx

from invincible.core.identity import ensure_default_project
from invincible.main import app
from tests.conftest import (
    provider_body,
    sse_body,
    stream_chunk,
    v1_user,
)

MESSAGES = [{"role": "user", "content": "hi"}]


async def _events(response):
    return [
        event[len("data: "):]
        for event in response.text.split("\n\n")
        if event.startswith("data: ") and not event.startswith("data: [DONE]")
    ]


async def _user_kwargs(uid: int) -> dict:
    """Store-level kwargs for the inv_ user a v1_user mint resolved."""
    return {"user_id": uid,
            "project_id": await ensure_default_project(app.state.engine, uid)}


async def test_health_check(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


async def test_root_serves_landing_page_to_browsers(client):
    """Address-bar navigation (Accept: text/html) gets the marketing
    page; the negotiation must never leak into API clients (above)."""
    response = await client.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # Hero + CTA anchors render from the template, not a blank shell.
    body = response.text
    assert "One gateway." in body
    assert "Your providers." in body
    assert "Your continuity." in body
    assert "https://invincible-ai.me/v1" in body
    assert "OAuth-protected MCP tools" in body
    assert "Per-user BYOK failover" in body
    assert "OpenAI Responses (Codex)" in body
    assert "Zero inbound ports" in body
    assert "Frequently asked questions" in body
    assert "Pair once. Route everything." in body
    assert "Every provider" not in body
    assert "pip install invincible-ai" in body
    assert "invincible harness connect" in body
    assert "invincible start" not in body
    assert 'href="/register"' in body
    assert 'href="/login"' in body


async def test_chat_completion_success(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions", headers=auth, json={"messages": MESSAGES}
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_streaming_true_returns_event_stream(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        headers=auth,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


async def test_streaming_chunks_emitted_incrementally(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        headers=auth,
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


async def test_streaming_ends_with_done(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        headers=auth,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.text.endswith("data: [DONE]\n\n")


async def test_streaming_auth_enforced(client):
    response = await client.post(
        "/v1/chat/completions", json={"messages": MESSAGES, "stream": True}
    )
    assert response.status_code == 401


async def test_stream_false_returns_json(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"messages": MESSAGES, "stream": False},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == alpha_body


async def test_streaming_failover_before_first_chunk(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        headers=auth,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.status_code == 200
    payloads = [json.loads(event) for event in await _events(response)]
    assert payloads[0]["model"] == "beta-model"
    assert not router.health_tracker.is_available("byok:1")


async def test_streaming_all_providers_fail_returns_503(
    client, router_setter, byok_env
):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(500),
            "gamma.example.com": httpx.Response(429),
        }
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=auth,
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


async def test_streaming_midstream_error_terminates_cleanly(
    client, router_setter, byok_env
):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        headers=auth,
        json={"messages": MESSAGES, "stream": True},
    )
    assert response.status_code == 200
    assert "data: " in response.text
    assert '"error"' in response.text
    assert not response.text.endswith("data: [DONE]\n\n")


async def test_streamed_tool_calls_are_persisted(client, router_setter, byok_env):
    """Streamed tool_call fragments are reassembled into the persisted
    assistant turn, matching what a non-streaming upstream would return."""
    uid, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        headers={**auth, "X-Session-Id": "tool-stream"},
        json={"messages": MESSAGES, "stream": True},
    )

    history = await app.state.sessions.load(
        "tool-stream", **await _user_kwargs(uid))
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
    client, router_setter, byok_env
):
    """Fragments for several tool calls interleave by index; the persisted
    turn lists them in ascending index order with complete arguments."""
    uid, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        headers={**auth, "X-Session-Id": "parallel-tools"},
        json={"messages": MESSAGES, "stream": True},
    )

    history = await app.state.sessions.load(
        "parallel-tools", **await _user_kwargs(uid))
    assistant = [m for m in history if m["role"] == "assistant"][0]
    assert [t["id"] for t in assistant["tool_calls"]] == ["call_a", "call_b"]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"y":2}'
    assert assistant["tool_calls"][1]["function"]["arguments"] == '{"x":1}'


async def test_midstream_error_persists_partial_tool_turn(
    client, router_setter, byok_env
):
    """A stream that dies mid-flight still persists what accumulated, so the
    stored history matches the partial output the client received."""
    uid, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        headers={**auth, "X-Session-Id": "partial-tool-stream"},
        json={"messages": MESSAGES, "stream": True},
    )
    assert '"error"' in response.text

    history = await app.state.sessions.load(
        "partial-tool-stream", **await _user_kwargs(uid))
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


async def test_x_api_key_auth_succeeds(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers={"x-api-key": raw_key},
        json={"messages": MESSAGES},
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_bearer_priority_over_x_api_key(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {raw_key}",
            "x-api-key": "wrong-key",
        },
        json={"messages": MESSAGES},
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_valid_auth_succeeds(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"messages": MESSAGES},
    )
    assert response.status_code == 200
    assert response.json() == alpha_body


async def test_no_auth_is_401_even_with_providers_configured(
    client, router_setter
):
    """The fail-open anonymous realm is gone: with no GATEWAY_API_KEY to
    unset and no bearer at all, /v1/* is 401 - there is no local mode."""
    alpha_body = provider_body("alpha")
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)}
    )
    response = await client.post(
        "/v1/chat/completions", json={"messages": MESSAGES}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


async def test_missing_messages_returns_422(client, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"}, json={})
    assert response.status_code == 422


async def test_all_providers_fail_returns_503(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={"alpha.example.com": httpx.Response(429)})
    response = await client.post(
        "/v1/chat/completions", headers=auth, json={"messages": MESSAGES}
    )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "gateway_error"


async def test_upstream_error_forwarded(client, router_setter, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    error_body = {"error": {"message": "bad request"}}
    router_setter(
        handlers={"alpha.example.com": httpx.Response(400, json=error_body)}
    )
    response = await client.post(
        "/v1/chat/completions", headers=auth, json={"messages": MESSAGES}
    )
    assert response.status_code == 400
    assert response.json() == error_body


async def test_models_lists_configured_models(client, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    response = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {raw_key}"})
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [
        model["id"] for model in body["data"]
    ] == ["alpha-model", "beta-model", "gamma-model"]


async def test_models_zero_credentials_returns_empty_list(client, byok_env):
    """A key with nothing connected lists nothing - matching the chat
    surface's clean 400 (LOW-3)."""
    _, raw_key = await v1_user(client, "api@example.com", providers=[])
    response = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {raw_key}"})
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}


async def test_models_matches_openai_schema(client, byok_env):
    _, raw_key = await v1_user(client, "api@example.com")
    response = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {raw_key}"})
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


async def test_model_field_accepted_no_422(client, router_setter, byok_env):
    """The OpenAI request body accepts model; an unknown model name is a
    soft hint in auto mode, not a 422."""
    _, raw_key = await v1_user(client, "api@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=provider_body("alpha"))}
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"messages": MESSAGES, "model": "claude-sonnet-4"},
    )
    assert response.status_code == 200
