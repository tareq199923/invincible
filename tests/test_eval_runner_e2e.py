"""MockTransport end-to-end: login -> preflight -> stream -> approval -> grade.

No Postgres, no network, no real provider. Proves the runner drives the
wire protocol (SSE + approve-while-streaming) and grades deterministically.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tools.eval import runner as eval_runner
from tools.eval import tasks as task_schema

SSE_STREAM = (
    'event: token\ndata: {"text": "CALC-"}\n\n'
    'event: token\ndata: {"text": "OK"}\n\n'
    'event: tool_call\ndata: {"call_id": "c1", "name": "execute_bash", '
    '"summary": "Run: echo hi"}\n\n'
    'event: approval\ndata: {"token": "tok-1", "call_id": "c1", '
    '"action": "execute_bash", "summary": "Run: echo hi", "detail": "echo hi"}\n\n'
    'event: tool_result\ndata: {"call_id": "c1", "name": "execute_bash", '
    '"ok": true, "preview": "hi"}\n\n'
    'event: done\ndata: {"text": "CALC-OK", "provider": "fake", '
    '"model": "fake-model", "attempts": 1, "tools_used": 1, '
    '"execution": "local"}\n\n'
)


def _fake_task() -> task_schema.EvalTask:
    return task_schema.validate_task_dict({
        "id": "e2e-fake",
        "category": "write",
        "prompt": "say CALC-OK",
        "files": {"a.txt": "hello"},
        "checks": [
            {"type": "final_text_contains", "pattern": "CALC-OK"},
            {"type": "tool_called", "tool": "execute_bash"},
        ],
    })


def _transport(seen: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/auth/login":
            seen["login"] = True
            return httpx.Response(
                200, json={"id": 9, "email": "eval@test.com"},
                headers={"set-cookie": "invincible_session=abc123; Path=/"},
            )
        if path == "/auth/me":
            return httpx.Response(200, json={"id": 9})
        if path == "/dashboard/chat/models":
            # Real wire shape: plain id strings (see chat_models).
            return httpx.Response(200, json={"models": ["fake-model"]})
        if path == "/dashboard/chat/stream":
            body = json.loads(request.content.decode("utf-8"))
            assert body["mode"] == "manual"
            assert body["session_id"].startswith("web-eval-")
            assert "working directory" in body["message"]
            return httpx.Response(
                200, content=SSE_STREAM.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        if path == "/dashboard/chat/approve":
            payload = json.loads(request.content.decode("utf-8"))
            seen.setdefault("approvals", []).append(payload)
            assert payload == {"token": "tok-1", "approve": True}
            return httpx.Response(200, json={"ok": True, "approved": True})
        return httpx.Response(404, json={"error": "unexpected " + path})

    return httpx.MockTransport(handler)


async def test_e2e_login_stream_approve_grade():
    seen: dict = {}
    async with httpx.AsyncClient(transport=_transport(seen)) as client:
        await eval_runner.login(
            client, "http://test", "eval@test.com", "pw", None)
        assert seen["login"] is True
        ids, me_id = await eval_runner.preflight(
            client, "http://test", "fake-model")
        assert ids == ["fake-model"]
        assert me_id == 9
        run = await eval_runner.run_once(
            client, base_url="http://test", model="fake-model",
            task=_fake_task(),
        )
    assert run["passed"] is True
    assert run["final_text"] == "CALC-OK"
    assert run["tool_counts"] == {"execute_bash": 1}
    assert run["approvals_asked"] == 1
    assert run["approvals_approved"] == 1
    assert run["approvals"][0]["approved"] is True
    assert seen["approvals"] == [{"token": "tok-1", "approve": True}]
    assert run["done"]["provider"] == "fake"
    assert run["hit_cap"] is False
    # Workspace cleaned up (no --keep-workspace).
    import os

    assert not os.path.exists(run["workspace"])


async def test_risky_approval_denied_over_wire():
    """Same stream, but the tool is dangerous -> runner denies it."""
    evil_stream = (
        'event: approval\ndata: {"token": "tok-9", "call_id": "c9", '
        '"action": "execute_bash", "summary": "Run: rm -rf /", '
        '"detail": "rm -rf /"}\n\n'
        'event: done\ndata: {"text": "declined", "provider": "fake", '
        '"model": "fake-model", "attempts": 1, "tools_used": 0, '
        '"execution": "local"}\n\n'
    )
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dashboard/chat/stream":
            return httpx.Response(200, content=evil_stream.encode("utf-8"))
        if request.url.path == "/dashboard/chat/approve":
            payload = json.loads(request.content.decode("utf-8"))
            seen["approval"] = payload
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        run = await eval_runner.run_once(
            client, base_url="http://x", model="m", task=_fake_task(),
        )
    assert seen["approval"] == {"token": "tok-9", "approve": False}
    assert run["approvals_denied"] == 1


async def test_preflight_failures_are_loud():
    async def _ids(handler, model="m"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            return await eval_runner.preflight(client, "http://x", model)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(eval_runner.EvalError, match="server is down"):
        await _ids(down)

    def no_provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={})
        if request.url.path == "/auth/me":
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(200, json={"models": []})

    with pytest.raises(eval_runner.EvalError, match="no provider"):
        await _ids(no_provider)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(401, json={"error": "x"})
        )
    ) as client:
        with pytest.raises(eval_runner.EvalError, match="401"):
            await eval_runner.login(client, "http://x", "e", "p", None)


def test_model_ids_accepts_strings_and_objects():
    # Live shape is plain strings; objects tolerated for forward compat.
    assert eval_runner.model_ids_from_payload(
        {"models": ["a", "b"]}) == ["a", "b"]
    assert eval_runner.model_ids_from_payload(
        {"models": [{"id": "a"}, {"id": "b"}]}) == ["a", "b"]
    assert eval_runner.model_ids_from_payload(
        {"models": ["a", 1, None, " ", {"id": ""}]}) == ["a"]
    assert eval_runner.model_ids_from_payload({}) == []
    assert eval_runner.model_ids_from_payload(None) == []
