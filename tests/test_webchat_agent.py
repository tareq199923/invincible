# tests/test_webchat_agent.py
"""Agentic webchat: plan / manual / auto modes over the paired-PC tool
path (cookie realm only).

Provider upstreams are faked via httpx.MockTransport (scripted
tool-call-then-text bodies); the OS boundary is faked by monkeypatching
``tool_executor._run_command`` / ``read_file`` so no real subprocess or
filesystem is ever touched. Agent-offline behavior is covered with the
routing toggle on and no agent heartbeat.
"""
import asyncio
import json

import httpx
import pytest

from invincible.core.accounts import SESSION_COOKIE
from invincible.core.credential_store import ByokCredentialStore
from invincible.core.identity import ApiKeyStore
from invincible.core.webchat_agent import ApprovalWaiter
from invincible.main import app
from tests.conftest import provider_body, register_account


async def agent_user(client, email, credential_count=1):
    registered, _ = await register_account(client, email)
    assert registered.status_code == 201, registered.text
    body = registered.json()
    store = ByokCredentialStore(app.state.engine)
    for i in range(credential_count):
        await store.create(
            user_id=body["id"],
            provider_name=f"Web{i + 1}",
            model_id=f"w{i + 1}-model",
            base_url=f"https://w{i + 1}.example.com/v1",
            api_key=f"web-key-{i + 1}",
        )
    return body["id"], body["project_id"]


def fn_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def tool_body(provider, calls):
    return {
        "id": "cmpl-tools",
        "model": f"{provider}-model",
        "choices": [{
            "message": {"role": "assistant", "content": None,
                        "tool_calls": calls},
        }],
    }


def scripted(responses):
    """Stateful MockTransport handler: serves each response in order
    (last repeats), recording parsed request bodies."""
    bodies = []
    state = {"i": 0}

    def handler(request):
        bodies.append(json.loads(request.content))
        resp = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return resp if isinstance(resp, httpx.Response) else \
            httpx.Response(200, json=resp)

    return bodies, handler


def offered_tool_names(bodies):
    names = []
    for body in bodies:
        for tool in body.get("tools") or []:
            names.append(tool["function"]["name"])
    return names


def parse_web_events(text):
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


async def test_mode_must_be_known(client, byok_env):
    await agent_user(client, "modes@example.com", credential_count=0)
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-m", "message": "hi", "mode": "turbo"})
    assert resp.status_code == 400, resp.text
    assert "mode must be" in resp.json()["error"]["message"]


async def test_plan_mode_offers_read_only_tools(
    client, byok_env, router_setter, monkeypatch
):
    bodies, handler = scripted([
        tool_body("w1", [fn_call("c1", "read_file", {"path": "a.txt"})]),
        provider_body("w1", content="plan: do X then Y"),
    ])
    router_setter({"w1.example.com": handler})
    reads = []

    async def fake_read(path):
        reads.append(path)
        return {"status": "read", "path": path, "content": "hello"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    uid, pid = await agent_user(client, "plan@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-plan", "message": "make a plan",
        "mode": "plan"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    assert "execute_bash" not in offered_tool_names(bodies)
    assert "write_file" not in offered_tool_names(bodies)
    assert "read_file" in offered_tool_names(bodies)
    assert reads == ["a.txt"]
    done = [d for n, d in events if n == "done"]
    assert len(done) == 1
    assert done[0]["mode"] == "plan"
    assert done[0]["tools_used"] == 1
    assert done[0]["execution"] == "local"
    assert not [d for n, d in events if n == "approval"]
    history = await app.state.sessions.load(
        "web-plan", user_id=uid, project_id=pid)
    assert [m.get("role") for m in history] == [
        "user", "assistant", "tool", "assistant"]


async def test_auto_mode_executes_without_approval(
    client, byok_env, router_setter, monkeypatch
):
    bodies, handler = scripted([
        tool_body("w1", [fn_call("c1", "execute_bash",
                                 {"command": "echo hi"})]),
        provider_body("w1", content="ran it"),
    ])
    router_setter({"w1.example.com": handler})
    ran = []

    async def fake_run(command, timeout=30.0):
        ran.append(command)
        return {"status": "ok", "output": "hi"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "_run_command", fake_run)
    uid, pid = await agent_user(client, "auto@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-auto", "message": "run echo", "mode": "auto"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    assert ran == ["echo hi"]
    assert not [d for n, d in events if n == "approval"]
    results = [d for n, d in events if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is True
    done = [d for n, d in events if n == "done"]
    assert done[0]["mode"] == "auto"
    history = await app.state.sessions.load(
        "web-auto", user_id=uid, project_id=pid)
    assert history[1]["tool_calls"][0]["function"]["name"] == "execute_bash"
    assert history[2] == {
        "role": "tool", "tool_call_id": "c1",
        "content": json.dumps({"status": "ok", "output": "hi"}),
    }


async def test_denylist_blocks_before_staging(
    client, byok_env, router_setter, monkeypatch
):
    bodies, handler = scripted([
        tool_body("w1", [fn_call("c1", "execute_bash",
                                 {"command": "rm -rf /"})]),
        provider_body("w1", content="cannot do that"),
    ])
    router_setter({"w1.example.com": handler})

    async def exploding_run(command, timeout=30.0):
        raise AssertionError("blocked command must never execute")

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "_run_command", exploding_run)
    await agent_user(client, "deny@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-deny", "message": "wipe it", "mode": "auto"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    results = [d for n, d in events if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is False
    assert "Blocked" in results[0]["preview"]
    assert not [d for n, d in events if n == "approval"]


async def test_agent_offline_is_a_tool_error(
    client, byok_env, router_setter, monkeypatch
):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    bodies, handler = scripted([
        tool_body("w1", [fn_call("c1", "read_file", {"path": "a.txt"})]),
        provider_body("w1", content="agent is away"),
    ])
    router_setter({"w1.example.com": handler})
    await agent_user(client, "offline@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-off", "message": "read it", "mode": "auto"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    results = [d for n, d in events if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is False
    assert "harness connect" in results[0]["preview"]
    done = [d for n, d in events if n == "done"]
    assert done[0]["execution"] == "agent"


async def _collect_with_approval(client, body, uid, approve):
    """Drive a manual-mode turn end to end: the stream POST runs while a
    background task watches the waiter map and answers the approval.
    (The test client only delivers SSE bodies on completion, so the
    approval is driven off the waiter, not off parsed stream chunks -
    the event assertions still run against the delivered body.)
    Returns (events, approve_response)."""
    waiter: ApprovalWaiter = app.state.webchat_approvals
    outcome: dict = {}

    async def approver():
        for _ in range(400):
            await asyncio.sleep(0.05)
            live = waiter.pending_tokens(uid)
            if live:
                outcome["resp"] = await client.post(
                    "/dashboard/chat/approve",
                    json={"token": live[0], "approve": approve},
                )
                outcome["token"] = live[0]
                return

    async with asyncio.timeout(60):
        task = asyncio.create_task(approver())
        resp = await client.post("/dashboard/chat/stream", json=body)
        await task
    assert resp.status_code == 200, resp.text
    return parse_web_events(resp.text), outcome.get("resp")


async def test_manual_approve_flow(
    client, byok_env, router_setter, monkeypatch
):
    bodies, handler = scripted([
        tool_body("w1", [fn_call("c1", "execute_bash",
                                 {"command": "echo approved"})]),
        provider_body("w1", content="approved and ran"),
    ])
    router_setter({"w1.example.com": handler})
    ran = []

    async def fake_run(command, timeout=30.0):
        ran.append(command)
        return {"status": "ok", "output": "approved"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "_run_command", fake_run)
    uid, pid = await agent_user(client, "manual@example.com")
    events, approved_resp = await _collect_with_approval(
        client,
        {"session_id": "web-man", "message": "run it", "mode": "manual"},
        uid, True,
    )
    assert approved_resp is not None
    assert approved_resp.status_code == 200, approved_resp.text
    assert approved_resp.json() == {"ok": True, "approved": True}
    assert ran == ["echo approved"]
    approvals = [d for n, d in events if n == "approval"]
    assert len(approvals) == 1
    assert approvals[0]["action"] == "execute_bash"
    assert "echo approved" in approvals[0]["detail"]
    results = [d for n, d in events if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is True
    done = [d for n, d in events if n == "done"]
    assert done[0]["mode"] == "manual"
    history = await app.state.sessions.load(
        "web-man", user_id=uid, project_id=pid)
    assert [m.get("role") for m in history] == [
        "user", "assistant", "tool", "assistant"]


async def test_manual_deny_feeds_model(
    client, byok_env, router_setter, monkeypatch
):
    bodies, handler = scripted([
        tool_body("w1", [fn_call("c1", "write_file",
                                 {"path": "x.txt", "content": "no"})]),
        provider_body("w1", content="ok, skipped"),
    ])
    router_setter({"w1.example.com": handler})

    async def exploding_write(path, content):
        raise AssertionError("denied write must never execute")

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "_write_file", exploding_write)
    uid, _pid = await agent_user(client, "deny2@example.com")
    events, approved_resp = await _collect_with_approval(
        client,
        {"session_id": "web-deny2", "message": "write it",
         "mode": "manual"},
        uid, False,
    )
    assert approved_resp.status_code == 200, approved_resp.text
    results = [d for n, d in events if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is False
    done = [d for n, d in events if n == "done"]
    assert len(done) == 1


async def test_approve_endpoint_gates(client, byok_env):
    uid, _pid = await agent_user(client, "gates@example.com",
                                 credential_count=0)
    raw = (await ApiKeyStore(app.state.engine).create(uid, label="t"))["raw"]
    # Anonymous and inv_-key callers are rejected (cookie realm only).
    client.cookies.delete(SESSION_COOKIE)
    assert (await client.post(
        "/dashboard/chat/approve",
        json={"token": "x", "approve": True})).status_code == 401
    assert (await client.post(
        "/dashboard/chat/approve", json={"token": "x", "approve": True},
        headers={"Authorization": f"Bearer {raw}"})).status_code == 401


async def test_approve_endpoint_validation_and_404(client, byok_env):
    await agent_user(client, "approve404@example.com", credential_count=0)
    assert (await client.post(
        "/dashboard/chat/approve", json={})).status_code == 400
    assert (await client.post(
        "/dashboard/chat/approve",
        json={"token": "nope", "approve": "yes"})).status_code == 400
    missed = await client.post(
        "/dashboard/chat/approve",
        json={"token": "no-such-token", "approve": True})
    assert missed.status_code == 404, missed.text
    assert missed.json()["detail"]["error"]["type"] == "not_found_error"


async def test_approve_foreign_token_is_404(client, byok_env):
    await agent_user(client, "owner@example.com", credential_count=0)
    waiter: ApprovalWaiter = app.state.webchat_approvals
    seen = {}

    async def waiter_side():
        seen["ok"] = await waiter.wait("tok-foreign", 999999)

    task = asyncio.ensure_future(waiter_side())
    await asyncio.sleep(0)
    # Another user's token resolves as unknown (never leaks existence).
    resp = await client.post(
        "/dashboard/chat/approve",
        json={"token": "tok-foreign", "approve": True})
    assert resp.status_code == 404, resp.text
    assert waiter.resolve("tok-foreign", 999999, True) is True
    await task
    assert seen["ok"] is True
    # Settled tokens cannot resolve twice.
    assert waiter.resolve("tok-foreign", 999999, True) is False


async def test_double_approve_is_404(client, byok_env, router_setter,
                                     monkeypatch):
    bodies, handler = scripted([
        tool_body("w1", [fn_call("c1", "execute_bash",
                                 {"command": "echo once"})]),
        provider_body("w1", content="done"),
    ])
    router_setter({"w1.example.com": handler})

    async def fake_run(command, timeout=30.0):
        return {"status": "ok", "output": "once"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "_run_command", fake_run)
    uid, _pid = await agent_user(client, "double@example.com")
    waiter: ApprovalWaiter = app.state.webchat_approvals
    seen: dict = {}

    async def approver():
        for _ in range(400):
            await asyncio.sleep(0.05)
            live = waiter.pending_tokens(uid)
            if live:
                token = live[0]
                seen["token"] = token
                first = await client.post(
                    "/dashboard/chat/approve",
                    json={"token": token, "approve": True})
                assert first.status_code == 200, first.text
                return

    async with asyncio.timeout(60):
        task = asyncio.create_task(approver())
        resp = await client.post("/dashboard/chat/stream", json={
            "session_id": "web-dbl", "message": "run it",
            "mode": "manual",
        })
        await task
    assert resp.status_code == 200, resp.text
    assert "token" in seen
    second = await client.post(
        "/dashboard/chat/approve",
        json={"token": seen["token"], "approve": True})
    assert second.status_code == 404, second.text


async def test_waiter_timeout_and_discard(monkeypatch):
    import invincible.core.webchat_agent as wa

    waiter = ApprovalWaiter()
    assert waiter.resolve("missing", 1, True) is False
    waiter.discard("missing")  # never raises
    monkeypatch.setattr(wa, "APPROVAL_WAIT_SECONDS", 0.05)
    assert await waiter.wait("tok-t", 1) is False  # nobody resolves
    with pytest.raises(asyncio.CancelledError):
        task = asyncio.ensure_future(waiter.wait("tok-x", 1))
        await asyncio.sleep(0)
        waiter.discard("tok-x")
        await task
