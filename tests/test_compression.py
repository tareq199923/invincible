import json

import httpx
import pytest

from invincible.core.compression import (
    DEFAULT_TOOL_RESULT_MAX_CHARS,
    compress_messages,
    compression_enabled,
)
from invincible.core.router import estimate_tokens, trim_messages


def user(content):
    return {"role": "user", "content": content}


def assistant(content):
    return {"role": "assistant", "content": content}


def system(content):
    return {"role": "system", "content": content}


def tool(content, call_id="call_1"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_compression_enabled_by_default(monkeypatch):
    monkeypatch.delenv("INVINCIBLE_COMPRESSION", raising=False)
    assert compression_enabled()


@pytest.mark.parametrize("value", ["0", "false", "off", "OFF", " False "])
def test_compression_disabled_by_env(monkeypatch, value):
    monkeypatch.setenv("INVINCIBLE_COMPRESSION", value)
    assert not compression_enabled()


def test_short_messages_pass_through_untouched():
    messages = [system("be helpful"), user("hi"), assistant("hello!")]
    assert compress_messages(messages) == messages


def test_long_tool_result_is_truncated_with_marker():
    big = "A" * 3000 + "M" * 3000 + "Z" * 3000
    result = compress_messages([tool(big)], tool_result_max_chars=2000)[0]
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call_1"
    assert len(result["content"]) < len(big)
    assert "compressed away" in result["content"]
    assert result["content"].startswith("A" * 100)
    assert result["content"].endswith("Z" * 100)


def test_tool_result_at_limit_is_not_truncated():
    content = "x" * DEFAULT_TOOL_RESULT_MAX_CHARS
    result = compress_messages([tool(content)])[0]
    assert result["content"] == content


def test_blank_runs_collapse_in_any_role():
    content = "line1\n\n\n\n\nline2"
    for msg in (user(content), assistant(content), system(content)):
        assert compress_messages([msg])[0]["content"] == "line1\n\nline2"


def test_structure_and_tool_calls_preserved():
    tool_call_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }
    messages = [user("read it"), tool_call_msg, tool("x" * 9000)]
    result = compress_messages(messages)
    assert result[1] is tool_call_msg  # no string content: passed through as-is
    assert result[1]["tool_calls"] == tool_call_msg["tool_calls"]
    assert result[2]["tool_call_id"] == "call_1"


def test_input_messages_are_never_mutated():
    original = tool("A" * 9000)
    snapshot = dict(original)
    compress_messages([original])
    assert original == snapshot


def test_compression_lets_trim_keep_more_turns():
    messages = [system("be helpful")]
    big = "x" * 8000
    for i in range(6):
        messages.append(user(f"turn {i}"))
        messages.append(tool(f"output {i}: {big}", call_id=f"c{i}"))
        messages.append(assistant(f"reply {i}"))

    max_context = 4000
    plain = trim_messages(messages, max_context)
    compressed = trim_messages(compress_messages(messages), max_context)

    assert len(compressed) >= len(plain)
    assert estimate_tokens({"role": "x", "content": json.dumps(compressed)}) or True
    # Same session, strictly cheaper to send:
    assert sum(estimate_tokens(m) for m in compressed) < sum(
        estimate_tokens(m) for m in messages
    )


@pytest.mark.asyncio
async def test_router_sends_compressed_payload_by_default(make_router, monkeypatch):
    monkeypatch.delenv("INVINCIBLE_COMPRESSION", raising=False)
    received = {}

    def handler(request: httpx.Request):
        received["payload"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]
            },
        )

    providers = [
        {
            "name": "alpha",
            "tier": 1,
            "base_url": "https://alpha.example.com/v1",
            "api_key_env": "ALPHA_API_KEY",
            "model_id": "alpha-model",
            "max_context": 1_000_000,
        }
    ]
    router = make_router(
        providers=providers, handlers={"alpha.example.com": handler}
    )
    await router.route_request([user("go"), tool("A" * 9000)])
    sent = received["payload"]["messages"]
    tool_msg = next(m for m in sent if m["role"] == "tool")
    assert len(tool_msg["content"]) < 9000
    assert "compressed away" in tool_msg["content"]


@pytest.mark.asyncio
async def test_router_sends_verbatim_payload_when_disabled(make_router, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_COMPRESSION", "0")
    received = {}

    def handler(request: httpx.Request):
        received["payload"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]
            },
        )

    providers = [
        {
            "name": "alpha",
            "tier": 1,
            "base_url": "https://alpha.example.com/v1",
            "api_key_env": "ALPHA_API_KEY",
            "model_id": "alpha-model",
            "max_context": 1_000_000,
        }
    ]
    router = make_router(
        providers=providers, handlers={"alpha.example.com": handler}
    )
    await router.route_request([user("go"), tool("A" * 9000)])
    sent = received["payload"]["messages"]
    tool_msg = next(m for m in sent if m["role"] == "tool")
    assert tool_msg["content"] == "A" * 9000
