# tests/test_responses_api.py
"""POST /v1/responses (OpenAI Responses API compatibility, spoken by
Codex CLI): request translation to the internal model, response/SSE
translation back, BYOK routing, and the prefix-dedupe persistence that
stateless Responses clients (which resend the full conversation every
turn) require.
"""
import json

import httpx
import pytest
from cryptography.fernet import Fernet

from invincible.compat.common import upstream_error_detail
from invincible.compat.responses import responses_to_internal
from invincible.main import app
from tests.conftest import (
    default_providers,
    local_owner_kwargs,
    provider_body,
    sse_body,
    stream_chunk,
)

AUTH = {"Authorization": "Bearer test-gateway-key"}


def _responses_events(response):
    """Parse a Responses SSE response into [(event, payload), ...]."""
    events = []
    for block in response.text.split("\n\n"):
        event = None
        payload = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
        if event is not None:
            events.append((event, payload))
    return events


class _FailingStream(httpx.AsyncByteStream):
    def __init__(self, prefix: bytes):
        self._prefix = prefix

    async def __aiter__(self):
        yield self._prefix
        raise httpx.StreamError("connection dropped mid-stream")

    async def aclose(self):
        pass


# --------------------------------------------------------- pure translation


def test_string_input_and_instructions_translate_to_internal():
    internal = responses_to_internal("hi", "Be concise.")
    assert internal == [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "hi"},
    ]


def test_empty_input_raises_value_error():
    with pytest.raises(ValueError):
        responses_to_internal("")


def test_reasoning_items_are_skipped():
    internal = responses_to_internal([
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hi"}]},
    ])
    assert internal == [{"role": "user", "content": "hi"}]


# ------------------------------------------------------- non-streaming happy


async def test_response_object_shape(client, router_setter):
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="Hello world"))
    })
    response = await client.post(
        "/v1/responses",
        headers=AUTH,
        json={
            "model": "gpt-5.6-terra",
            "instructions": "Be terse.",
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "hi"}]}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["id"].startswith("resp_")
    assert body["model"] == "gpt-5.6-terra"
    message_items = [i for i in body["output"] if i["type"] == "message"]
    assert len(message_items) == 1
    assert message_items[0]["id"].startswith("msg_")
    part = message_items[0]["content"][0]
    assert part["type"] == "output_text"
    assert part["text"] == "Hello world"
    assert part["annotations"] == []
    usage = body["usage"]
    assert usage["input_tokens"] >= 1
    assert usage["output_tokens"] >= 1
    assert usage["total_tokens"] == (
        usage["input_tokens"] + usage["output_tokens"])


async def test_plain_string_input(client, router_setter):
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="ok"))
    })
    response = await client.post(
        "/v1/responses", headers=AUTH,
        json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


async def test_instructions_become_system_upstream(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/responses",
        headers=AUTH,
        json={"instructions": "You are terse.",
              "input": "hi"},
    )
    assert response.status_code == 200
    outgoing = captured[0]["messages"]
    assert outgoing[0] == {"role": "system", "content": "You are terse."}
    assert outgoing[-1] == {"role": "user", "content": "hi"}


# ------------------------------------------------- tool calls round-trip


async def test_function_call_items_round_trip(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/responses",
        headers=AUTH,
        json={
            "model": "m",
            "input": [
                {"type": "message", "role": "user",
                 "content": "run the tool"},
                {"type": "function_call", "call_id": "call_1",
                 "name": "search", "arguments": '{"query": "x"}'},
                {"type": "function_call_output", "call_id": "call_1",
                 "output": "result text"},
            ],
        },
    )
    assert response.status_code == 200
    outgoing = captured[0]["messages"]
    assert outgoing == [
        {"role": "user", "content": "run the tool"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search",
                                 "arguments": '{"query": "x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result text"},
    ]


async def test_tool_choice_without_tools_is_dropped(client, router_setter):
    """Codex occasionally sends tool_choice on a turn with no tools; every
    OpenAI-compatible upstream 400s on that pair, so the router must drop
    tool_choice when the tools list is empty."""
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/responses",
        headers=AUTH,
        json={"model": "m", "input": "hi",
              "tools": [], "tool_choice": "auto"},
    )
    assert response.status_code == 200
    assert "tool_choice" not in captured[0]
    assert "tools" not in captured[0]


async def test_tools_and_tool_choice_translate(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/responses",
        headers=AUTH,
        json={
            "model": "m",
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "search",
                    "description": "Search things",
                    "strict": False,
                    "parameters": {"type": "object",
                                   "properties": {"q": {"type": "string"}}},
                },
                {"type": "web_search"},
            ],
            "tool_choice": "required",
        },
    )
    assert response.status_code == 200
    outgoing = captured[0]
    assert outgoing["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search things",
                "parameters": {"type": "object",
                               "properties": {"q": {"type": "string"}}},
            },
        }
    ]
    assert outgoing["tool_choice"] == "required"


async def test_provider_tool_calls_become_function_call_items(
    client, router_setter
):
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, json={
            "id": "cmpl-x",
            "model": "alpha-model",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_9",
                        "type": "function",
                        "function": {"name": "shell",
                                     "arguments": '{"cmd": "ls"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        })
    })
    response = await client.post(
        "/v1/responses", headers=AUTH, json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    calls = [i for i in body["output"] if i["type"] == "function_call"]
    assert len(calls) == 1
    assert calls[0]["call_id"] == "call_9"
    assert calls[0]["name"] == "shell"
    assert json.loads(calls[0]["arguments"]) == {"cmd": "ls"}


# ------------------------------------------------------------------ streaming


async def test_streaming_canonical_sequence(client, router_setter):
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"content": "Hel"}),
            stream_chunk("alpha", {"content": "lo!"}),
            stream_chunk("alpha", {}, finish_reason="stop"),
        ))
    })
    response = await client.post(
        "/v1/responses", headers=AUTH,
        json={"model": "m", "input": "hi", "stream": True},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _responses_events(response)
    names = [name for name, _ in events]
    assert names[0] == "response.created"
    assert names[1] == "response.output_item.added"
    assert names[2] == "response.content_part.added"
    assert names[3:5] == ["response.output_text.delta"] * 2
    assert names[5] == "response.output_text.done"
    assert names[6] == "response.content_part.done"
    assert names[7] == "response.output_item.done"
    assert names[8] == "response.completed"
    assert names[-1] == "response.completed"

    deltas = [p["delta"] for name, p in events
              if name == "response.output_text.delta"]
    assert "".join(deltas) == "Hello!"

    created = dict(events)[
        "response.created"]["response"]
    assert created["object"] == "response"
    assert created["status"] == "in_progress"

    completed = [p for name, p in events
                 if name == "response.completed"][0]["response"]
    assert completed["status"] == "completed"
    message_items = [i for i in completed["output"]
                     if i["type"] == "message"]
    assert message_items[0]["content"][0]["text"] == "Hello!"
    assert completed["usage"]["output_tokens"] >= 1


async def test_streaming_function_calls(client, router_setter):
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"tool_calls": [
                {"index": 0, "id": "call_7", "type": "function",
                 "function": {"name": "shell", "arguments": '{"cm'}},
            ]}),
            stream_chunk("alpha", {"tool_calls": [
                {"index": 0, "function": {"arguments": 'd": "ls"}'}},
            ]}),
            stream_chunk("alpha", {}, finish_reason="tool_calls"),
        ))
    })
    response = await client.post(
        "/v1/responses", headers=AUTH,
        json={"model": "m", "input": "hi", "stream": True},
    )
    assert response.status_code == 200
    events = _responses_events(response)
    names = [name for name, _ in events]

    added = [p for name, p in events
             if name == "response.output_item.added"]
    function_added = [p for p in added
                      if p["item"]["type"] == "function_call"]
    assert function_added[0]["item"]["call_id"] == "call_7"

    arg_deltas = [p["delta"] for name, p in events
                  if name == "response.function_call_arguments.delta"]
    assert "".join(arg_deltas) == '{"cmd": "ls"}'

    arg_done = [p for name, p in events
                if name == "response.function_call_arguments.done"][0]
    assert arg_done["arguments"] == '{"cmd": "ls"}'

    completed = [p for name, p in events
                 if name == "response.completed"][0]["response"]
    calls = [i for i in completed["output"]
             if i["type"] == "function_call"]
    assert calls[0]["call_id"] == "call_7"
    assert json.loads(calls[0]["arguments"]) == {"cmd": "ls"}

    assert "response.output_item.done" in names


async def test_stream_persists_session_history(client, router_setter):
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"content": "Hi"}),
            stream_chunk("alpha", {}, finish_reason="stop"),
        ))
    })
    response = await client.post(
        "/v1/responses",
        headers={**AUTH, "X-Session-Id": "resp-stream"},
        json={"model": "m", "input": "hello", "stream": True},
    )
    assert response.status_code == 200
    # Consume the body so the stream generator (and its persistence
    # callback) runs to completion before we read the store.
    assert "response.completed" in response.text
    history = await app.state.sessions.load(
        "resp-stream", **await local_owner_kwargs(app.state.engine))
    assert history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi"},
    ]


async def test_mid_stream_failure_closes_well_formed(client, router_setter):
    prefix = sse_body(
        stream_chunk("alpha", {"role": "assistant"}),
        stream_chunk("alpha", {"content": "partial"}),
        done=False,
    ).encode("utf-8")
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, content=_FailingStream(prefix))
    })
    response = await client.post(
        "/v1/responses", headers=AUTH,
        json={"model": "m", "input": "hi", "stream": True},
    )
    assert response.status_code == 200
    events = _responses_events(response)
    names = [name for name, _ in events]
    # One error frame, no completed event, stream closed cleanly.
    assert "error" in names
    assert "response.completed" not in names


# --------------------------------------------- prefix-dedupe persistence


async def test_resent_conversation_does_not_duplicate(client, router_setter):
    """Codex resends the full conversation each turn: the second request
    must not duplicate history upstream nor in the stored session."""
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    headers = {**AUTH, "X-Session-Id": "dedupe"}

    # Turn 1: just the user message. Codex always sends instructions -
    # the resent system message must not break the prefix match.
    await client.post(
        "/v1/responses", headers=headers,
        json={"model": "m", "instructions": "Be terse.",
              "input": [{"type": "message", "role": "user",
                         "content": "hi"}]},
    )
    # Turn 2: the resent conversation (turn 1 + its reply) + the new
    # user message - exactly what Codex sends.
    await client.post(
        "/v1/responses", headers=headers,
        json={
            "model": "m",
            "instructions": "Be terse.",
            "input": [
                {"type": "message", "role": "user", "content": "hi"},
                {"type": "message", "role": "assistant", "content": "ok"},
                {"type": "message", "role": "user", "content": "again"},
            ],
        },
    )

    # Upstream saw the conversation exactly once per request - no
    # duplicated "hi"/"ok" pair from a history prepend.
    outgoing = captured[1]["messages"]
    user_contents = [m["content"] for m in outgoing
                     if m["role"] == "user"]
    assert user_contents.count("hi") == 1
    assert user_contents.count("again") == 1

    # Stored history has each turn once.
    history = await app.state.sessions.load(
        "dedupe", **await local_owner_kwargs(app.state.engine))
    contents = [m.get("content") for m in history]
    assert contents == ["hi", "ok", "again", "ok"]


async def test_codex_session_id_header_creates_its_own_session(
    client, router_setter,
):
    """Codex CLI (>=0.154) stamps requests with a bare `session-id` header
    (no x- prefix). It must key its own session - falling through to
    "default" would mix Codex traffic with every other headerless
    client and break the stored-history prefix match."""
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="ok"))
    })
    headers = {**AUTH, "session-id": "codex-conv-1",
               "originator": "codex_cli"}

    await client.post(
        "/v1/responses", headers=headers,
        json={"model": "m",
              "input": [{"type": "message", "role": "user",
                         "content": "hi"}]},
    )

    # The user turn landed under the Codex session key, not "default".
    owner = await local_owner_kwargs(app.state.engine)
    history = await app.state.sessions.load("codex-conv-1", **owner)
    assert [m.get("content") for m in history] == ["hi", "ok"]
    default_history = await app.state.sessions.load("default", **owner)
    assert not default_history


async def test_diverged_history_persists_reply_only(client, router_setter):
    """When the resent conversation does not start with the stored
    history (client compacted/rewound), only the assistant reply is
    appended - never the full input."""
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="ok"))
    })
    headers = {**AUTH, "X-Session-Id": "diverged"}

    await client.post(
        "/v1/responses", headers=headers,
        json={"model": "m", "input": [{"type": "message", "role": "user",
                                       "content": "hi"}]},
    )
    # Compacted: the assistant turn is gone from the resent input.
    await client.post(
        "/v1/responses", headers=headers,
        json={"model": "m",
              "input": [{"type": "message", "role": "user",
                         "content": "different"}]},
    )

    history = await app.state.sessions.load(
        "diverged", **await local_owner_kwargs(app.state.engine))
    contents = [m.get("content") for m in history]
    assert contents == ["hi", "ok", "ok"]


async def test_system_instructions_never_accumulate(client, router_setter):
    """The resent system message never lands in the stored history. Each
    turn resends the GROWING conversation (Codex semantics - an identical
    resend would be a retry, which the dedupe correctly treats as no new
    user turn)."""
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="ok"))
    })
    headers = {**AUTH, "X-Session-Id": "no-sys"}
    conversation = [{"type": "message", "role": "user", "content": "hi"}]
    for _ in range(3):
        response = await client.post(
            "/v1/responses", headers=headers,
            json={"instructions": "Be terse.", "input": conversation},
        )
        assert response.status_code == 200
        reply = [i for i in response.json()["output"]
                 if i["type"] == "message"][0]["content"][0]["text"]
        conversation = conversation + [
            {"type": "message", "role": "assistant", "content": reply},
            {"type": "message", "role": "user", "content": "hi"},
        ]
    history = await app.state.sessions.load(
        "no-sys", **await local_owner_kwargs(app.state.engine))
    assert all(m.get("role") != "system" for m in history)
    assert [m["role"] for m in history] == [
        "user", "assistant", "user", "assistant", "user", "assistant",
    ]


# ------------------------------------------------------- BYOK and errors


@pytest.fixture
def byok_env(monkeypatch):
    import invincible.core.url_safety as url_safety

    monkeypatch.setenv(
        "INVINCIBLE_CREDENTIAL_KEY",
        Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(
        url_safety, "_default_resolve", lambda host: ["93.184.216.34"])


async def _mint_key_without_credential(client, email: str) -> dict:
    """A real user + default project + API key, but NO BYOK credential -
    the state that must produce the no-credentials 400."""
    from sqlalchemy import text

    engine = app.state.engine
    async with engine.begin() as conn:
        uid = (await conn.execute(text(
            "INSERT INTO users (email, created_at)"
            " VALUES (:e, 1.0) RETURNING id"
        ), {"e": email})).scalar_one()
        pid = (await conn.execute(text(
            "INSERT INTO projects (user_id, name, is_default, created_at)"
            " VALUES (:u, 'personal', TRUE, 1.0) RETURNING id"
        ), {"u": uid})).scalar_one()
    record = await app.state.api_keys.create(int(uid))
    return {"user_id": int(uid), "project_id": int(pid), "raw": record["raw"]}


async def test_byok_key_without_credentials_gets_400(client, router_setter,
                                                     byok_env):
    record = await _mint_key_without_credential(
        client, "responses-no-cred@example.com")
    response = await client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {record['raw']}"},
        json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 400
    assert "connect" in response.json()["error"]["message"].lower()


async def test_byok_routes_through_own_credential(client, router_setter,
                                                  byok_env):
    from tests.test_isolation import _mint_user_and_key

    captured = []

    def handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha"))

    router_setter({"alpha.example.com": handler})
    record = await _mint_user_and_key(client, "responses-byok@example.com")
    response = await client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {record['raw']}"},
        json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 200
    assert captured, "routed through the BYOK credential's provider"


async def test_invalid_input_is_400_openai_error(client, router_setter):
    router_setter({})
    response = await client.post(
        "/v1/responses", headers=AUTH, json={"model": "m", "input": ""},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["message"]


async def test_upstream_error_detail_is_surfaced(client, router_setter):
    """A non-failover upstream 400 carries the provider's own message in
    the protocol-correct error shape (the generic "Upstream request
    failed" masked the real cause during Codex debugging)."""
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            400,
            json={"error": {"message": "z-ai/glm-5.3-free is not a valid "
                                       "model ID",
                            "type": "invalid_request_error"}},
        ),
    })
    response = await client.post(
        "/v1/responses", headers=AUTH, json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert "not a valid model ID" in error["message"]
    assert error["type"] == "invalid_request_error"


async def test_all_providers_failed_is_503(client, router_setter):
    router_setter(handlers={
        "alpha.example.com": httpx.Response(500, json={"error": "boom"}),
        "beta.example.com": httpx.Response(500, json={"error": "boom"}),
        "gamma.example.com": httpx.Response(500, json={"error": "boom"}),
    })
    response = await client.post(
        "/v1/responses", headers=AUTH, json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "server_error"


async def test_provider_pool_unchanged_by_other_tests(client, router_setter):
    """Sanity: the default operator pool still serves legacy principals
    (the endpoint shares the standard dual-realm auth)."""
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="ok"))
    })
    response = await client.post(
        "/v1/responses", headers=AUTH, json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 200


def test_default_providers_fixture_hosts():
    """The failover test above needs all three default provider hosts."""
    hosts = {p["base_url"].split("//")[1].split("/")[0]
             for p in default_providers()}
    assert hosts == {
        "alpha.example.com", "beta.example.com", "gamma.example.com"}


# ------------------------------------------------- upstream_error_detail


def test_upstream_error_detail_openai_shape():
    assert upstream_error_detail(
        {"error": {"message": "not a valid model ID",
                   "type": "invalid_request_error"}}
    ) == "not a valid model ID"


def test_upstream_error_detail_plain_shapes():
    assert upstream_error_detail({"error": "plain error"}) == "plain error"
    assert upstream_error_detail({"message": "msg"}) == "msg"
    assert upstream_error_detail({"detail": "detail text"}) == "detail text"


def test_upstream_error_detail_unrecognized_returns_none():
    assert upstream_error_detail({"status": 400}) is None
    assert upstream_error_detail({"error": 123}) is None
    assert upstream_error_detail({"error": {"message": "  "}}) is None
    assert upstream_error_detail("not a dict") is None
    assert upstream_error_detail(None) is None


def test_upstream_error_detail_is_capped():
    long_message = "x" * 500
    assert len(upstream_error_detail(
        {"error": {"message": long_message}})) == 300
