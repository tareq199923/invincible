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
from invincible.core.credential_store import ByokCredentialStore
from invincible.core.identity import ensure_default_project
from invincible.core.user_settings_store import UserSettingsStore
from invincible.main import app
from tests.conftest import (
    default_providers,
    provider_body,
    sse_body,
    stream_chunk,
    v1_user,
)


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


async def _user_kwargs(uid: int) -> dict:
    """Store-level kwargs for the inv_ user a v1_user mint resolved."""
    return {"user_id": uid,
            "project_id": await ensure_default_project(app.state.engine, uid)}


async def test_response_object_shape(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="Hello world"))
    })
    response = await client.post(
        "/v1/responses",
        headers=auth,
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


async def test_plain_string_input(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="ok"))
    })
    response = await client.post(
        "/v1/responses", headers=auth,
        json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


async def test_instructions_become_system_upstream(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/responses",
        headers=auth,
        json={"instructions": "You are terse.",
              "input": "hi"},
    )
    assert response.status_code == 200
    outgoing = captured[0]["messages"]
    assert outgoing[0] == {"role": "system", "content": "You are terse."}
    assert outgoing[-1] == {"role": "user", "content": "hi"}


# ------------------------------------------------- tool calls round-trip


async def test_function_call_items_round_trip(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/responses",
        headers=auth,
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


async def test_parallel_function_calls_merge_into_one_assistant(
    client, router_setter, byok_env
):
    """Responses renders parallel tool calls as sibling function_call
    items followed by their outputs. Each must NOT become its own
    assistant message: strict chat-completions validators require every
    tool message's id to appear in the IMMEDIATELY preceding assistant's
    tool_calls (upstream 400: "tool message must follow assistant tool
    calls")."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/responses",
        headers=auth,
        json={
            "model": "m",
            "input": [
                {"type": "message", "role": "user",
                 "content": "run both tools"},
                {"type": "function_call", "call_id": "call_A",
                 "name": "search", "arguments": '{"query": "x"}'},
                {"type": "function_call", "call_id": "call_B",
                 "name": "shell", "arguments": '{"cmd": "ls"}'},
                {"type": "function_call_output", "call_id": "call_A",
                 "output": "result A"},
                {"type": "function_call_output", "call_id": "call_B",
                 "output": "result B"},
                {"type": "message", "role": "user", "content": "thanks"},
            ],
        },
    )
    assert response.status_code == 200
    outgoing = captured[0]["messages"]
    assert outgoing == [
        {"role": "user", "content": "run both tools"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_A",
                    "type": "function",
                    "function": {"name": "search",
                                 "arguments": '{"query": "x"}'},
                },
                {
                    "id": "call_B",
                    "type": "function",
                    "function": {"name": "shell",
                                 "arguments": '{"cmd": "ls"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_A", "content": "result A"},
        {"role": "tool", "tool_call_id": "call_B", "content": "result B"},
        {"role": "user", "content": "thanks"},
    ]


async def test_assistant_text_joins_following_function_calls(
    client, router_setter, byok_env
):
    """An assistant message item directly before function_call items is
    the same turn: one assistant message with content AND tool_calls."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/responses",
        headers=auth,
        json={
            "model": "m",
            "input": [
                {"type": "message", "role": "user", "content": "go"},
                {"type": "message", "role": "assistant",
                 "content": "I'll search for that."},
                {"type": "function_call", "call_id": "call_1",
                 "name": "search", "arguments": '{"query": "x"}'},
                {"type": "function_call_output", "call_id": "call_1",
                 "output": "found it"},
            ],
        },
    )
    assert response.status_code == 200
    outgoing = captured[0]["messages"]
    assert outgoing == [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "I'll search for that.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search",
                                 "arguments": '{"query": "x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "found it"},
    ]


async def test_tool_choice_without_tools_is_dropped(client, router_setter, byok_env):
    """Codex occasionally sends tool_choice on a turn with no tools; every
    OpenAI-compatible upstream 400s on that pair, so the router must drop
    tool_choice when the tools list is empty."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/responses",
        headers=auth,
        json={"model": "m", "input": "hi",
              "tools": [], "tool_choice": "auto"},
    )
    assert response.status_code == 200
    assert "tool_choice" not in captured[0]
    assert "tools" not in captured[0]


async def test_tools_and_tool_choice_translate(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    response = await client.post(
        "/v1/responses",
        headers=auth,
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
    client, router_setter, byok_env
):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        "/v1/responses", headers=auth, json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    calls = [i for i in body["output"] if i["type"] == "function_call"]
    assert len(calls) == 1
    assert calls[0]["call_id"] == "call_9"
    assert calls[0]["name"] == "shell"
    assert json.loads(calls[0]["arguments"]) == {"cmd": "ls"}


# ------------------------------------------------- tool-call pairing repair
#
# DeepSeek (vLLM on NVIDIA NIM) rejects any request where an assistant
# tool_calls turn is not immediately followed by tool messages covering
# ALL of its ids. repair_tool_pairing() is the pre-routing invariant.


def test_repair_tool_pairing_valid_history_is_unchanged():
    from invincible.compat.common import repair_tool_pairing

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    assert repair_tool_pairing(messages) == messages


def test_repair_tool_pairing_out_of_order_tool_results_fold_back():
    from invincible.compat.common import repair_tool_pairing

    # The second tool result arrives BEFORE the first (the exact shape
    # an interleaved client replay produces); the invariant demands it
    # follow the assistant turn in id order.
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "f", "arguments": "{}"}},
            {"id": "call_b", "type": "function",
             "function": {"name": "g", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_b", "content": "b"},
        {"role": "tool", "tool_call_id": "call_a", "content": "a"},
    ]
    repaired = repair_tool_pairing(messages)
    # Both results now sit directly after the assistant turn (arrival
    # order is preserved among them); no non-tool message intervenes.
    tool_ids = [m["tool_call_id"] for m in repaired if m.get("role") == "tool"]
    assert tool_ids == ["call_b", "call_a"]
    roles = [m["role"] for m in repaired]
    assert roles == ["user", "assistant", "tool", "tool"]


def test_repair_tool_pairing_interleaved_non_tool_message_folds():
    from invincible.compat.common import repair_tool_pairing

    # A user message slipped between the assistant turn and one of its
    # results - upstream would see an under-covered assistant turn.
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "f", "arguments": "{}"}},
            {"id": "call_b", "type": "function",
             "function": {"name": "g", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": "a"},
        {"role": "user", "content": "meanwhile"},
        {"role": "tool", "tool_call_id": "call_b", "content": "b"},
    ]
    repaired = repair_tool_pairing(messages)
    roles = [m["role"] for m in repaired]
    # call_b's result moved directly after the assistant turn, before
    # the interposed user message.
    assert roles == ["user", "assistant", "tool", "tool", "user"]


def test_repair_tool_pairing_missing_call_id_gets_synthetic():
    from invincible.compat.common import repair_tool_pairing

    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "", "type": "function",
             "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "", "content": "ok"},
    ]
    repaired = repair_tool_pairing(messages)
    call = repaired[0]["tool_calls"][0]
    assert call["id"] == "call_missing_0_0"
    assert repaired[1]["tool_call_id"] == "call_missing_0_0"


def test_repair_tool_pairing_dangling_tool_call_dropped():
    from invincible.compat.common import repair_tool_pairing

    # The model announced a second call that never got a result; keeping
    # it would make upstream reject the whole request.
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "f", "arguments": "{}"}},
            {"id": "call_b", "type": "function",
             "function": {"name": "g", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": "a"},
    ]
    repaired = repair_tool_pairing(messages)
    assistant = [m for m in repaired if m.get("tool_calls")][0]
    assert [c["id"] for c in assistant["tool_calls"]] == ["call_a"]


def test_repair_tool_pairing_unpairable_tool_result_raises():
    from invincible.compat.common import repair_tool_pairing

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "call_ghost", "content": "?"}
    ]
    with pytest.raises(ValueError, match="messages\\[1\\]"):
        repair_tool_pairing(messages)


def test_repair_tool_pairing_result_before_call_is_buffered():
    from invincible.compat.common import repair_tool_pairing

    # A tool result arriving before its assistant turn is buffered and
    # folded in once that turn claims its id.
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "call_late", "content": "early"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_late", "type": "function",
             "function": {"name": "f", "arguments": "{}"}},
        ]},
    ]
    repaired = repair_tool_pairing(messages)
    roles = [m["role"] for m in repaired]
    assert roles == ["user", "assistant", "tool"]


def test_repair_tool_pairing_non_dict_message_raises():
    from invincible.compat.common import repair_tool_pairing

    with pytest.raises(ValueError, match="messages\\[2\\] must be an object"):
        repair_tool_pairing([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            "not a dict",
        ])


# ------------------------------------------------------------------ streaming


async def test_streaming_canonical_sequence(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"content": "Hel"}),
            stream_chunk("alpha", {"content": "lo!"}),
            stream_chunk("alpha", {}, finish_reason="stop"),
        ))
    })
    response = await client.post(
        "/v1/responses", headers=auth,
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


async def test_streaming_function_calls(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        "/v1/responses", headers=auth,
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


async def test_streaming_split_tool_calls_across_chunks_keep_ids(
    client, router_setter, byok_env
):
    """Two tool calls streamed over separate chunks (the NIM/vLLM shape
    for a second call) keep DISTINCT ids on the wire AND in persistence -
    the second id may not be re-allocated or merged into the first."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}

    def alpha_handler(request: httpx.Request):
        return httpx.Response(
            200,
            content=sse_body(
                stream_chunk("alpha", {"role": "assistant"}),
                stream_chunk("alpha", {"tool_calls": [
                    {"index": 0, "id": "call_read", "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": '{"path": "a.py"}'}},
                ]}),
                stream_chunk("alpha", {"tool_calls": [
                    # Second call in its own chunk: NIM streams it with a
                    # fresh index and its own id.
                    {"index": 1, "id": "call_retry", "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": '{"path": "a.py"}'}},
                ]}),
                stream_chunk("alpha", {}, finish_reason="tool_calls"),
            ),
        )

    router_setter({"alpha.example.com": alpha_handler})
    headers = {**auth, "X-Session-Id": "split-tools"}
    response = await client.post(
        "/v1/responses", headers=headers,
        json={"model": "m", "input": "read the file", "stream": True},
    )
    assert response.status_code == 200

    events = _responses_events(response)
    completed = [p for name, p in events
                 if name == "response.completed"][0]["response"]
    calls = [i for i in completed["output"] if i["type"] == "function_call"]
    assert [(c["call_id"], c["name"]) for c in calls] == [
        ("call_read", "read_file"), ("call_retry", "read_file")]

    # Persistence must record the very same ids the client saw.
    history = await app.state.sessions.load(
        "split-tools", **await _user_kwargs(uid))
    assistant = [m for m in history if m["role"] == "assistant"][0]
    assert [c["id"] for c in assistant["tool_calls"]] == [
        "call_read", "call_retry"]


async def test_streaming_missing_second_tool_call_id_single_sourced(
    client, router_setter, byok_env
):
    """NIM/vLLM omitting the second tool call's id must not fork the id:
    the wire (output_item.added, response.completed) and the persisted
    assistant turn all carry ONE allocated id."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200,
            content=sse_body(
                stream_chunk("alpha", {"role": "assistant"}),
                stream_chunk("alpha", {"tool_calls": [
                    {"index": 0, "id": "call_first", "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": '{"path": "a.py"}'}},
                ]}),
                stream_chunk("alpha", {"tool_calls": [
                    # id missing entirely - the vLLM post-processing
                    # failure mode.
                    {"index": 1, "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": '{"path": "a.py"}'}},
                ]}),
                stream_chunk("alpha", {}, finish_reason="tool_calls"),
            ),
        )
    })
    response = await client.post(
        "/v1/responses", headers=auth,
        json={"model": "m", "input": "read the file", "stream": True},
    )
    assert response.status_code == 200

    events = _responses_events(response)
    added = [p for p in events if p[0] == "response.output_item.added"]
    function_added = [p[1]["item"] for p in added
                      if p[1]["item"]["type"] == "function_call"]
    completed = [p for name, p in events
                 if name == "response.completed"][0]["response"]
    completed_calls = [i for i in completed["output"]
                       if i["type"] == "function_call"]

    ids_on_wire = [item["call_id"] for item in function_added]
    ids_completed = [c["call_id"] for c in completed_calls]
    # Two calls, two DISTINCT ids, none empty.
    assert len(ids_on_wire) == 2
    assert all(ids_on_wire)
    assert len(set(ids_on_wire)) == 2
    assert ids_completed == ids_on_wire

    # The failed second call was not merged into the first.
    assert completed_calls[0]["call_id"] == "call_first"
    assert completed_calls[1]["call_id"] != "call_first"


async def test_streaming_index_collision_with_new_id_creates_new_call(
    client, router_setter, byok_env
):
    """A reused upstream index carrying a DIFFERENT id is a new tool
    call, not a merge - both calls survive with their own arguments."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200,
            content=sse_body(
                stream_chunk("alpha", {"role": "assistant"}),
                stream_chunk("alpha", {"tool_calls": [
                    {"index": 0, "id": "call_one", "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": '{"path": "one.txt"}'}},
                ]}),
                stream_chunk("alpha", {"tool_calls": [
                    # Same index 0, different id - vLLM index reuse.
                    {"index": 0, "id": "call_two", "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": '{"path": "two.txt"}'}},
                ]}),
                stream_chunk("alpha", {}, finish_reason="tool_calls"),
            ),
        )
    })
    response = await client.post(
        "/v1/responses", headers=auth,
        json={"model": "m", "input": "read the files", "stream": True},
    )
    assert response.status_code == 200

    completed = [p for name, p in _responses_events(response)
                 if name == "response.completed"][0]["response"]
    calls = [i for i in completed["output"] if i["type"] == "function_call"]
    assert [(c["call_id"], json.loads(c["arguments"])) for c in calls] == [
        ("call_one", {"path": "one.txt"}),
        ("call_two", {"path": "two.txt"}),
    ]


async def test_streaming_index_collision_with_empty_id_not_merged(
    client, router_setter, byok_env
):
    """A second call on a REUSED index whose id the upstream omitted
    (the NIM failure mode) still becomes its own tool state: only an
    empty id differs from the known one, but the re-sent function name
    is the tell - a genuine continuation delta never carries a name, so
    a different name on an open index starts a NEW call with a freshly
    allocated id (a missing id/name never corrupts the first call)."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200,
            content=sse_body(
                stream_chunk("alpha", {"role": "assistant"}),
                stream_chunk("alpha", {"tool_calls": [
                    {"index": 0, "id": "call_alpha", "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": '{"path": "a.py"}'}},
                ]}),
                stream_chunk("alpha", {"tool_calls": [
                    # Index reused, id omitted, different tool: new call.
                    {"index": 0, "type": "function",
                     "function": {"name": "list_dir",
                                  "arguments": '{"path": "."}'}},
                ]}),
                stream_chunk("alpha", {}, finish_reason="tool_calls"),
            ),
        )
    })
    response = await client.post(
        "/v1/responses", headers=auth,
        json={"model": "m", "input": "read the files", "stream": True},
    )
    assert response.status_code == 200

    completed = [p for name, p in _responses_events(response)
                 if name == "response.completed"][0]["response"]
    calls = [i for i in completed["output"] if i["type"] == "function_call"]
    assert len(calls) == 2, calls
    assert calls[0]["call_id"] == "call_alpha"
    assert calls[0]["name"] == "read_file"
    assert calls[0]["arguments"] == '{"path": "a.py"}'
    assert calls[1]["call_id"] != "call_alpha"
    assert calls[1]["name"] == "list_dir"
    assert calls[1]["arguments"] == '{"path": "."}'


async def test_streaming_same_index_empty_id_no_name_is_continuation(
    client, router_setter, byok_env
):
    """The complementary protocol case: a delta on an existing index
    with no id and no name is an arguments fragment of the call already
    open on that index - it must continue it, never fork a new state."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"tool_calls": [
                {"index": 0, "id": "call_x", "type": "function",
                 "function": {"name": "f", "arguments": '{"a"'}},
            ]}),
            # Same index, no id, no name: pure arguments continuation.
            stream_chunk("alpha", {"tool_calls": [
                {"index": 0, "function": {"arguments": ': 1}'}},
            ]}),
            stream_chunk("alpha", {}, finish_reason="tool_calls"),
        ))
    })
    response = await client.post(
        "/v1/responses", headers=auth,
        json={"model": "m", "input": "hi", "stream": True},
    )
    assert response.status_code == 200
    events = _responses_events(response)
    completed = [p for name, p in events
                 if name == "response.completed"][0]["response"]
    fc = [i for i in completed["output"] if i["type"] == "function_call"]
    # Exactly one function_call item, with the full concatenated args.
    assert len(fc) == 1
    assert fc[0]["call_id"] == "call_x"
    assert json.loads(fc[0]["arguments"]) == {"a": 1}


async def test_responses_to_internal_out_of_order_fc_fco_repaired():
    """Out-of-order/interleaved function_call/function_call_output items
    are repaired into a DeepSeek-valid pairing instead of forwarded as
    the strict left-to-right fold's dangling assistant turn."""
    internal = responses_to_internal([
        {"type": "message", "role": "user", "content":
            [{"type": "input_text", "text": "hi"}]},
        {"type": "function_call", "call_id": "call_a", "name": "f",
         "arguments": "{}"},
        {"type": "function_call", "call_id": "call_b", "name": "g",
         "arguments": "{}"},
        # The SECOND output arrives first - the repro's exact shape.
        {"type": "function_call_output", "call_id": "call_b", "output": "b"},
        {"type": "function_call_output", "call_id": "call_a", "output": "a"},
        {"type": "message", "role": "user", "content":
            [{"type": "input_text", "text": "go on"}]},
    ])
    roles = [m["role"] for m in internal]
    # Everything folds back directly after the assistant turn; the
    # trailing user message stays last.
    assert roles == ["user", "assistant", "tool", "tool", "user"]
    tool_ids = [m["tool_call_id"] for m in internal if m["role"] == "tool"]
    assert sorted(tool_ids) == ["call_a", "call_b"]


async def test_streaming_split_json_data_lines_no_failover(
    client, router_setter, byok_env
):
    """A tool-call JSON chunk split across two data: lines is buffered
    and parsed - the stream survives instead of triggering malformed-SSE
    failover (reserved for a malformed FIRST chunk)."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    tool_chunk = json.dumps({
        "id": "chatcmpl-x", "object": "chat.completion.chunk", "model": "m",
        "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_split", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path"}'}}]}},
        ],
    })
    done_chunk = json.dumps({
        "id": "chatcmpl-x", "object": "chat.completion.chunk", "model": "m",
        "choices": [{"index": 0, "delta": {},
                     "finish_reason": "tool_calls"}],
    })
    # Split the tool chunk's JSON across two data: lines with NO blank
    # line between them (a blank line would end the SSE event).
    cut = len(tool_chunk) // 2
    raw = (
        f"data: {tool_chunk[:cut]}\n"
        f"data: {tool_chunk[cut:]}\n"
        f"data: {done_chunk}\n"
        "data: [DONE]\n\n"
    ).encode()

    class _SplitStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw

        async def aclose(self):
            pass

    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=_SplitStream())
    })
    response = await client.post(
        "/v1/responses", headers=auth,
        json={"model": "m", "input": "read the file", "stream": True},
    )
    # The split chunk parsed as ONE event and the stream completed
    # normally - no failover, no malformed-SSE teardown.
    assert response.status_code == 200
    completed = [p for name, p in _responses_events(response)
                 if name == "response.completed"][0]["response"]
    calls = [i for i in completed["output"] if i["type"] == "function_call"]
    assert calls[0]["call_id"] == "call_split"
    assert calls[0]["name"] == "read_file"


async def test_debug_stream_on_writes_request_id_keyed_file(
    client, router_setter, byok_env, monkeypatch, tmp_path
):
    """INVINCIBLE_DEBUG_STREAM on: a per-request dump keyed by the
    gateway request id captures the outgoing payload + raw chunks."""
    monkeypatch.setenv("INVINCIBLE_DEBUG_STREAM", "1")
    monkeypatch.chdir(tmp_path)
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200,
            content=sse_body(
                stream_chunk("alpha", {"role": "assistant"}),
                stream_chunk("alpha", {"content": "Hi"}),
                stream_chunk("alpha", {}, finish_reason="stop"),
            ),
        )
    })
    response = await client.post(
        "/v1/responses", headers=auth,
        json={"model": "m", "input": "hi", "stream": True},
    )
    assert response.status_code == 200
    assert "response.completed" in response.text

    dumps = list(tmp_path.glob("debug_stream_*.json"))
    assert dumps, "expected a debug_stream_<request_id>.json dump"
    payload = json.loads(dumps[0].read_text(encoding="utf-8"))
    assert payload["request_id"]
    assert payload["provider"] == "alpha"
    assert payload["chunk_count"] == len(payload["chunks"]) > 0
    assert payload["chunks"][0]["object"] == "chat.completion.chunk"


async def test_debug_stream_off_writes_nothing(
    client, router_setter, byok_env, monkeypatch, tmp_path
):
    """INVINCIBLE_DEBUG_STREAM unset: completely silent - no dump files,
    no behavior change."""
    monkeypatch.chdir(tmp_path)
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200,
            content=sse_body(
                stream_chunk("alpha", {"role": "assistant"}),
                stream_chunk("alpha", {"content": "Hi"}),
                stream_chunk("alpha", {}, finish_reason="stop"),
            ),
        )
    })
    response = await client.post(
        "/v1/responses", headers=auth,
        json={"model": "m", "input": "hi", "stream": True},
    )
    assert response.status_code == 200
    assert "response.completed" in response.text
    assert not list(tmp_path.glob("debug_stream_*.json"))


async def test_stream_persists_session_history(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"content": "Hi"}),
            stream_chunk("alpha", {}, finish_reason="stop"),
        ))
    })
    response = await client.post(
        "/v1/responses",
        headers={**auth, "X-Session-Id": "resp-stream"},
        json={"model": "m", "input": "hello", "stream": True},
    )
    assert response.status_code == 200
    # Consume the body so the stream generator (and its persistence
    # callback) runs to completion before we read the store.
    assert "response.completed" in response.text
    history = await app.state.sessions.load(
        "resp-stream", **await _user_kwargs(uid))
    assert history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi"},
    ]


async def test_mid_stream_failure_closes_well_formed(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
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
        "/v1/responses", headers=auth,
        json={"model": "m", "input": "hi", "stream": True},
    )
    assert response.status_code == 200
    events = _responses_events(response)
    names = [name for name, _ in events]
    # One error frame, no completed event, stream closed cleanly.
    assert "error" in names
    assert "response.completed" not in names


# --------------------------------------------- prefix-dedupe persistence


async def test_resent_conversation_does_not_duplicate(client, router_setter, byok_env):
    """Codex resends the full conversation each turn: the second request
    must not duplicate history upstream nor in the stored session."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    captured = []

    def alpha_handler(request: httpx.Request):
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body("alpha", content="ok"))

    router_setter({"alpha.example.com": alpha_handler})
    headers = {**auth, "X-Session-Id": "dedupe"}

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
        "dedupe", **await _user_kwargs(uid))
    contents = [m.get("content") for m in history]
    assert contents == ["hi", "ok", "again", "ok"]


async def test_codex_session_id_header_creates_its_own_session(
    client, router_setter, byok_env
):
    """Codex CLI (>=0.154) stamps requests with a bare `session-id` header
    (no x- prefix). It must key its own session - falling through to
    "default" would mix Codex traffic with every other headerless
    client and break the stored-history prefix match."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="ok"))
    })
    headers = {**auth, "session-id": "codex-conv-1",
               "originator": "codex_cli"}

    await client.post(
        "/v1/responses", headers=headers,
        json={"model": "m",
              "input": [{"type": "message", "role": "user",
                         "content": "hi"}]},
    )

    # The user turn landed under the Codex session key, not "default".
    owner = await _user_kwargs(uid)
    history = await app.state.sessions.load("codex-conv-1", **owner)
    assert [m.get("content") for m in history] == ["hi", "ok"]
    default_history = await app.state.sessions.load("default", **owner)
    assert not default_history


async def test_diverged_history_persists_reply_only(client, router_setter, byok_env):
    """When the resent conversation does not start with the stored
    history (client compacted/rewound), only the assistant reply is
    appended - never the full input."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="ok"))
    })
    headers = {**auth, "X-Session-Id": "diverged"}

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
        "diverged", **await _user_kwargs(uid))
    contents = [m.get("content") for m in history]
    assert contents == ["hi", "ok", "ok"]


async def test_system_instructions_never_accumulate(client, router_setter, byok_env):
    """The resent system message never lands in the stored history. Each
    turn resends the GROWING conversation (Codex semantics - an identical
    resend would be a retry, which the dedupe correctly treats as no new
    user turn)."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="ok"))
    })
    headers = {**auth, "X-Session-Id": "no-sys"}
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
        "no-sys", **await _user_kwargs(uid))
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


async def test_invalid_input_is_400_openai_error(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter({})
    response = await client.post(
        "/v1/responses", headers=auth, json={"model": "m", "input": ""},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["message"]


async def test_upstream_error_detail_is_surfaced(client, router_setter, byok_env):
    """A non-failover upstream 400 carries the provider's own message in
    the protocol-correct error shape (the generic "Upstream request
    failed" masked the real cause during Codex debugging)."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            400,
            json={"error": {"message": "z-ai/glm-5.3-free is not a valid "
                                       "model ID",
                            "type": "invalid_request_error"}},
        ),
    })
    response = await client.post(
        "/v1/responses", headers=auth, json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert "not a valid model ID" in error["message"]
    assert error["type"] == "invalid_request_error"


async def test_all_providers_failed_is_503(client, router_setter, byok_env):
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(500, json={"error": "boom"}),
        "beta.example.com": httpx.Response(500, json={"error": "boom"}),
        "gamma.example.com": httpx.Response(500, json={"error": "boom"}),
    })
    response = await client.post(
        "/v1/responses", headers=auth, json={"model": "m", "input": "hi"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "server_error"


async def test_provider_pool_unchanged_by_other_tests(client, router_setter, byok_env):
    """Sanity: the caller's own credential pool serves every request -
    routing is per-principal, never a shared pool."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="ok"))
    })
    response = await client.post(
        "/v1/responses", headers=auth, json={"model": "m", "input": "hi"},
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


# ------------------------------- served-model reporting (Codex status line)


async def _chain_user(client, email, step_models, providers=None):
    """A v1 user whose saved chain pairs the first ``len(step_models)``
    connected credentials with ``step_models`` in order. Returns the raw
    inv_ key."""
    uid, raw_key = await v1_user(client, email, providers=providers)
    rows = await ByokCredentialStore(app.state.engine).list_for_user(uid)
    chained = rows[:len(step_models)]
    assert len(chained) == len(step_models), (
        "not enough connected credentials for the requested chain steps")
    await UserSettingsStore(app.state.engine).save_routing(uid, {
        "mode": "chain",
        "chain": [{"credential_id": row["id"], "model": model}
                  for row, model in zip(chained, step_models, strict=True)],
    })
    return raw_key


async def test_response_reports_serving_model_not_requested(
    client, router_setter, byok_env
):
    """``model`` reports the step that actually served, not the request's
    hint.

    Under chain routing a step carries its own model, so the two differ
    whenever the request names no step (or a step other than the one that
    replied). Codex renders this field as its status-line model, so
    echoing the request would make a cross-model fallback invisible.
    """
    raw_key = await _chain_user(client, "served@example.com", ["alpha-step"])
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="Hello world"))
    })
    response = await client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "requested-elsewhere", "input": "hi"},
    )
    assert response.status_code == 200, response.text
    # The step's model ran, and the body agrees with the route headers.
    assert response.headers["x-invincible-model"] == "alpha-step"
    assert response.json()["model"] == "alpha-step"


async def test_stream_reports_serving_model_not_requested(
    client, router_setter, byok_env
):
    """The SSE path reports the serving model on every frame, so Codex's
    status line stays honest mid-stream (response.created fires before the
    first upstream chunk, but the winning attempt is already known)."""
    raw_key = await _chain_user(
        client, "served-stream@example.com", ["alpha-step"])
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"content": "Hi"}),
            stream_chunk("alpha", {}, finish_reason="stop"),
        ))
    })
    response = await client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "requested-elsewhere", "input": "hi", "stream": True},
    )
    assert response.status_code == 200
    events = _responses_events(response)
    assert dict(events)["response.created"]["response"]["model"] == (
        "alpha-step")
    completed = [p for name, p in events
                 if name == "response.completed"][0]["response"]
    assert completed["model"] == "alpha-step"


async def test_auto_mode_still_reports_the_requested_model(
    client, router_setter, byok_env
):
    """The complement: with no routing configured (auto), the per-request
    model override makes every candidate call the requested model, so the
    reported model is unchanged - this change only moves chain/pinned."""
    uid, raw_key = await v1_user(client, "auto-model@example.com")
    router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200, json=provider_body("alpha", content="Hello world"))
    })
    response = await client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "gpt-5.6-terra", "input": "hi"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "gpt-5.6-terra"


# ------------------------------------- streaming tool-call assembly (NIM/vLLM)
#
# vLLM-backed deployments (NVIDIA NIM serving DeepSeek) have been observed
# omitting the second tool call's id and reusing delta indices; these tests
# pin the gateway's countermeasures: one id allocated per call and reused
# on the wire AND in persistence, and an index reuse treated as a new call.


async def test_streaming_split_tool_calls_ids_persisted_match_streamed(
    client, router_setter, byok_env
):
    """Two tool calls streamed across separate chunks keep distinct ids,
    and the persisted assistant turn carries exactly the ids the client
    saw - the invariant DeepSeek validates on the next replay."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"tool_calls": [
                {"index": 0, "id": "call_read", "type": "function",
                 "function": {"name": "read_file",
                              "arguments": '{"pa'}},
            ]}),
            stream_chunk("alpha", {"tool_calls": [
                {"index": 1, "id": "call_list", "type": "function",
                 "function": {"name": "list_dir",
                              "arguments": '{"pa'}},
            ]}),
            stream_chunk("alpha", {"tool_calls": [
                {"index": 0, "function": {"arguments": 'th": "a"}'}},
                {"index": 1, "function": {"arguments": 'th": "."}'}},
            ]}),
            stream_chunk("alpha", {}, finish_reason="tool_calls"),
        ))
    })
    response = await client.post(
        "/v1/responses", headers={**auth, "X-Session-Id": "split-calls"},
        json={"model": "m", "input": "read both", "stream": True},
    )
    assert response.status_code == 200
    events = _responses_events(response)
    added = [p for name, p in events
             if name == "response.output_item.added"
             and p["item"]["type"] == "function_call"]
    streamed_ids = [p["item"]["call_id"] for p in added]
    assert streamed_ids == ["call_read", "call_list"]

    completed = [p for name, p in events
                 if name == "response.completed"][0]["response"]
    calls = [i for i in completed["output"] if i["type"] == "function_call"]
    assert [c["call_id"] for c in calls] == ["call_read", "call_list"]
    assert json.loads(calls[0]["arguments"]) == {"path": "a"}
    assert json.loads(calls[1]["arguments"]) == {"path": "."}

    assert "response.completed" in response.text
    history = await app.state.sessions.load(
        "split-calls", **await _user_kwargs(uid))
    assistant = [m for m in history if m["role"] == "assistant"][0]
    assert [c["id"] for c in assistant["tool_calls"]] == streamed_ids
    assert [c["function"]["name"] for c in assistant["tool_calls"]] == [
        "read_file", "list_dir"]


async def test_streaming_missing_second_tool_call_id_gets_one_allocated(
    client, router_setter, byok_env
):
    """NIM/vLLM omits the second call's id mid-stream: the gateway
    allocates exactly one id at state creation and uses that same string
    in the streamed item and the persisted turn - never two divergent
    randoms."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"tool_calls": [
                {"index": 0, "id": "call_read", "type": "function",
                 "function": {"name": "read_file",
                              "arguments": '{"path": "a"}'}},
            ]}),
            stream_chunk("alpha", {"tool_calls": [
                # Second call: NO id at all (the NIM failure mode).
                {"index": 1, "type": "function",
                 "function": {"name": "read_file",
                              "arguments": '{"path": "b"}'}},
            ]}),
            stream_chunk("alpha", {}, finish_reason="tool_calls"),
        ))
    })
    response = await client.post(
        "/v1/responses", headers={**auth, "X-Session-Id": "missing-id"},
        json={"model": "m", "input": "retry read", "stream": True},
    )
    assert response.status_code == 200
    events = _responses_events(response)
    added = [p for name, p in events
             if name == "response.output_item.added"
             and p["item"]["type"] == "function_call"]
    streamed_ids = [p["item"]["call_id"] for p in added]
    assert len(streamed_ids) == 2
    assert streamed_ids[0] == "call_read"
    # The missing id got exactly one allocation - present, unique, and
    # reused everywhere.
    assert streamed_ids[1]
    assert streamed_ids[1] != streamed_ids[0]

    completed = [p for name, p in events
                 if name == "response.completed"][0]["response"]
    calls = [i for i in completed["output"] if i["type"] == "function_call"]
    assert [c["call_id"] for c in calls] == streamed_ids

    assert "response.completed" in response.text
    history = await app.state.sessions.load(
        "missing-id", **await _user_kwargs(uid))
    assistant = [m for m in history if m["role"] == "assistant"][0]
    assert [c["id"] for c in assistant["tool_calls"]] == streamed_ids


async def test_streaming_reused_index_with_new_id_not_merged(
    client, router_setter, byok_env
):
    """A provider that reuses delta index 0 for a genuinely different
    call (new id) must get a second tool state - never a merge that
    drops one call and corrupts the other."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"tool_calls": [
                {"index": 0, "id": "call_first", "type": "function",
                 "function": {"name": "read_file",
                              "arguments": '{"path": "a.txt"}'}},
            ]}),
            stream_chunk("alpha", {"tool_calls": [
                # SAME index 0, DIFFERENT id: a new call.
                {"index": 0, "id": "call_second", "type": "function",
                 "function": {"name": "read_file",
                              "arguments": '{"path": "b.txt"}'}},
            ]}),
            stream_chunk("alpha", {}, finish_reason="tool_calls"),
        ))
    })
    response = await client.post(
        "/v1/responses", headers={**auth, "X-Session-Id": "index-reuse"},
        json={"model": "m", "input": "retry read", "stream": True},
    )
    assert response.status_code == 200
    events = _responses_events(response)
    added = [p for name, p in events
             if name == "response.output_item.added"
             and p["item"]["type"] == "function_call"]
    assert [p["item"]["call_id"] for p in added] == [
        "call_first", "call_second"]

    completed = [p for name, p in events
                 if name == "response.completed"][0]["response"]
    calls = [i for i in completed["output"] if i["type"] == "function_call"]
    assert [c["call_id"] for c in calls] == ["call_first", "call_second"]
    assert json.loads(calls[0]["arguments"]) == {"path": "a.txt"}
    assert json.loads(calls[1]["arguments"]) == {"path": "b.txt"}

    assert "response.completed" in response.text
    history = await app.state.sessions.load(
        "index-reuse", **await _user_kwargs(uid))
    assistant = [m for m in history if m["role"] == "assistant"][0]
    assert [c["id"] for c in assistant["tool_calls"]] == [
        "call_first", "call_second"]
    assert [c["function"]["arguments"] for c in assistant["tool_calls"]] == [
        '{"path": "a.txt"}', '{"path": "b.txt"}']


async def test_debug_stream_capture_on_writes_file_off_writes_nothing(
    client, router_setter, byok_env, monkeypatch
):
    """INVINCIBLE_DEBUG_STREAM (opt-in) captures the outgoing payload plus
    the raw upstream chunk sequence into debug_stream_<request_id>.json -
    the evidence file the tool-call pairing investigation lacked. Unset,
    nothing is ever written."""
    uid, raw_key = await v1_user(client, "responses@example.com")
    auth = {"Authorization": f"Bearer {raw_key}"}
    handlers = {
        "alpha.example.com": httpx.Response(200, content=sse_body(
            stream_chunk("alpha", {"role": "assistant"}),
            stream_chunk("alpha", {"content": "hi"}),
            stream_chunk("alpha", {}, finish_reason="stop"),
        ))
    }

    import pathlib

    from invincible.core.router import _debug_stream_path

    def _glob():
        return list(pathlib.Path().glob("debug_stream_*.json"))

    # OFF (default): a full streaming request leaves no capture behind.
    router_setter(handlers=handlers)
    await client.post(
        "/v1/responses", headers={**auth, "X-Session-Id": "dbg-off"},
        json={"model": "m", "input": "hi", "stream": True},
    )
    leftovers = _glob()
    for path in leftovers:
        path.unlink(missing_ok=True)
    assert not leftovers

    # ON: the capture is keyed by the gateway request id the response
    # headers already report, and carries provider + payload + chunks.
    monkeypatch.setenv("INVINCIBLE_DEBUG_STREAM", "1")
    router_setter(handlers=handlers)
    response = await client.post(
        "/v1/responses", headers={**auth, "X-Session-Id": "dbg-on"},
        json={"model": "m", "input": "hi", "stream": True},
    )
    assert response.status_code == 200
    request_id = response.headers["x-invincible-request-id"]
    captured = _debug_stream_path(request_id)
    assert captured.exists()
    try:
        data = json.loads(captured.read_text(encoding="utf-8"))
        assert data["request_id"] == request_id
        assert data["provider"] == "alpha"
        assert data["payload"]["model"] == "m"
        assert len(data["chunks"]) == 3
    finally:
        captured.unlink(missing_ok=True)


# ------------------------------------------------------------ responses fold


def test_responses_to_internal_repairs_out_of_order_function_outputs():
    """A function_call_output arriving before its function_call (the
    strict left-to-right fold alone cannot pair it) is buffered and the
    final messages still satisfy the pairing invariant."""
    internal = responses_to_internal([
        {"type": "function_call_output", "call_id": "call_a",
         "output": "early"},
        {"type": "function_call", "call_id": "call_a", "name": "f",
         "arguments": "{}"},
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "go"}]},
    ])
    roles = [m["role"] for m in internal]
    assert roles == ["assistant", "tool", "user"]
    assert internal[0]["tool_calls"][0]["id"] == "call_a"
    assert internal[1]["tool_call_id"] == "call_a"
    assert internal[1]["content"] == "early"


def test_responses_to_internal_unpairable_output_raises():
    """A tool output no call ever claims is unrecoverable: the fold must
    raise (naming the index) rather than forward a request upstream is
    guaranteed to reject."""
    with pytest.raises(ValueError, match="messages\\[0\\]"):
        responses_to_internal([
            {"type": "function_call_output", "call_id": "call_ghost",
             "output": "?"},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "go"}]},
        ])


# ----------------------------------------------------------------- SSE parse


async def test_iter_stream_buffers_json_split_across_data_lines():
    """A tool-call chunk split across consecutive ``data:`` lines is
    buffered and parsed once complete - not a malformed-SSE failover."""
    from invincible.core.router import _iter_stream

    raw = (
        b'data: {"id":"1","choices":[{"delta":{"tool_calls":[{"ind\n'
        b'data: ex":0,"id":"call_9","function":{"name":"f"}}]}}]}\n'
        b"\n"
        b"data: [DONE]\n\n"
    )
    chunks = [c async for c in _iter_stream(httpx.Response(200, content=raw))]
    assert chunks == [{
        "id": "1",
        "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_9", "function": {"name": "f"}}]}},
        ],
    }]


async def test_iter_stream_tolerates_done_variants():
    """``[DONE]`` variants (case/bracket differences) terminate the
    stream instead of being parsed as junk."""
    from invincible.core.router import _iter_stream

    raw = (
        b'data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}\n\n'
        b"data: done\n\n"
    )
    chunks = [c async for c in _iter_stream(httpx.Response(200, content=raw))]
    assert len(chunks) == 1
    assert chunks[0]["choices"][0]["delta"]["content"] == "hi"


async def test_iter_stream_malformed_first_chunk_raises_for_failover():
    """A genuinely malformed FIRST chunk keeps raising - that is the
    Router's signal that the provider is unusable and failover should
    pick the next credential."""
    from invincible.core.router import _iter_stream

    raw = b"data: {not json ever\n\n"
    with pytest.raises(json.JSONDecodeError):
        chunks = [
            c async for c in _iter_stream(httpx.Response(200, content=raw))
        ]
        # Force iteration so the parse actually runs.
        assert not chunks


async def test_iter_stream_skips_malformed_later_chunk():
    """A malformed chunk AFTER healthy events is logged and skipped; an
    otherwise healthy stream is not torn down."""
    from invincible.core.router import _iter_stream

    raw = (
        b'data: {"id":"1","choices":[{"delta":{"content":"a"}}]}\n\n'
        b"data: {oops garbage\n\n"
        b'data: {"id":"2","choices":[{"delta":{"content":"b"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    chunks = [c async for c in _iter_stream(httpx.Response(200, content=raw))]
    assert [c["id"] for c in chunks] == ["1", "2"]
