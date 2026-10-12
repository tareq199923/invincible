# tests/test_baymax_screenshot.py
"""B0.3 simulated pointing: screenshot results ride the tool_result SSE
event as a display-safe image; everything else stays text-only.

Provider upstreams are faked via httpx.MockTransport (same scripted
pattern as test_webchat_agent.py); the agent boundary is faked by
monkeypatching ``webchat_agent._run_screenshot`` so no Chrome or paired
machine is ever touched.
"""
import json

import httpx

from invincible.core import tool_executor, webchat_agent
from tests.conftest import provider_body
from tests.test_webchat_agent import agent_user

_FAKE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _shot(url="https://example.com", size=68):
    return {"status": "screenshot", "mime": "image/png",
            "data_b64": _FAKE_B64, "bytes": size, "url": url}


def test_screenshot_image_extracts_ok_shape():
    img = webchat_agent._screenshot_image(_shot())
    assert img == {"mime": "image/png", "data_b64": _FAKE_B64,
                   "bytes": 68, "url": "https://example.com"}


def test_screenshot_image_rejects_non_image_shapes():
    assert webchat_agent._screenshot_image(
        {"status": "error", "error": "nope"}) is None
    assert webchat_agent._screenshot_image(
        {"status": "unavailable", "reason": "no agent"}) is None
    assert webchat_agent._screenshot_image({}) is None
    assert webchat_agent._screenshot_image("screenshot") is None
    assert webchat_agent._screenshot_image(None) is None


def test_screenshot_image_rejects_wrong_mime_empty_oversize():
    base = _shot()
    assert webchat_agent._screenshot_image(
        {**base, "mime": "image/jpeg"}) is None
    assert webchat_agent._screenshot_image(
        {**base, "data_b64": ""}) is None
    assert webchat_agent._screenshot_image(
        {**base, "bytes": tool_executor.SCREENSHOT_MAX_BYTES + 1}) is None
    assert webchat_agent._screenshot_image(
        {**base, "bytes": 0}) is None


def _fn_call(call_id, name, args):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _tool_body(provider, calls):
    return {"id": "cmpl-tools", "model": f"{provider}-model",
            "choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": calls}}]}


def _scripted(responses):
    bodies = []
    state = {"i": 0}

    def handler(request):
        bodies.append(json.loads(request.content))
        resp = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return resp if isinstance(resp, httpx.Response) else \
            httpx.Response(200, json=resp)

    return bodies, handler


def _parse_events(text):
    events = []
    for part in text.split("\n\n"):
        part = part.strip()
        if not part:
            continue
        name, data = None, None
        for line in part.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        events.append((name, data))
    return events


async def test_screenshot_tool_result_carries_image(
    client, byok_env, router_setter, monkeypatch
):
    bodies, handler = _scripted([
        _tool_body("w1", [_fn_call("c1", "screenshot",
                                   {"url": "https://example.com"})]),
        provider_body("w1", content="saw it"),
    ])
    router_setter({"w1.example.com": handler})

    async def fake_shot(executor, args):
        assert args == {"url": "https://example.com"}
        return _shot()

    monkeypatch.setattr(webchat_agent, "_run_screenshot", fake_shot)
    await agent_user(client, "shots@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-shot", "message": "look at example",
        "mode": "auto"})
    assert resp.status_code == 200, resp.text
    results = [d for n, d in _parse_events(resp.text)
               if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is True
    assert "example.com" in results[0]["preview"]
    assert "data_b64" not in results[0]["preview"]
    img = results[0]["image"]
    assert img["mime"] == "image/png"
    assert img["data_b64"] == _FAKE_B64
    assert img["url"] == "https://example.com"


async def test_non_screenshot_tool_result_has_no_image(
    client, byok_env, router_setter, monkeypatch
):
    bodies, handler = _scripted([
        _tool_body("w1", [_fn_call("c1", "read_file", {"path": "a.txt"})]),
        provider_body("w1", content="read it"),
    ])
    router_setter({"w1.example.com": handler})

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "hello"}

    monkeypatch.setattr(tool_executor, "read_file", fake_read)
    await agent_user(client, "noimg@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-noimg", "message": "read a", "mode": "auto"})
    assert resp.status_code == 200, resp.text
    results = [d for n, d in _parse_events(resp.text)
               if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is True
    assert "image" not in results[0]


def test_chat_template_renders_shot_preview():
    from pathlib import Path

    html = Path(__file__).resolve().parent.parent.joinpath(
        "invincible", "templates", "chat.html").read_text(encoding="utf-8")
    assert "shot-preview" in html
    assert 'payload.image.mime === "image/png"' in html
    assert '"data:image/png;base64,"' in html
    css = Path(__file__).resolve().parent.parent.joinpath(
        "invincible", "templates", "static", "app.css").read_text(
        encoding="utf-8")
    assert ".shot-preview" in css
