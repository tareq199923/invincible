# tests/test_route_metadata.py
"""x-invincible-* exposure headers across all four response paths
(Phase 13.5)."""
import json

import httpx

from tests.conftest import (
    provider_body,
    sse_body,
    stream_chunk,
    v1_user,
)


async def test_openai_nonstreaming_headers_report_actual_route(
    client, router_setter, byok_env
):
    _, auth_key = await v1_user(client, "route@example.com")
    auth = {"Authorization": f"Bearer {auth_key}"}
    def alpha_down(request):
        return httpx.Response(500, json={"error": "down"})

    def beta_ok(request):
        return httpx.Response(200, json=provider_body("beta"))

    router_setter({"alpha.example.com": alpha_down, "beta.example.com": beta_ok})
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.headers["x-invincible-provider"] == "beta"
    assert resp.headers["x-invincible-model"] == "beta-model"
    assert resp.headers["x-invincible-attempts"] == "2"
    assert resp.headers["x-invincible-request-id"]


async def test_openai_single_attempt_headers(client, router_setter, byok_env):
    _, auth_key = await v1_user(client, "route@example.com")
    auth = {"Authorization": f"Bearer {auth_key}"}
    router_setter({"alpha.example.com": lambda r: httpx.Response(
        200, json=provider_body("alpha")
    )})
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.headers["x-invincible-attempts"] == "1"
    assert resp.headers["x-invincible-provider"] == "alpha"


async def test_openai_streaming_headers_present_before_body(
    client, router_setter, byok_env
):
    _, auth_key = await v1_user(client, "route@example.com")
    auth = {"Authorization": f"Bearer {auth_key}"}
    router_setter({
        "alpha.example.com": lambda r: httpx.Response(500, json={"error": "down"}),
        "beta.example.com": lambda r: httpx.Response(
            200,
            content=sse_body(stream_chunk("beta", {"content": "hey"})),
        ),
    })
    req = client.build_request(
        "POST",
        "/v1/chat/completions",
        headers=auth,
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    resp = await client.send(req, stream=True)
    assert resp.status_code == 200
    assert resp.headers["x-invincible-provider"] == "beta"
    assert resp.headers["x-invincible-attempts"] == "2"
    assert resp.headers["content-type"].startswith("text/event-stream")
    await resp.aclose()


async def test_anthropic_nonstreaming_headers_and_model_echo(
    client, router_setter, byok_env
):
    _, auth_key = await v1_user(client, "route@example.com")
    auth = {"Authorization": f"Bearer {auth_key}"}
    def alpha_down(request):
        return httpx.Response(500, json={"error": "down"})

    def beta_ok(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    router_setter({"alpha.example.com": alpha_down, "beta.example.com": beta_ok})
    resp = await client.post(
        "/v1/messages",
        headers=auth,
        json={"model": "claude-3-5-sonnet", "max_tokens": 10,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.headers["x-invincible-provider"] == "beta"
    assert resp.headers["x-invincible-attempts"] == "2"
    body = json.loads(resp.content)
    # The Anthropic model field still echoes what the client asked for;
    # actual routing is exposed via headers and runs records only.
    assert body["model"] == "claude-3-5-sonnet"


async def test_anthropic_streaming_headers(client, router_setter, byok_env):
    _, auth_key = await v1_user(client, "route@example.com")
    auth = {"Authorization": f"Bearer {auth_key}"}
    router_setter({
        "alpha.example.com": lambda r: httpx.Response(500, json={"error": "down"}),
        "beta.example.com": lambda r: httpx.Response(
            200,
            content=sse_body(stream_chunk("beta", {"content": "hey"})),
        ),
    })
    req = client.build_request(
        "POST",
        "/v1/messages",
        headers=auth,
        json={"max_tokens": 10, "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    resp = await client.send(req, stream=True)
    assert resp.status_code == 200
    assert resp.headers["x-invincible-provider"] == "beta"
    assert resp.headers["x-invincible-request-id"]
    await resp.aclose()
