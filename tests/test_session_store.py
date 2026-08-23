import asyncio
import json

import httpx
import pytest

from invincible.core.session_store import SessionStore
from invincible.main import app
from tests.conftest import provider_body, sse_body, stream_chunk


@pytest.mark.asyncio
async def test_corrupt_session_row_returns_empty_history(tmp_path):
    # Phase 15a: corrupt payloads live in messages.payload now. Intent is
    # unchanged - a corrupt stored row must degrade to an empty history,
    # never crash the request.
    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    await store.init()
    await store.append(
        "broken", [{"role": "user", "content": "hi"},
                   {"role": "assistant", "content": "hello"}]
    )
    await store._db.execute("UPDATE messages SET payload = 'not json{'")
    await store._db.commit()

    assert await store.load("broken") == []

    await store.close()


@pytest.mark.asyncio
async def test_session_history_is_replayed_on_second_request(router_setter, client):
    """Proves the gateway remembers prior turns within the same session_id,
    and does NOT leak history across a different session_id."""

    received_payloads = []

    def alpha_handler(request: httpx.Request):
        received_payloads.append(request.read())
        return httpx.Response(
            200, json=provider_body("alpha", content="Nice to meet you!")
        )

    router_setter({"alpha.example.com": alpha_handler})

    headers = {
        "Authorization": "Bearer test-gateway-key",
        "X-Session-Id": "conversation-1",
    }

    resp1 = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "My name is TestUser"}]},
    )
    assert resp1.status_code == 200

    resp2 = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "What is my name?"}]},
    )
    assert resp2.status_code == 200

    import json
    second_outgoing = json.loads(received_payloads[1])
    outgoing_contents = [m["content"] for m in second_outgoing["messages"]]

    assert "My name is TestUser" in outgoing_contents
    assert "Nice to meet you!" in outgoing_contents
    assert "What is my name?" in outgoing_contents


@pytest.mark.asyncio
async def test_different_session_ids_do_not_share_history(router_setter, client):
    def alpha_handler(request: httpx.Request):
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})

    await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-gateway-key",
            "X-Session-Id": "session-a",
        },
        json={"messages": [{"role": "user", "content": "secret: banana"}]},
    )

    received = []

    def alpha_handler_2(request: httpx.Request):
        received.append(request.read())
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler_2})

    await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-gateway-key",
            "X-Session-Id": "session-b",
        },
        json={"messages": [{"role": "user", "content": "what is the secret?"}]},
    )

    import json
    payload = json.loads(received[0])
    contents = [m["content"] for m in payload["messages"]]
    assert "secret: banana" not in contents


@pytest.mark.asyncio
async def test_concurrent_appends_lose_no_turns(tmp_path):
    """append() serializes read-modify-write per store, so N concurrent
    requests to the same session all land instead of last-write-wins."""
    store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    await store.init()

    async def append_turn(i):
        await store.append(
            "race-session", [{"role": "user", "content": f"q{i}"},
                             {"role": "assistant", "content": f"a{i}"}]
        )

    await asyncio.gather(*[append_turn(i) for i in range(25)])

    history = await store.load("race-session")
    assert len(history) == 50
    users = [m["content"] for m in history if m["role"] == "user"]
    assistants = [m["content"] for m in history if m["role"] == "assistant"]
    assert sorted(users, key=lambda s: int(s[1:])) == [f"q{i}" for i in range(25)]
    assert sorted(assistants, key=lambda s: int(s[1:])) == [f"a{i}" for i in range(25)]

    await store.close()


@pytest.mark.asyncio
async def test_openai_system_messages_not_persisted_to_session(
    client, router_setter
):
    """Mirror of the Anthropic guarantee: repeated OpenAI requests with a
    system prompt must not accumulate system messages in history."""
    received = []

    def recording_handler(request: httpx.Request):
        received.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": recording_handler})
    headers = {
        "Authorization": "Bearer test-gateway-key",
        "X-Session-Id": "openai-no-sys-accum",
    }

    for _ in range(3):
        response = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "messages": [
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "hi"},
                ]
            },
        )
        assert response.status_code == 200

    history = await app.state.sessions.load("openai-no-sys-accum")
    assert [m["role"] for m in history] == [
        "user", "assistant", "user", "assistant", "user", "assistant",
    ]
    assert all(m.get("role") != "system" for m in history)


@pytest.mark.asyncio
async def test_openai_system_prompt_still_sent_upstream_each_request(
    client, router_setter
):
    """Every OpenAI request still sends its own current system prompt
    upstream; prior requests' system prompts never leak in as stale copies."""
    received = []

    def recording_handler(request: httpx.Request):
        received.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": recording_handler})
    headers = {
        "Authorization": "Bearer test-gateway-key",
        "X-Session-Id": "openai-sys-per-request",
    }

    await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [
            {"role": "system", "content": "System-One"},
            {"role": "user", "content": "a"},
        ]},
    )
    await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [
            {"role": "system", "content": "System-Two"},
            {"role": "user", "content": "b"},
        ]},
    )

    first_systems = [
        m["content"] for m in received[0]["messages"] if m["role"] == "system"
    ]
    second_systems = [
        m["content"] for m in received[1]["messages"] if m["role"] == "system"
    ]
    assert first_systems == ["System-One"]
    assert second_systems == ["System-Two"]


@pytest.mark.asyncio
async def test_openai_claude_code_session_id_isolates_history(router_setter, client):
    """x-claude-code-session-id isolates OpenAI sessions the same way:
    session A's history is never replayed into session B."""
    received = []

    def alpha_handler(request: httpx.Request):
        received.append(request.read())
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})

    await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-gateway-key",
            "x-claude-code-session-id": "openai-session-A",
        },
        json={"messages": [{"role": "user", "content": "secret-from-A"}]},
    )

    await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-gateway-key",
            "x-claude-code-session-id": "openai-session-B",
        },
        json={"messages": [{"role": "user", "content": "what is the secret?"}]},
    )

    import json
    payload = json.loads(received[1])
    contents = [m["content"] for m in payload["messages"]]
    assert "secret-from-A" not in contents
    assert "what is the secret?" in contents


@pytest.mark.asyncio
async def test_streamed_reply_is_persisted_to_session(client, router_setter):
    """The streamed reply is reconstructed from the chunk deltas and saved to
    the session store once the stream completes, like the non-stream path."""
    received_payloads = []

    def alpha_handler(request: httpx.Request):
        received_payloads.append(json.loads(request.read()))
        return httpx.Response(
            200,
            content=sse_body(
                stream_chunk("alpha", {"role": "assistant"}),
                stream_chunk("alpha", {"content": "Hello"}),
                stream_chunk("alpha", {"content": " world"}),
                stream_chunk("alpha", {}, finish_reason="stop"),
            ),
        )

    router_setter({"alpha.example.com": alpha_handler})

    headers = {
        "Authorization": "Bearer test-gateway-key",
        "X-Session-Id": "stream-convo",
    }
    await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )

    history = await app.state.sessions.load("stream-convo")
    assistant_messages = [m for m in history if m["role"] == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["content"] == "Hello world"

    await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "again"}]},
    )
    second_outgoing = received_payloads[1]["messages"]
    assert [m["content"] for m in second_outgoing] == ["hi", "Hello world", "again"]
