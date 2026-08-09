import json

import httpx
import pytest

from invincible.compat.anthropic import (
    anthropic_to_internal,
    build_error,
    flatten_content_blocks,
    translate_finish_reason,
)
from invincible.main import app
from tests.conftest import provider_body, sse_body, stream_chunk

AUTH = {"Authorization": "Bearer test-gateway-key"}
ANTHROPIC_BODY = {
    "model": "claude-sonnet-4",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "hi"}],
}


def _anthropic_events(response):
    """Parse an Anthropic SSE response into [(event, payload), ...]."""
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


# ---------------------------------------------------------------- root probes


async def test_head_root_returns_200(client):
    response = await client.request("HEAD", "/")
    assert response.status_code == 200
    assert response.content == b""


async def test_get_health_detail(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "Invincible"
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]


# ----------------------------------------------------- non-streaming messages


async def test_anthropic_completion_success(client, router_setter):
    alpha_body = provider_body("alpha", content="Hello world")
    router_setter(handlers={"alpha.example.com": httpx.Response(200, json=alpha_body)})
    response = await client.post("/v1/messages", headers=AUTH, json=ANTHROPIC_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["id"].startswith("msg_")
    assert body["model"] == "claude-sonnet-4"
    assert body["content"] == [{"type": "text", "text": "Hello world"}]
    assert body["stop_reason"] == "end_turn"
    assert body["stop_sequence"] is None
    assert body["usage"]["input_tokens"] >= 1
    assert body["usage"]["output_tokens"] >= 1


async def test_anthropic_echoes_requested_model_as_hint(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200, json=provider_body("alpha", content="ok")
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["model"] == "claude-opus-4-8"


async def test_anthropic_system_and_blocks_are_flattened(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-sonnet-4",
            "system": [{"type": "text", "text": "Be concise."}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Explore "},
                        {"type": "image", "source": {"type": "base64", "data": "x"}},
                        {"type": "text", "text": "this"},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    outgoing = captured[0]["messages"]
    assert outgoing[0] == {"role": "system", "content": "Be concise."}
    assert outgoing[1] == {"role": "user", "content": "Explore this"}


async def test_anthropic_tool_blocks_are_preserved_not_degraded(client, router_setter):
    """tool_use/tool_result blocks keep their structure instead of degrading
    to placeholder text, so the Router can send a valid tool conversation
    to OpenAI-compatible providers."""
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="Searched."))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-sonnet-4",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "search",
                            "input": {"query": "x"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": "result text"}],
                        }
                    ],
                },
            ],
        },
    )
    assert response.status_code == 200
    outgoing = captured[0]["messages"]
    assert outgoing == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "toolu_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"query": "x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "result text"},
    ]


# ------------------------------------------------------------------- streaming


async def test_anthropic_streaming_returns_event_stream(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                content=sse_body(
                    stream_chunk("alpha", {"role": "assistant"}),
                    stream_chunk("alpha", {"content": "Hi"}),
                    stream_chunk("alpha", {}, finish_reason="stop"),
                ),
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


async def test_anthropic_streaming_emits_canonical_sequence(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                content=sse_body(
                    stream_chunk("alpha", {"role": "assistant"}),
                    stream_chunk("alpha", {"content": "Hel"}),
                    stream_chunk("alpha", {"content": "lo!"}),
                    stream_chunk("alpha", {}, finish_reason="stop"),
                ),
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    events = _anthropic_events(response)
    assert [e for e, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]

    start_payload = events[0][1]
    assert start_payload["type"] == "message_start"
    assert start_payload["message"]["type"] == "message"
    assert start_payload["message"]["role"] == "assistant"
    assert start_payload["message"]["model"] == "claude-sonnet-4"
    assert start_payload["message"]["content"] == []

    deltas = [p["delta"]["text"] for e, p in events if e == "content_block_delta"]
    assert "".join(deltas) == "Hello!"

    message_delta = [p for e, p in events if e == "message_delta"][0]
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert message_delta["delta"]["stop_sequence"] is None
    assert message_delta["usage"]["output_tokens"] >= 1
    assert events[-1] == ("message_stop", {"type": "message_stop"})


async def test_anthropic_streamed_reply_persisted(client, router_setter):
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
        "X-Session-Id": "shared-convo",
    }
    response = await client.post(
        "/v1/messages",
        headers=headers,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 200

    history = await app.state.sessions.load("shared-convo")
    assistant_messages = [m for m in history if m["role"] == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["content"] == "Hello world"

    await client.post(
        "/v1/messages",
        headers=headers,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    second_outgoing = received_payloads[1]["messages"]
    assert [m["content"] for m in second_outgoing] == ["hi", "Hello world", "hi"]


async def test_cross_protocol_session_sharing(client, router_setter):
    """An OpenAI client on the same session id sees an Anthropic reply, and
    vice versa - because both protocols persist the same internal model."""
    def alpha_handler(request: httpx.Request):
        return httpx.Response(200, json=provider_body("alpha", content="Hello world"))

    router_setter({"alpha.example.com": alpha_handler})
    headers = {"Authorization": "Bearer test-gateway-key", "X-Session-Id": "shared"}

    await client.post(
        "/v1/messages",
        headers=headers,
        json={"messages": [{"role": "user", "content": "introduce yourself"}]},
    )

    history = await app.state.sessions.load("shared")
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["content"] == "Hello world"

    received = []

    def recording_handler(request: httpx.Request):
        received.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="hi"))

    router_setter({"alpha.example.com": recording_handler})
    await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "morning"}]},
    )
    contents = [m["content"] for m in received[0]["messages"]]
    assert "Hello world" in contents


async def test_claude_code_session_id_isolates_history(client, router_setter):
    """x-claude-code-session-id is the session key: session A's history is
    never replayed into session B."""
    received = []

    def recording_handler(request: httpx.Request):
        received.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": recording_handler})

    await client.post(
        "/v1/messages",
        headers={**AUTH, "x-claude-code-session-id": "claude-session-A"},
        json={"messages": [{"role": "user", "content": "secret-from-A"}]},
    )
    history_a = await app.state.sessions.load("claude-session-A")
    assert [m["role"] for m in history_a] == ["user", "assistant"]

    await client.post(
        "/v1/messages",
        headers={**AUTH, "x-claude-code-session-id": "claude-session-B"},
        json={"messages": [{"role": "user", "content": "what is the secret?"}]},
    )
    contents = [m["content"] for m in received[1]["messages"]]
    assert "secret-from-A" not in contents
    assert "what is the secret?" in contents


async def test_claude_code_session_continuity(client, router_setter):
    """Reusing the same x-claude-code-session-id replays prior turns: a
    session stays continuous instead of starting fresh each request."""
    received = []

    def recording_handler(request: httpx.Request):
        received.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="Hello Alice"))

    router_setter({"alpha.example.com": recording_handler})
    headers = {**AUTH, "x-claude-code-session-id": "claude-session-cont"}

    await client.post(
        "/v1/messages",
        headers=headers,
        json={"messages": [{"role": "user", "content": "My name is Alice"}]},
    )
    await client.post(
        "/v1/messages",
        headers=headers,
        json={"messages": [{"role": "user", "content": "What is my name?"}]},
    )
    contents = [m["content"] for m in received[1]["messages"]]
    assert "My name is Alice" in contents
    assert "Hello Alice" in contents
    assert "What is my name?" in contents


async def test_anthropic_x_session_id_used_when_claude_header_absent(
    client, router_setter
):
    """Legacy X-Session-Id clients keep working and keep their history."""
    received = []

    def recording_handler(request: httpx.Request):
        received.append(json.loads(request.read()))
        return httpx.Response(
            200, json=provider_body("alpha", content="Nice to meet you!")
        )

    router_setter({"alpha.example.com": recording_handler})
    headers = {**AUTH, "X-Session-Id": "legacy-session"}

    await client.post(
        "/v1/messages",
        headers=headers,
        json={"messages": [{"role": "user", "content": "My name is Alice"}]},
    )
    await client.post(
        "/v1/messages",
        headers=headers,
        json={"messages": [{"role": "user", "content": "What is my name?"}]},
    )
    contents = [m["content"] for m in received[1]["messages"]]
    assert "My name is Alice" in contents
    assert "Nice to meet you!" in contents


async def test_claude_code_session_id_wins_over_x_session_id(client, router_setter):
    """When both headers are present the Claude Code session id wins."""
    def alpha_handler(request: httpx.Request):
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    headers = {
        **AUTH,
        "x-claude-code-session-id": "claude-session",
        "X-Session-Id": "legacy-session",
    }
    await client.post(
        "/v1/messages",
        headers=headers,
        json={"messages": [{"role": "user", "content": "priority-secret"}]},
    )
    assert [m["content"] for m in await app.state.sessions.load("claude-session")] == [
        "priority-secret",
        "ok",
    ]
    assert await app.state.sessions.load("legacy-session") == []


async def test_anthropic_no_session_header_uses_default(client, router_setter):
    """Clients sending neither header keep using the default session."""
    def alpha_handler(request: httpx.Request):
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    await client.post(
        "/v1/messages",
        headers=AUTH,
        json={"messages": [{"role": "user", "content": "default-convo"}]},
    )
    assert [m["content"] for m in await app.state.sessions.load("default")] == [
        "default-convo",
        "ok",
    ]


async def test_anthropic_midstream_error_terminates_cleanly(client, router_setter):
    stream_fail = _FailingStream(
        sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"content": "partial"}),
            done=False,
        ).encode()
    )
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                stream=stream_fail,
                headers={"content-type": "text/event-stream"},
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 200
    events = _anthropic_events(response)
    event_names = [e for e, _ in events]
    assert "message_start" in event_names
    assert "content_block_delta" in event_names
    assert event_names[-1] == "error"
    error_payload = events[-1][1]
    assert error_payload["type"] == "error"
    assert error_payload["error"]["type"] == "api_error"
    assert not response.text.rstrip().endswith("message_stop")


# ------------------------------------------------------------------ tools


def tool_call_body(provider, name, arguments, tool_call_id="call_1", content=None):
    return {
        "id": f"cmpl-{provider}",
        "model": f"{provider}-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


EXECUTE_BASH_TOOL = {
    "name": "execute_bash",
    "description": "Run a shell command on the host machine.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}


async def test_anthropic_tools_forwarded_to_provider(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-sonnet-4",
            "max_tokens": 1024,
            "tools": [
                EXECUTE_BASH_TOOL,
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
                {
                    "name": "write_file",
                    "description": "Write a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
            ],
            "tool_choice": {"type": "any"},
            "messages": [{"role": "user", "content": "What is my cwd?"}],
        },
    )
    assert response.status_code == 200
    outgoing = captured[0]
    assert [t["function"]["name"] for t in outgoing["tools"]] == [
        "execute_bash",
        "read_file",
        "write_file",
    ]
    assert all(t["type"] == "function" for t in outgoing["tools"])
    assert outgoing["tools"][0]["function"]["parameters"]["required"] == ["command"]
    assert outgoing["tool_choice"] == "required"


async def test_anthropic_tool_use_response(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                json=tool_call_body(
                    "alpha",
                    name="execute_bash",
                    arguments='{"command": "pwd"}',
                    tool_call_id="toolu_pwd",
                ),
            )
        }
    )
    response = await client.post("/v1/messages", headers=AUTH, json=ANTHROPIC_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == [
        {
            "type": "tool_use",
            "id": "toolu_pwd",
            "name": "execute_bash",
            "input": {"command": "pwd"},
        }
    ]
    assert body["stop_reason"] == "tool_use"


async def test_anthropic_tool_turn_round_trips(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="/Users/sark"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-sonnet-4",
            "tools": [EXECUTE_BASH_TOOL],
            "messages": [
                {"role": "user", "content": "What is my current working directory?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "execute_bash",
                            "input": {"command": "pwd"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "/Users/sark",
                        },
                        {"type": "text", "text": "what is it?"},
                    ],
                },
            ],
        },
    )
    assert response.status_code == 200
    outgoing = captured[0]["messages"]
    assert outgoing[1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "execute_bash", "arguments": '{"command": "pwd"}'},
            }
        ],
    }
    assert outgoing[2] == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": "/Users/sark",
    }
    assert outgoing[3] == {"role": "user", "content": "what is it?"}
    assert response.json()["content"] == [{"type": "text", "text": "/Users/sark"}]


async def test_anthropic_streaming_tool_use_events(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                content=sse_body(
                    stream_chunk("alpha", {"role": "assistant"}),
                    stream_chunk(
                        "alpha",
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "toolu_pwd",
                                    "type": "function",
                                    "function": {
                                        "name": "execute_bash",
                                        "arguments": '{"command": "',
                                    },
                                }
                            ]
                        },
                    ),
                    stream_chunk(
                        "alpha",
                        {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "pwd"}}
                            ]
                        },
                    ),
                    stream_chunk(
                        "alpha",
                        {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"}'}}
                            ]
                        },
                    ),
                    stream_chunk("alpha", {}, finish_reason="tool_calls"),
                ),
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 200
    events = _anthropic_events(response)
    assert [e for e, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    start = [p for e, p in events if e == "content_block_start"][0]
    assert start["content_block"] == {
        "type": "tool_use",
        "id": "toolu_pwd",
        "name": "execute_bash",
        "input": {},
    }
    partials = [
        p["delta"]["partial_json"] for e, p in events if e == "content_block_delta"
    ]
    assert "".join(partials) == '{"command": "pwd"}'
    message_delta = [p for e, p in events if e == "message_delta"][0]
    assert message_delta["delta"]["stop_reason"] == "tool_use"


async def test_anthropic_streaming_mixed_text_and_tool(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                content=sse_body(
                    stream_chunk("alpha", {"role": "assistant"}),
                    stream_chunk("alpha", {"content": "Calling "}),
                    stream_chunk(
                        "alpha",
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "toolu_1",
                                    "function": {"name": "Bash", "arguments": "{}"},
                                }
                            ]
                        },
                    ),
                    stream_chunk("alpha", {}, finish_reason="tool_calls"),
                ),
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    events = _anthropic_events(response)
    starts = [
        (p["index"], p["content_block"]["type"])
        for e, p in events
        if e == "content_block_start"
    ]
    assert starts == [(0, "text"), (1, "tool_use")]
    stops = [p["index"] for e, p in events if e == "content_block_stop"]
    assert stops == [0, 1]
    deltas = [
        (p["index"], p["delta"]["type"])
        for e, p in events
        if e == "content_block_delta"
    ]
    assert deltas == [(0, "text_delta"), (1, "input_json_delta")]


async def test_anthropic_streaming_multiple_tool_calls(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                content=sse_body(
                    stream_chunk("alpha", {"role": "assistant"}),
                    stream_chunk(
                        "alpha",
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "toolu_a",
                                    "function": {"name": "read_file", "arguments": "{"},
                                },
                                {
                                    "index": 1,
                                    "id": "toolu_b",
                                    "function": {
                                        "name": "execute_bash",
                                        "arguments": "{",
                                    },
                                },
                            ]
                        },
                    ),
                    stream_chunk(
                        "alpha",
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"path": "a.txt"}'},
                                },
                                {
                                    "index": 1,
                                    "function": {"arguments": '"command": "ls"}'},
                                },
                            ]
                        },
                    ),
                    stream_chunk("alpha", {}, finish_reason="tool_calls"),
                ),
            )
        }
    )
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    events = _anthropic_events(response)
    starts = [
        (p["index"], p["content_block"]["type"], p["content_block"]["name"])
        for e, p in events
        if e == "content_block_start"
    ]
    assert starts == [(0, "tool_use", "read_file"), (1, "tool_use", "execute_bash")]
    partials_by_index = {}
    for e, p in events:
        if e == "content_block_delta":
            partials_by_index.setdefault(p["index"], "")
            partials_by_index[p["index"]] += p["delta"]["partial_json"]
    assert partials_by_index == {
        0: '{"path": "a.txt"}',
        1: '{"command": "ls"}',
    }


async def test_anthropic_malformed_tool_arguments_tolerated(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                200,
                json=tool_call_body(
                    "alpha",
                    name="execute_bash",
                    arguments='{"command": "pwd"',
                ),
            )
        }
    )
    response = await client.post("/v1/messages", headers=AUTH, json=ANTHROPIC_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "execute_bash", "input": {}}
    ]
    assert body["stop_reason"] == "tool_use"


async def test_anthropic_streamed_tool_reply_persisted(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(
            200,
            content=sse_body(
                stream_chunk("alpha", {"role": "assistant"}),
                stream_chunk(
                    "alpha",
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "toolu_pwd",
                                "function": {
                                    "name": "execute_bash",
                                    "arguments": '{"command": "pwd"}',
                                },
                            }
                        ]
                    },
                ),
                stream_chunk("alpha", {}, finish_reason="tool_calls"),
            ),
        )

    router_setter({"alpha.example.com": alpha_handler})
    headers = {**AUTH, "X-Session-Id": "tool-session"}
    response = await client.post(
        "/v1/messages",
        headers=headers,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 200

    history = await app.state.sessions.load("tool-session")
    assistant_messages = [m for m in history if m["role"] == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["content"] is None
    assert assistant_messages[0]["tool_calls"] == [
        {
            "id": "toolu_pwd",
            "type": "function",
            "function": {"name": "execute_bash", "arguments": '{"command": "pwd"}'},
        }
    ]

    await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "model": "claude-sonnet-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_pwd",
                            "content": "/Users/sark",
                        }
                    ],
                }
            ],
        },
    )
    second_outgoing = captured[1]["messages"]
    roles = [m["role"] for m in second_outgoing]
    assert roles == ["user", "assistant", "tool"]
    assert second_outgoing[1]["tool_calls"][0]["id"] == "toolu_pwd"
    assert second_outgoing[2] == {
        "role": "tool",
        "tool_call_id": "toolu_pwd",
        "content": "/Users/sark",
    }


# ------------------------------------------------------------------- failover


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
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 200
    events = _anthropic_events(response)
    assert events[0][0] == "message_start"
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
        "/v1/messages",
        headers=AUTH,
        json={**ANTHROPIC_BODY, "stream": True},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "overloaded_error"


async def test_nonstreaming_failover_to_next_tier(client, router_setter):
    router = router_setter(
        handlers={
            "alpha.example.com": httpx.Response(429),
            "beta.example.com": httpx.Response(
                200, json=provider_body("beta", "hello")
            ),
        }
    )
    response = await client.post("/v1/messages", headers=AUTH, json=ANTHROPIC_BODY)
    assert response.status_code == 200
    assert response.json()["model"] == "claude-sonnet-4"
    assert not router.health_tracker.is_available("alpha")


async def test_all_providers_fail_returns_overloaded(client, router_setter):
    router_setter(handlers={"alpha.example.com": httpx.Response(429)})
    response = await client.post("/v1/messages", headers=AUTH, json=ANTHROPIC_BODY)
    assert response.status_code == 503
    assert response.json() == {
        "type": "error",
        "error": {
            "type": "overloaded_error",
            "message": "All providers failed or are in cooldown.",
        },
    }


async def test_upstream_400_is_mapped_and_sanitized(client, router_setter):
    router_setter(
        handlers={
            "alpha.example.com": httpx.Response(
                400, json={"error": {"message": "internal secret"}}
            )
        }
    )
    response = await client.post("/v1/messages", headers=AUTH, json=ANTHROPIC_BODY)
    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "internal secret" not in json.dumps(body)


# ------------------------------------------------------------------- auth


async def test_anthropic_auth_missing_returns_401(client):
    response = await client.post("/v1/messages", json=ANTHROPIC_BODY)
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


async def test_anthropic_auth_invalid_returns_401(client):
    response = await client.post(
        "/v1/messages",
        headers={"Authorization": "Bearer wrong-key"},
        json=ANTHROPIC_BODY,
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


async def test_anthropic_auth_x_api_key_succeeds(client, router_setter):
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=provider_body("alpha"))}
    )
    response = await client.post(
        "/v1/messages",
        headers={
            "x-api-key": "test-gateway-key",
            "anthropic-version": "2023-06-01",
        },
        json=ANTHROPIC_BODY,
    )
    assert response.status_code == 200


async def test_anthropic_auth_x_api_key_invalid_returns_401(client):
    response = await client.post(
        "/v1/messages",
        headers={"x-api-key": "wrong-key"},
        json=ANTHROPIC_BODY,
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "auth_error"


async def test_anthropic_auth_open_when_key_unset(client, router_setter, monkeypatch):
    router_setter(
        handlers={"alpha.example.com": httpx.Response(200, json=provider_body("alpha"))}
    )
    monkeypatch.delenv("GATEWAY_API_KEY")
    response = await client.post("/v1/messages", json=ANTHROPIC_BODY)
    assert response.status_code == 200


# ------------------------------------------------------------- invalid input


async def test_missing_messages_returns_422(client):
    response = await client.post("/v1/messages", headers=AUTH, json={"model": "x"})
    assert response.status_code == 422


async def test_non_list_messages_returns_422(client):
    response = await client.post(
        "/v1/messages", headers=AUTH, json={"messages": "not a list"}
    )
    assert response.status_code == 422


async def test_empty_messages_returns_400(client):
    response = await client.post("/v1/messages", headers=AUTH, json={"messages": []})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_useless_text_returns_400(client):
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={"messages": [{"role": "user", "content": ""}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_unknown_role_returns_400(client):
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={"messages": [{"role": "admin", "content": "hi"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_developer_and_foo_roles_still_rejected(client):
    for role in ("developer", "foo"):
        response = await client.post(
            "/v1/messages",
            headers=AUTH,
            json={"messages": [{"role": role, "content": "hi"}]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"


async def test_system_role_inside_messages_is_accepted(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "system", "content": "Be precise."},
                {"role": "user", "content": "hi"},
            ],
        },
    )
    assert response.status_code == 200
    assert captured[0]["messages"][0] == {
        "role": "system",
        "content": "Be precise.",
    }


async def test_top_level_and_messages_system_combined(client, router_setter):
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages",
        headers=AUTH,
        json={
            "model": "claude-sonnet-4",
            "system": "Top system.",
            "messages": [
                {"role": "system", "content": "Inner system."},
                {"role": "user", "content": "hi"},
            ],
        },
    )
    assert response.status_code == 200
    systems = [
        m["content"] for m in captured[0]["messages"] if m["role"] == "system"
    ]
    assert systems == ["Top system.", "Inner system."]


async def test_system_messages_not_persisted_to_session(client, router_setter):
    """Repeated requests with a system prompt must not accumulate system
    messages in the persisted session history - user/assistant turns stay."""
    received = []

    def recording_handler(request: httpx.Request):
        received.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": recording_handler})
    headers = {**AUTH, "X-Session-Id": "no-sys-accum"}

    for _ in range(3):
        response = await client.post(
            "/v1/messages",
            headers=headers,
            json={
                "system": "Be concise.",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200

    history = await app.state.sessions.load("no-sys-accum")
    assert [m["role"] for m in history] == [
        "user", "assistant", "user", "assistant", "user", "assistant",
    ]
    assert all(m.get("role") != "system" for m in history)


async def test_system_prompt_still_sent_upstream_each_request(
    client, router_setter
):
    """Every request still sends its own current system prompt upstream, and
    prior requests' system prompts never leak in as stale copies."""
    received = []

    def recording_handler(request: httpx.Request):
        received.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": recording_handler})
    headers = {**AUTH, "X-Session-Id": "sys-per-request"}

    await client.post(
        "/v1/messages",
        headers=headers,
        json={"system": "System-One", "messages": [{"role": "user", "content": "a"}]},
    )
    await client.post(
        "/v1/messages",
        headers=headers,
        json={"system": "System-Two", "messages": [{"role": "user", "content": "b"}]},
    )

    first_systems = [
        m["content"] for m in received[0]["messages"] if m["role"] == "system"
    ]
    second_systems = [
        m["content"] for m in received[1]["messages"] if m["role"] == "system"
    ]
    assert first_systems == ["System-One"]
    assert second_systems == ["System-Two"]


async def test_tool_history_intact_with_system(client, router_setter):
    """Tool-call/tool-result history stays intact in the persisted session
    while system messages are excluded."""
    received = []

    def recording_handler(request: httpx.Request):
        received.append(json.loads(request.read()))
        if len(received) == 1:
            return httpx.Response(
                200,
                json=tool_call_body(
                    "alpha",
                    name="execute_bash",
                    arguments='{"command": "pwd"}',
                    tool_call_id="toolu_persist",
                ),
            )
        return httpx.Response(200, json=provider_body("alpha", content="done"))

    router_setter({"alpha.example.com": recording_handler})
    headers = {**AUTH, "X-Session-Id": "tool-sys-session"}

    await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "system": "Use tools.",
            "tools": [EXECUTE_BASH_TOOL],
            "messages": [{"role": "user", "content": "what dir?"}],
        },
    )
    await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "system": "Use tools.",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_persist",
                            "content": "/c/Users/SARK/Desktop/ai-gateway",
                        }
                    ],
                }
            ],
        },
    )

    history = await app.state.sessions.load("tool-sys-session")
    assert all(m.get("role") != "system" for m in history)
    assistant_with_tool = [
        m for m in history if m.get("tool_calls")
    ]
    assert len(assistant_with_tool) == 1
    assert assistant_with_tool[0]["tool_calls"][0]["id"] == "toolu_persist"
    tool_msgs = [m for m in history if m.get("role") == "tool"]
    assert tool_msgs[0]["tool_call_id"] == "toolu_persist"
    assert "ai-gateway" in tool_msgs[0]["content"]


async def test_optional_fields_ignored_but_tools_forwarded(client, router_setter):
    """metadata / temperature / top_p / top_k / stop_sequences / unknown
    fields / beta query must never produce a 422, while tools and
    tool_choice are translated and forwarded to the provider."""
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/messages?beta=true",
        headers={
            **AUTH,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "tools-2024-04-04",
        },
        json={
            "model": "claude-sonnet-4",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "tools": [{"name": "search", "description": "Search", "input_schema": {}}],
            "tool_choice": {"type": "auto"},
            "metadata": {"user_id": "abc"},
            "temperature": 0.5,
            "top_p": 0.9,
            "top_k": 5,
            "stop_sequences": ["\\n"],
        },
    )
    assert response.status_code == 200
    assert response.json()["type"] == "message"
    outgoing = captured[0]
    assert outgoing["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search",
                "parameters": {},
            },
        }
    ]
    assert outgoing["tool_choice"] == "auto"


# ------------------------------------------------------------- pure helpers


async def test_translate_finish_reason_mapping():
    assert translate_finish_reason("stop") == "end_turn"
    assert translate_finish_reason("length") == "max_tokens"
    assert translate_finish_reason("tool_calls") == "tool_use"
    assert translate_finish_reason(None) == "end_turn"
    assert translate_finish_reason("bogus") == "end_turn"


async def test_flatten_content_blocks_matrix():
    assert flatten_content_blocks("plain", "user") == "plain"
    assert (
        flatten_content_blocks(
            [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "user"
        )
        == "ab"
    )
    assert (
        flatten_content_blocks(
            [{"type": "tool_use", "name": "search", "input": {}}], "assistant"
        )
        == "[tool_use: search]"
    )
    assert (
        flatten_content_blocks([{"type": "tool_result", "content": "out"}], "user")
        == "out"
    )
    assert flatten_content_blocks(123, "user") == ""
    assert flatten_content_blocks(None, "user") == ""


async def test_build_error_mapping():
    assert build_error(400, "x")[0] == 400
    assert build_error(401, "x")[1]["error"]["type"] == "authentication_error"
    assert build_error(403, "x")[1]["error"]["type"] == "permission_error"
    assert build_error(404, "x")[1]["error"]["type"] == "not_found_error"
    assert build_error(429, "x")[1]["error"]["type"] == "rate_limit_error"
    assert build_error(500, "x")[1]["error"]["type"] == "api_error"
    assert build_error(503, "x")[1]["error"]["type"] == "overloaded_error"
    assert build_error(599, "x")[1]["error"]["type"] == "api_error"


async def test_anthropic_to_internal_promotes_system():
    internal = anthropic_to_internal(
        [{"role": "user", "content": "hi"}], system="Be nice."
    )
    assert internal[0] == {"role": "system", "content": "Be nice."}
    assert internal[1] == {"role": "user", "content": "hi"}


async def test_anthropic_to_internal_accepts_system_role_in_messages():
    internal = anthropic_to_internal(
        [
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "hi"},
        ]
    )
    assert internal[0] == {"role": "system", "content": "Be precise."}
    assert internal[1] == {"role": "user", "content": "hi"}


async def test_anthropic_to_internal_rejects_empty():
    with pytest.raises(ValueError):
        anthropic_to_internal([])
    with pytest.raises(ValueError):
        anthropic_to_internal([{"role": "user", "content": ""}])
