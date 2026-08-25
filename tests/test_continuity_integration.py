# tests/test_continuity_integration.py
"""Continuity brief injection across both chat protocols (Phase 15b B2),
including the failover-interruption e2e and never-persist guarantee.

The conftest `client` fixture builds app.state directly (no lifespan), so
these tests attach ContinuityEngine/RunStore exactly the way main.py's
lifespan does - on the same engine as app.state.sessions.
"""
import json

import httpx

from invincible.core.continuity import ContinuityEngine
from invincible.core.run_store import RunStore
from tests.conftest import provider_body, sse_body, stream_chunk

GATEWAY = {"Authorization": "Bearer test-gateway-key"}
MARKER = "Session continuity"


async def attach_continuity(with_runs=False):
    shared_engine = app_state_sessions().engine
    engine = ContinuityEngine(engine=shared_engine, runs=None)
    if with_runs:
        runs = RunStore(engine=shared_engine)
        engine._runs = runs
        app_state_runs(runs)
    await engine.init()
    from invincible.main import app

    app.state.continuity = engine
    return engine


def app_state_sessions():
    from invincible.main import app

    return app.state.sessions


def app_state_runs(runs):
    from invincible.main import app

    app.state.runs = runs


async def seed_task(engine, session_id="default", payload=None):
    # Phase 2: seed through ownership like MCP does - resolve-or-create
    # the local-owner session row so scoped reads (session_pk) find it.
    from invincible.core.db import ensure_local_owner

    sessions = app_state_sessions()
    uid, pid = await ensure_local_owner(engine.engine)
    session_pk = await sessions.resolve_or_create(
        session_id, user_id=uid, project_id=pid,
    )
    await engine.set_state(
        session_id,
        payload or {"task": "count 1-100", "next_value": 6},
        actor="mcp:task_state_set",
        session_pk=session_pk,
    )
    await engine.create_checkpoint(
        session_id, note="through 5", actor="mcp:checkpoint_create",
        session_pk=session_pk,
    )
    return session_pk


def capture_handler(captured, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return httpx.Response(status, json=provider_body("beta"))
    return handler


async def chat(client, payload_overrides=None, headers=GATEWAY):
    body = {"messages": [{"role": "user", "content": "continue"}]}
    if payload_overrides:
        body.update(payload_overrides)
    return await client.post("/v1/chat/completions", headers=headers,
                             json=body)


async def test_openai_injects_continuation_brief_upstream(client, router_setter):
    engine = await attach_continuity()
    await seed_task(engine)
    captured = []
    router_setter({"beta.example.com": capture_handler(captured)})
    resp = await chat(client)
    assert resp.status_code == 200
    system_texts = [
        m.get("content", "") for m in captured[0]["messages"]
        if m["role"] == "system"
    ]
    assert any(MARKER in t for t in system_texts)
    assert any('"next_value": 6' in t for t in system_texts)


async def test_injected_brief_is_never_persisted(client, router_setter):
    engine = await attach_continuity()
    await seed_task(engine)
    router_setter({"alpha.example.com": lambda r: httpx.Response(
        200, json=provider_body("alpha"))})
    await chat(client)
    history = await app_state_sessions().load("default")
    assert history, "assistant reply should persist"
    assert all(MARKER not in (m.get("content") or "") for m in history)


async def test_toggle_off_removes_injection(client, router_setter, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_CONTINUITY", "0")
    engine = await attach_continuity()
    await seed_task(engine)
    captured = []
    router_setter({"alpha.example.com": capture_handler(captured)})
    await chat(client)
    assert all(
        MARKER not in (m.get("content") or "")
        for m in captured[0]["messages"]
    )


async def test_anthropic_path_injects_continuation_brief(client, router_setter):
    engine = await attach_continuity()
    await seed_task(engine)
    captured = []

    def ok(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant",
                                           "content": "ok"},
                               "finish_reason": "stop"}]},
        )

    router_setter({"alpha.example.com": ok})
    resp = await client.post(
        "/v1/messages",
        headers=GATEWAY,
        json={"max_tokens": 10,
              "messages": [{"role": "user", "content": "continue"}]},
    )
    assert resp.status_code == 200
    system_texts = [
        m.get("content", "") for m in captured[0]["messages"]
        if m["role"] == "system"
    ]
    assert any('"next_value": 6' in t for t in system_texts)


async def test_interrupted_signal_reaches_next_request_after_failover(
    client, router_setter
):
    """The counting promise, e2e-lite: state says next=6; ALL providers
    die; the NEXT request's outgoing context carries the trusted progress
    plus the interruption note naming the newest failed attempt. After a
    successful attempt, the note clears for subsequent renders."""
    engine = await attach_continuity(with_runs=True)
    await seed_task(engine)

    # Wire runs recording into whichever Router the fixture builds.
    router_setter({
        "alpha.example.com": lambda r: httpx.Response(500, json={}),
        "beta.example.com": lambda r: httpx.Response(500, json={}),
        "gamma.example.com": lambda r: httpx.Response(500, json={}),
    })
    from invincible.main import app

    router = app.state.router
    router.run_recorder = app.state.runs.record

    # Phase 1: everything fails -> 503, but attempts are recorded.
    dead = await chat(client)
    assert dead.status_code == 503
    recorded = await app.state.runs.recent(session_id="default")
    assert recorded, "failover attempts must be recorded for this session"
    assert all(r["outcome"] != "ok" for r in recorded)
    assert engine._runs is app.state.runs

    # Newest post-checkpoint failure is gamma (tier order alpha->beta->gamma).
    note = await engine.interruption_note("default")
    assert note and "'gamma'" in note

    # Phase 2: beta healthy (alpha/gamma still down).
    captured = []
    router_setter.routers.clear()
    router_setter({
        "beta.example.com": capture_handler(captured),
    })
    router = router_setter.routers[-1]
    router.run_recorder = app.state.runs.record

    resp = await chat(client)
    assert resp.status_code == 200
    system_texts = [
        m.get("content", "") for m in captured[0]["messages"]
        if m["role"] == "system"
    ]
    brief = next((t for t in system_texts if MARKER in t), "")
    assert '"next_value": 6' in brief
    assert "ended unexpectedly on provider 'gamma'" in brief

    # A successful attempt clears the interruption signal going forward.
    cleared = await engine.context_message("default")
    assert "ended unexpectedly" not in cleared["content"]


async def test_streaming_path_injects_brief_too(client, router_setter):
    engine = await attach_continuity()
    await seed_task(engine)
    captured = []

    def stream_ok(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return httpx.Response(200, text=sse_body(
            stream_chunk("beta", {"content": "hi"}, None),
            stream_chunk("beta", {}, "stop"),
        ))

    router_setter({"alpha.example.com": stream_ok})
    req = client.build_request(
        "POST", "/v1/chat/completions", headers=GATEWAY,
        json={"messages": [{"role": "user", "content": "go"}],
              "stream": True},
    )
    resp = await client.send(req, stream=True)
    assert resp.status_code == 200
    await resp.aclose()
    assert any(
        MARKER in (m.get("content") or "")
        for m in captured[0]["messages"] if m["role"] == "system"
    )


# ------------------------------------------------- B3: MCP continuity tools

def rpc_call(name, arguments, rpc_id=1):
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def tool_text(resp):
    data = resp.json()
    result = data["result"]
    return result.get("isError", False), result["content"][0]["text"]


async def test_mcp_set_then_llm_sees_state_e2e(
    client, router_setter, bearer_headers
):
    """The strict requirement, end to end: an MCP tool call writes canonical
    state; the very next LLM request - via the OpenAI path - receives it."""
    await attach_continuity()

    resp = await client.post("/mcp", headers=bearer_headers, json=rpc_call(
        "task_state_set",
        {
            "payload": json.dumps({
                "task": "count 1-100",
                "completed_through": 5,
                "next_value": 6,
            }),
            "session_id": "default",
        },
    ))
    assert resp.status_code == 200
    is_err, text = tool_text(resp)
    assert not is_err and json.loads(text)["version"] == 1

    captured = []
    router_setter({"alpha.example.com": capture_handler(captured)})
    llm_resp = await chat(client)
    assert llm_resp.status_code == 200
    system_texts = [
        m.get("content", "") for m in captured[0]["messages"]
        if m["role"] == "system"
    ]
    assert any('"next_value": 6' in t and MARKER in t for t in system_texts)


async def test_mcp_get_roundtrip_and_default_session(client, bearer_headers):
    await attach_continuity()
    # No session_id -> lands in the dedicated "mcp" session namespace.
    set_resp = await client.post("/mcp", headers=bearer_headers, json=rpc_call(
        "task_state_set",
        {"payload": json.dumps({"step": 2}), "task_key": "build"},
    ))
    _, set_text = tool_text(set_resp)
    assert json.loads(set_text)["version"] == 1

    get_resp = await client.post("/mcp", headers=bearer_headers, json=rpc_call(
        "task_state_get", {"task_key": "build"}
    ))
    _, get_text = tool_text(get_resp)
    state = json.loads(get_text)
    assert state["payload"] == {"step": 2} and state["version"] == 1

    miss = await client.post("/mcp", headers=bearer_headers, json=rpc_call(
        "task_state_get", {"task_key": "nope"}
    ))
    _, miss_text = tool_text(miss)
    assert json.loads(miss_text)["payload"] is None


async def test_mcp_cas_conflict_is_tool_error_not_rpc_error(
    client, bearer_headers
):
    await attach_continuity()
    first = await client.post("/mcp", headers=bearer_headers, json=rpc_call(
        "task_state_set",
        {"payload": json.dumps({"v": 1}), "expected_version": 0},
    ))
    assert first.status_code == 200
    _, _ = tool_text(first)

    stale = await client.post("/mcp", headers=bearer_headers, json=rpc_call(
        "task_state_set",
        {"payload": json.dumps({"v": 2}), "expected_version": 0},
    ))
    assert stale.status_code == 200  # JSON-RPC layer fine...
    is_err, text = tool_text(stale)
    assert is_err and "current head" in text  # ...tool-level conflict report


async def test_mcp_checkpoint_visible_in_llm_brief(
    client, router_setter, bearer_headers
):
    await attach_continuity()
    await client.post("/mcp", headers=bearer_headers, json=rpc_call(
        "task_state_set",
        {"payload": json.dumps({"completed_through": 37}),
         "session_id": "default"},
    ))
    cp = await client.post("/mcp", headers=bearer_headers, json=rpc_call(
        "checkpoint_create",
        {"note": "completed through 37", "session_id": "default"},
    ))
    is_err, cp_text = tool_text(cp)
    assert not is_err and json.loads(cp_text)["state_version"] == 1

    captured = []
    router_setter({"alpha.example.com": capture_handler(captured)})
    await chat(client)
    system_texts = [
        m.get("content", "") for m in captured[0]["messages"]
        if m["role"] == "system"
    ]
    brief = next((t for t in system_texts if MARKER in t), "")
    assert '"completed_through": 37' in brief
    assert "Latest checkpoint #" in brief and "through 37" in brief


async def test_mcp_tools_listed_additively(client, bearer_headers):
    listing = await client.post("/mcp", headers=bearer_headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })
    names = {t["name"] for t in listing.json()["result"]["tools"]}
    assert {"read_file", "execute_bash", "write_file", "confirm_action"} <= names
    assert {"task_state_set", "task_state_get", "checkpoint_create"} <= names
