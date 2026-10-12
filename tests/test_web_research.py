# tests/test_web_research.py
"""F08 web_research: server-side Responses web_search through the
caller's own provider. HTTP is faked via httpx.MockTransport (passed
as ``transport``); no network, no credentials, no paired machine."""

import httpx

from invincible.core import web_research


def _responses_payload(text="Title Words Here\n\nFirst para.\n\nSecond para.",
                       annotations=None):
    return {"output": [{"type": "message",
                        "content": [{"text": text,
                                     "annotations": annotations or []}]}]}


def test_clean_report_text_strips_citations_and_markup():
    blocks = web_research.clean_report_text(
        "Hello **World**\n\n"
        "See ([x](https://a.example)) [y](https://b.example)\n\n"
        "# Head\n\n [https://c.example]")
    assert blocks == ["Hello World", "See y", "Head"]


def test_parse_report_shapes_contract_and_caps_sources():
    notes = [{"title": f"S{i}", "url": f"https://s{i}.example"}
             for i in range(7)]
    out = web_research.parse_report(
        "q?", _responses_payload(annotations=notes))
    assert out["status"] == "research"
    assert out["question"] == "q?"
    assert out["title"] == "Title Words Here"
    assert out["paragraphs"] == ["First para.", "Second para."]
    assert len(out["sources"]) == 5
    assert out["sources"][0] == {"title": "S0",
                                 "url": "https://s0.example"}


def test_parse_report_skips_non_messages_and_dedupes():
    payload = {"output": [
        {"type": "reasoning", "text": "nope"},
        {"type": "message", "content": [
            {"text": "T\n\nBody.",
             "annotations": [{"title": "A", "url": "https://a.example"},
                             {"title": "A2", "url": "https://a.example"}]}]},
    ]}
    out = web_research.parse_report("q?", payload)
    assert out["sources"] == [{"title": "A", "url": "https://a.example"}]


def test_parse_report_empty_is_error():
    assert web_research.parse_report(
        "q?", {"output": []})["status"] == "error"


async def _run(question="q?", payload=None, status=200, transport=None,
               context=""):
    if transport is None:
        def handler(request):
            assert request.url.path == "/v1/responses"
            assert request.headers["authorization"] == "Bearer k"
            return httpx.Response(status, json=payload or {})
        transport = httpx.MockTransport(handler)
    return await web_research.research(
        question, context, base_url="https://api.test/v1", api_key="k",
        model="m", transport=transport)


async def test_research_success_end_to_end_shape():
    out = await _run(
        payload=_responses_payload(
            annotations=[{"title": "S", "url": "https://s.example"}]))
    assert out["status"] == "research"
    assert out["title"] == "Title Words Here"


async def test_research_non_200_is_unavailable():
    out = await _run(payload={"error": "nope"}, status=404)
    assert out["status"] == "unavailable"
    assert "404" in out["reason"]


async def test_research_transport_error_is_error():
    def handler(request):
        raise httpx.ConnectError("down")
    out = await web_research.research(
        "q?", "", base_url="https://api.test/v1", api_key="k", model="m",
        transport=httpx.MockTransport(handler))
    assert out["status"] == "error"


async def test_research_empty_question_never_calls_http():
    called = []

    def handler(request):
        called.append(request)
        return httpx.Response(200, json={})

    out = await web_research.research(
        "  ", "", base_url="https://api.test/v1", api_key="k", model="m",
        transport=httpx.MockTransport(handler))
    assert out["status"] == "error"
    assert called == []


async def test_run_research_unavailable_without_engine():
    out = await web_research.run_research(None, None, {"question": "q?"})
    assert out["status"] == "unavailable"
    assert "providers" in out["reason"]


async def test_run_research_empty_question_is_error():
    out = await web_research.run_research(None, None, {"question": "  "})
    assert out["status"] == "error"


async def test_webchat_streams_research_result(
    client, byok_env, router_setter, monkeypatch
):
    import json as _json

    from tests.test_webchat_agent import agent_user

    def fn_call(call_id, name, args):
        return {"id": call_id, "type": "function",
                "function": {"name": name,
                             "arguments": _json.dumps(args)}}

    bodies = []
    state = {"i": 0}
    script = [
        {"id": "cmpl-tools", "model": "w1-model",
         "choices": [{"message": {"role": "assistant", "content": None,
                                  "tool_calls": [
                                      fn_call("c1", "web_research",
                                              {"question": "q?"})]}}]},
        {"id": "cmpl-text", "model": "w1-model",
         "choices": [{"message": {"role": "assistant",
                                  "content": "done"}}]},
    ]

    def handler(request):
        bodies.append(_json.loads(request.content))
        resp = script[min(state["i"], len(script) - 1)]
        state["i"] += 1
        return httpx.Response(200, json=resp)

    router_setter({"w1.example.com": handler})

    async def fake_research(engine, user_id, args):
        assert args["question"] == "q?"
        return {"status": "research", "question": "q?",
                "title": "T", "paragraphs": ["P"], "sources": []}

    monkeypatch.setattr(web_research, "run_research", fake_research)
    await agent_user(client, "research@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-research", "message": "research q",
        "mode": "auto"})
    assert resp.status_code == 200, resp.text
    events = []
    for part in resp.text.split("\n\n"):
        part = part.strip()
        if part:
            name, data = None, None
            for line in part.split("\n"):
                if line.startswith("event:"):
                    name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data = _json.loads(line[len("data:"):].strip())
            events.append((name, data))
    results = [d for n, d in events if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is True
    assert results[0]["name"] == "web_research"
    assert "T" in results[0]["preview"]
    offered = [t["function"]["name"]
               for body in bodies for t in body.get("tools") or []]
    assert "web_research" in offered
