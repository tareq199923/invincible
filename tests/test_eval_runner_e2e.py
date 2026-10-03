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


# Live shape is plain strings; objects tolerated for forward compat.
def test_model_ids_accepts_strings_and_objects():
    assert eval_runner.model_ids_from_payload(
        {"models": ["a", "b"]}) == ["a", "b"]
    assert eval_runner.model_ids_from_payload(
        {"models": [{"id": "a"}, {"id": "b"}]}) == ["a", "b"]
    assert eval_runner.model_ids_from_payload(
        {"models": ["a", 1, None, " ", {"id": ""}]}) == ["a"]
    assert eval_runner.model_ids_from_payload({}) == []
    assert eval_runner.model_ids_from_payload(None) == []


def test_cli_run_delay_defaults():
    from tools.eval.run_eval import build_parser

    args = build_parser().parse_args(["run", "--label", "x", "--model", "m"])
    assert args.delay_seconds == 0.0
    assert args.repeat == 3
    assert args.concurrency == 1
    paced = build_parser().parse_args(
        ["run", "--label", "x", "--model", "m", "--delay-seconds", "20"])
    assert paced.delay_seconds == 20.0


async def test_run_all_delay_paces_runs():
    import time
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory():
        async with httpx.AsyncClient(transport=_transport({})) as client:
            yield client

    start = time.monotonic()
    runs = await eval_runner.run_all(
        "http://test", "fake-model", [_fake_task()], repeat=2,
        delay_seconds=0.2, client_factory=factory,
    )
    elapsed = time.monotonic() - start
    assert len(runs) == 2
    assert all(r["passed"] for r in runs)
    assert elapsed >= 0.2


# --- outcomes: timeouts/errors are infra, never a pass ------------------------


def _fixture_task(task_id: str) -> task_schema.EvalTask:
    """A task whose only check passes as soon as the fixture is written.

    Used to prove a timed-out run is NOT scored as a pass even though its
    end-state checks happen to pass.
    """
    return task_schema.validate_task_dict({
        "id": task_id,
        "category": "write",
        "prompt": "touch ok.txt",
        "files": {"ok.txt": "x"},
        "checks": [{"type": "file_exists", "path": "ok.txt"}],
    })


async def test_run_once_timeout_is_never_a_pass(monkeypatch):
    """Regression: a run that hits the per-task timeout used to be graded
    on end-state checks alone and could be counted as PASSED."""
    import asyncio as _asyncio

    async def _timed_out(coro, timeout=None):
        coro.close()  # never start the stream
        raise _asyncio.TimeoutError

    monkeypatch.setattr(eval_runner.asyncio, "wait_for", _timed_out)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        run = await eval_runner.run_once(
            client, base_url="http://x", model="m",
            task=_fixture_task("e2e-timeout"),
        )
    assert run["error"]["message"] == "task timeout"
    assert run["passed"] is False          # end-state check passed...
    assert run["outcome"] == "timeout"     # ...but the run is infra
    assert run["checks"][0]["passed"] is True


async def test_run_once_stream_error_is_error_outcome():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dashboard/chat/stream":
            return httpx.Response(500, content=b"boom")
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        run = await eval_runner.run_once(
            client, base_url="http://x", model="m",
            task=_fixture_task("e2e-error"),
        )
    assert run["passed"] is False
    assert run["outcome"] == "error"


async def test_run_all_forwards_timeout_override(monkeypatch):
    from contextlib import asynccontextmanager

    seen: dict = {}

    async def fake(client, *, task, timeout_seconds=None, **kwargs):
        seen["timeout_seconds"] = timeout_seconds
        return {"task_id": task.id, "passed": True}

    monkeypatch.setattr(eval_runner, "run_once", fake)

    @asynccontextmanager
    async def factory():
        async with httpx.AsyncClient(transport=_transport({})) as client:
            yield client

    await eval_runner.run_all(
        "http://test", "fake-model", [_fixture_task("e2e-fwd")], repeat=1,
        timeout_seconds=42.0, client_factory=factory,
    )
    assert seen["timeout_seconds"] == 42.0


def test_cli_timeout_seconds_default_and_override():
    from tools.eval.run_eval import build_parser

    args = build_parser().parse_args(["run", "--label", "x", "--model", "m"])
    assert args.timeout_seconds is None       # default = each task's own
    over = build_parser().parse_args(
        ["run", "--label", "x", "--model", "m", "--timeout-seconds", "30"])
    assert over.timeout_seconds == 30.0


def test_cli_rejects_nonpositive_timeout(capsys):
    from tools.eval.run_eval import main

    rc = main(["run", "--label", "x", "--model", "m",
               "--timeout-seconds", "0"])
    assert rc == 2
    assert "--timeout-seconds must be > 0" in capsys.readouterr().out
