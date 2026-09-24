# tests/test_usage_accounting.py
"""Token accounting on the response paths (deep code review 2026-09-24,
finding 5).

Two separate defects lived here, and both made the numbers WRONG rather
than merely approximate:

1. A tool-only turn was measured from its text alone. Coding agents send
   turns that are nothing BUT tool calls, so those reported the
   empty-message floor - single digits - while the arguments actually
   produced could run to thousands of tokens.
2. The streamed chat endpoint measured the completed turn list - the
   caller's own input messages PLUS the reply - and recorded the total
   as output.

The heuristic itself was never at fault: ``estimate_tokens`` serializes
the whole message, tool calls included. Every call site simply handed it
an incomplete message. See ``compat/common.estimate_assistant_tokens``.
"""
import json

import httpx

from invincible.compat.anthropic import internal_to_anthropic
from invincible.compat.common import estimate_assistant_tokens
from invincible.compat.responses import internal_to_responses
from invincible.main import app
from tests.conftest import provider_body, sse_body, stream_chunk, v1_user

TOOL_CALLS = [{
    "id": "call_1",
    "type": "function",
    "function": {
        "name": "read_file",
        "arguments": json.dumps({"path": "/" + "x" * 400}),
    },
}]


def _provider_body(content=None, tool_calls=None) -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "model": "served-model",
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
    }


# --- the measurement itself -------------------------------------------------


def test_estimate_assistant_tokens_counts_tool_calls():
    """The regression, at its smallest.

    A tool-only turn has no text at all, so measuring the text alone
    returns the empty-message floor no matter how large the tool call is.
    The estimate is asserted RELATIVE to that floor rather than against a
    fixed number: ``estimate_tokens`` serializes the whole message, so the
    floor is the serialized envelope's length, not a constant worth
    pinning in a test.
    """
    without_calls = estimate_assistant_tokens("", None)
    with_calls = estimate_assistant_tokens("", TOOL_CALLS)
    assert with_calls > 100
    assert with_calls > without_calls * 10


# --- the two wire protocols -------------------------------------------------


def test_anthropic_usage_counts_tool_calls():
    body = internal_to_anthropic(
        _provider_body(tool_calls=TOOL_CALLS), "served-model", 10)
    assert body["usage"]["output_tokens"] > 100
    assert body["usage"]["input_tokens"] == 10


def test_responses_usage_counts_tool_calls():
    body = internal_to_responses(
        _provider_body(tool_calls=TOOL_CALLS), "served-model", 10)
    assert body["usage"]["output_tokens"] > 100
    assert body["usage"]["input_tokens"] == 10


def test_text_only_turns_still_count_their_text():
    """The fix must not become 'always big': a plain reply still measures
    its own content and nothing else."""
    body = internal_to_anthropic(
        _provider_body(content="hello"), "served-model", 10)
    assert body["usage"]["output_tokens"] < 20


# --- the recorded run row ---------------------------------------------------


async def test_streamed_chat_records_only_the_reply_as_output(
    client, router_setter, byok_env
):
    """The streamed endpoint recorded ``input + reply`` as output.

    A long question and a two-character reply make the two behaviours far
    apart: the bug reported roughly a hundred tokens, the fix reports one
    or two. The recorder is wired the way ``main.py``'s lifespan wires it -
    without that no run row is written at all, and the assertion would pass
    against an empty table (the trap that concealed finding 1).
    """
    _, raw_key = await v1_user(client, "usage@example.com")
    router = router_setter(handlers={
        "alpha.example.com": httpx.Response(
            200,
            content=sse_body(
                stream_chunk("alpha", {"role": "assistant", "content": "ok"}),
                stream_chunk("alpha", {}, finish_reason="stop"),
            ),
        )
    })
    router.run_recorder = app.state.runs.record

    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "messages": [{"role": "user", "content": "x" * 400}],
            "stream": True,
        },
    )
    assert response.status_code == 200

    rows = await app.state.runs.recent(limit=10)
    succeeded = [r for r in rows if r["outcome"] == "ok"]
    assert succeeded, "no successful run recorded"
    output = succeeded[0]["output_tokens"] or 0
    assert output >= 1
    assert output < 20, f"the input was counted as output: {output}"


async def test_nonstreaming_usage_is_unaffected(client, router_setter, byok_env):
    """A guard on the neighbouring path: real upstream usage still wins
    over the estimate, exactly as before."""
    _, raw_key = await v1_user(client, "usage-ns@example.com")
    body = provider_body("alpha")
    body["usage"] = {"prompt_tokens": 11, "completion_tokens": 22}
    router_setter(handlers={
        "alpha.example.com": httpx.Response(200, json=body)})

    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert response.json()["usage"]["completion_tokens"] == 22
