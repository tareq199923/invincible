# tests/test_computer_use.py
"""F10 Pack 3 computer-use: paired-PC input + launch tools.

Pure helpers (chord denylist, arg validation) are hermetic; the
SendInput/ShellExecute boundary is never touched by refusal-triggering
args (blocks and error results prove the runner handles the job type).
Live input runs only on a paired Windows PC via the agent runner —
here the OS side is faked at ``build_agent_executor`` so no real
mouse, keyboard, or launcher is ever driven.
"""

import asyncio
import os

import pytest

from invincible.core import harness_tools, tool_executor
from invincible.core.harness_policy import before_tool_call

COMPUTER_TOOLS = ("click", "type_text", "press_keys", "scroll", "drag",
                  "open_app", "open_url")


def test_chord_parsing_normalizes_aliases():
    assert tool_executor.parse_keys_chord("Ctrl+Shift+Esc") == frozenset(
        {"ctrl", "shift", "esc"})
    assert tool_executor.parse_keys_chord("windows+l") == frozenset(
        {"win", "l"})
    assert tool_executor.parse_keys_chord("control+alt+delete") == (
        frozenset({"ctrl", "alt", "del"}))
    assert tool_executor.parse_keys_chord("") == frozenset()
    assert tool_executor.parse_keys_chord("  F5  ") == frozenset({"f5"})


def test_chord_denylist_blocks_lock_close_sas():
    for chord in ("win+l", "Win+L", "alt+f4", "ctrl+alt+del",
                  "ctrl+alt+delete", "ctrl+shift+esc",
                  "windows+l"):
        with pytest.raises(tool_executor.ToolBlocked):
            tool_executor.check_keys_denylist(chord)


def test_chord_denylist_passes_plain_keys():
    # Single keys and ordinary chords never match: every blocked entry
    # needs its full modifier set.
    for chord in ("l", "f4", "ctrl+s", "alt+tab", "f5", "ctrl+shift+s",
                  "win+e", "ctrl+alt+t"):
        tool_executor.check_keys_denylist(chord)


def test_normalize_click_xy_and_control():
    assert tool_executor.normalize_computer_args(
        "click", {"x": 500, "y": 200}) == {
        "x": 500, "y": 200, "button": "left", "double": False}
    assert tool_executor.normalize_computer_args(
        "click", {"control": "C3", "x": 1, "y": 1,
                  "button": "RIGHT", "double": True}) == {
        "control": "C3", "button": "right", "double": True}
    for bad in ({"x": 9999, "y": 0}, {"x": 0}, {"y": 0}, {},
                {"x": "left", "y": 0}, {"button": "side"},
                {"control": "", "x": 1, "y": 1},
                {"control": 5, "x": 1, "y": 1}):
        with pytest.raises(ValueError):
            tool_executor.normalize_computer_args("click", bad)


def test_normalize_type_press_scroll_drag():
    assert tool_executor.normalize_computer_args(
        "type_text", {"text": "hi"})["submit"] is False
    assert tool_executor.normalize_computer_args(
        "press_keys", {"keys": "ctrl+s"}) == {"keys": "ctrl+s"}
    assert tool_executor.normalize_computer_args(
        "scroll", {"direction": "Down"}) == {"direction": "down",
                                             "amount": 3}
    assert tool_executor.normalize_computer_args(
        "drag", {"control_from": "C1", "x2": 9, "y2": 9}) == {
        "from": {"control": "C1"}, "to": {"x2": 9, "y2": 9}}
    for name, bad in (
        ("type_text", {"text": ""}),
        ("type_text", {"text": "x" * 2001}),
        ("press_keys", {"keys": ""}),
        ("press_keys", {"keys": "win+l"}),
        ("press_keys", {"keys": "ctrl+shift"}),
        ("scroll", {"direction": "sideways"}),
        ("scroll", {"direction": "up", "amount": 99}),
        ("drag", {}),
        ("drag", {"x1": 1, "y1": 1}),
    ):
        with pytest.raises((ValueError, tool_executor.ToolBlocked)):
            tool_executor.normalize_computer_args(name, bad)


def test_normalize_open_app_url():
    assert tool_executor.normalize_computer_args(
        "open_app", {"name": "notepad"}) == {"name": "notepad",
                                             "args": ""}
    assert tool_executor.normalize_computer_args(
        "open_url", {"url": "https://example.com/x"}) == {
        "url": "https://example.com/x"}
    for name, bad in (("open_app", {"name": ""}),
                      ("open_app", {"name": "  "}),
                      ("open_url", {"url": "not-a-url"}),
                      ("open_url", {"url": "ftp://x/y"}),
                      ("open_url", {})):
        with pytest.raises(ValueError):
            tool_executor.normalize_computer_args(name, bad)
    with pytest.raises(ValueError):
        tool_executor.normalize_computer_args("frobnicate", {})


def test_computer_tools_registered_agent_only_approval():
    for name in COMPUTER_TOOLS:
        tool = harness_tools.get_tool(name)
        assert tool.read_only is False
        assert tool.needs_approval is True
        assert tool.mcp is True
        assert tool.webchat is True
        assert tool.agent_job == name
        assert tool.server_executable is False
        assert name in harness_tools.agent_only_names()
        assert name in harness_tools.approval_required_names()
        assert name not in harness_tools.explore_names()
        assert name not in harness_tools.read_only_names()
        modes = harness_tools.webchat_names_for_mode
        assert name not in modes("plan")
        assert name in modes("manual")
        assert name in modes("auto")
    from invincible.core import webchat_agent

    assert set(webchat_agent.COMPUTER_ONLY_MUTATING) == set(
        COMPUTER_TOOLS)


# --- F25: open_app launch wait (window + first controls) --------------------


def test_normalize_open_app_wait_args():
    te = tool_executor
    # No wait trigger: today's exact shape, no wait keys.
    assert te.normalize_computer_args(
        "open_app", {"name": "notepad"}) == {"name": "notepad", "args": ""}
    assert te.normalize_computer_args(
        "open_app", {"name": "notepad", "timeout_ms": 5000}) == {
            "name": "notepad", "args": ""}
    assert te.normalize_computer_args(
        "open_app", {"name": "notepad", "window": "   "}) == {
            "name": "notepad", "args": ""}
    # A bare selector waits for the window itself, with the launch
    # default (8000), not the look default (3000).
    bare = te.normalize_computer_args(
        "open_app", {"name": "spotify", "window": "process:spotify"})
    assert bare == {
        "name": "spotify", "args": "", "window": "process:spotify",
        "wait_kind": None, "wait_label": None,
        "timeout_ms": te.OPEN_APP_WAIT_DEFAULT_MS, "wait_window": True}
    # Wait filters without a selector target the foreground window.
    filtered = te.normalize_computer_args(
        "open_app", {"name": "spotify", "wait_kind": "Button",
                     "wait_label": "  Play ", "timeout_ms": 5000})
    assert filtered["wait_kind"] == "button"
    assert filtered["wait_label"] == "Play"
    assert filtered["timeout_ms"] == 5000
    assert filtered["wait_window"] is False
    assert filtered["window"] is None
    # Staged dicts are re-normalized at execution (Wall 2): idempotent.
    for raw in ({"name": "spotify", "window": "process:spotify"},
                {"name": "spotify", "wait_label": "Play"}):
        once = te.normalize_computer_args("open_app", raw)
        assert te.normalize_computer_args("open_app", once) == once
    for bad in ({"name": "x", "window": "pid:abc"},
                {"name": "x", "window": "process:"},
                {"name": "x", "wait_kind": "not-a-kind"},
                {"name": "x", "wait_label": "Play", "timeout_ms": -1},
                {"name": "x", "wait_label": "Play", "timeout_ms": "soon"},
                {"name": "x", "timeout_ms": 5000, "window": 123}):
        with pytest.raises(ValueError):
            te.normalize_computer_args("open_app", bad)


def test_open_wait_result_merge_shape():
    opened = {"status": "ok", "action": "open_app", "name": "notepad",
              "verified": False}
    look = {
        "status": "look", "app": "Untitled - Notepad",
        "controls": [{"id": "C1", "kind": "text field", "label": "Text",
                      "x": 10, "y": 20}],
        "matched": {"id": "C1", "kind": "text field", "label": "Text",
                    "x": 10, "y": 20},
        "ready": True, "waited_ms": 120,
        "window": {"title": "Untitled - Notepad",
                   "process": "notepad.exe", "pid": 42},
        "mouse": {"x": 1, "y": 2}, "size": {"w": 1000, "h": 1000},
        "near": None, "truncated": False,
    }
    merged = tool_executor._open_wait_result(opened, look)
    assert merged["status"] == "ok"
    assert merged["action"] == "open_app"
    assert merged["verified"] is False
    assert merged["matched"]["id"] == "C1"
    assert merged["ready"] is True
    assert set(merged) == {
        "status", "action", "name", "verified", "app", "controls",
        "matched", "ready", "waited_ms", "window", "mouse", "size",
        "near", "truncated"}
    # Optional look keys ride along untouched.
    hinted = tool_executor._open_wait_result(
        opened, {**look, "note": "waiting note", "mouse_hint": "hint"})
    assert hinted["note"] == "waiting note"
    assert hinted["mouse_hint"] == "hint"
    # A failed readiness read keeps the launch and admits the miss.
    failed = tool_executor._open_wait_result(
        opened, {"status": "error", "error": "reader crashed"})
    assert failed["status"] == "ok"
    assert failed["ready"] is False
    assert failed["matched"] is None
    assert "readiness read failed" in failed["note"]
    assert "reader crashed" in failed["note"]
    assert set(failed) == {"status", "action", "name", "verified",
                           "ready", "matched", "note"}


def _open_app_env(monkeypatch, open_result):
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})
    monkeypatch.setattr(te, "_computer_open",
                        lambda api, name, extra: dict(open_result))
    return te


async def test_computer_open_app_plain_never_reads(monkeypatch):
    te = _open_app_env(monkeypatch, {
        "status": "ok", "action": "open_app", "name": "notepad",
        "verified": False})
    calls = []

    async def fake_look(**kwargs):
        calls.append(kwargs)
        return {"status": "look"}

    monkeypatch.setattr(te, "_look_at_screen", fake_look)
    result = await te._computer_act("open_app", {"name": "notepad"})
    assert result == {"status": "ok", "action": "open_app",
                      "name": "notepad", "verified": False}
    assert calls == []


async def test_computer_open_app_wait_merges_look(monkeypatch):
    te = _open_app_env(monkeypatch, {
        "status": "ok", "action": "open_app", "name": "spotify",
        "verified": False})
    seen = {}

    async def fake_look(**kwargs):
        seen.update(kwargs)
        return {"status": "look", "app": "Spotify",
                "controls": [{"id": "C7", "kind": "button",
                              "label": "Play", "x": 5, "y": 6}],
                "matched": {"id": "C7", "kind": "button",
                            "label": "Play", "x": 5, "y": 6},
                "ready": True, "waited_ms": 3100,
                "window": {"title": "Spotify", "process": "Spotify.exe",
                           "pid": 99},
                "mouse": {"x": 0, "y": 0}, "size": {"w": 800, "h": 600},
                "near": None, "truncated": False}

    monkeypatch.setattr(te, "_look_at_screen", fake_look)
    result = await te._computer_act(
        "open_app", {"name": "spotify", "window": "process:spotify",
                     "wait_label": "Play"})
    assert seen == {"window": "process:spotify", "wait_kind": None,
                    "wait_label": "Play",
                    "timeout_ms": te.OPEN_APP_WAIT_DEFAULT_MS,
                    "wait_window": True}
    assert result["status"] == "ok"
    assert result["action"] == "open_app"
    assert result["name"] == "spotify"
    assert result["verified"] is False
    assert result["matched"] == {"id": "C7", "kind": "button",
                                 "label": "Play", "x": 5, "y": 6}
    assert result["ready"] is True
    assert result["waited_ms"] == 3100


async def test_computer_open_app_wait_timeout_keeps_launch(monkeypatch):
    te = _open_app_env(monkeypatch, {
        "status": "ok", "action": "open_app", "name": "spotify",
        "verified": False})
    note = "no window matched process:spotify before the wait expired"

    async def fake_look(**kwargs):
        return {"status": "look", "app": None, "controls": [],
                "matched": None, "ready": False, "waited_ms": 8001,
                "window": None, "mouse": {"x": 0, "y": 0},
                "size": {"w": 800, "h": 600}, "near": None,
                "truncated": False, "note": note}

    monkeypatch.setattr(te, "_look_at_screen", fake_look)
    result = await te._computer_act(
        "open_app", {"name": "spotify", "window": "process:spotify"})
    assert result["status"] == "ok"
    assert result["verified"] is False
    assert result["ready"] is False
    assert result["matched"] is None
    assert result["note"] == note


async def test_computer_open_app_reader_failure_keeps_launch(monkeypatch):
    te = _open_app_env(monkeypatch, {
        "status": "ok", "action": "open_app", "name": "spotify",
        "verified": False})

    async def fake_look(**kwargs):
        return {"status": "error", "error": "reader crashed"}

    monkeypatch.setattr(te, "_look_at_screen", fake_look)
    result = await te._computer_act(
        "open_app", {"name": "spotify", "wait_label": "Play"})
    assert result["status"] == "ok"
    assert result["verified"] is False
    assert result["ready"] is False
    assert result["matched"] is None
    assert "readiness read failed" in result["note"]
    assert "reader crashed" in result["note"]


async def test_computer_open_app_launch_failure_never_reads(monkeypatch):
    error = {"status": "error", "error": "could not open 'spotify'"}
    te = _open_app_env(monkeypatch, error)
    calls = []

    async def fake_look(**kwargs):
        calls.append(kwargs)
        return {"status": "look"}

    monkeypatch.setattr(te, "_look_at_screen", fake_look)
    result = await te._computer_act(
        "open_app", {"name": "spotify", "window": "process:spotify"})
    assert result == error
    assert calls == []


def test_policy_press_keys_gated_rest_passthrough():
    assert _outcome("press_keys", {"keys": "win+l"}) == "blocked"
    assert _outcome("press_keys", {"keys": "ctrl+s"}) == "pass"
    for name in ("click", "type_text", "scroll", "drag", "open_app",
                 "open_url"):
        assert _outcome(name, {}) == "pass", name
        assert _outcome(name, {}, agent_routed=True) == "pass", name


def _outcome(tool_name: str, args: dict, **kwargs) -> str:
    try:
        before_tool_call(tool_name, args, **kwargs)
    except tool_executor.ToolBlocked:
        return "blocked"
    return "pass"


async def test_stage_and_confirm_round_trip():
    store = tool_executor.PendingActionStore()
    staged = tool_executor.stage_computer_action(
        "click", {"x": 10, "y": 20}, store, owner_subject=7)
    assert staged["status"] == "pending_confirmation"
    assert staged["action"] == "click"
    assert len(store) == 1

    declined = await tool_executor.confirm_action(
        store, staged["token"], False, requester_subject=7)
    assert declined == {"status": "declined"}

    staged = tool_executor.stage_computer_action(
        "click", {"x": 10, "y": 20}, store, owner_subject=7)
    foreign = await tool_executor.confirm_action(
        store, staged["token"], True, requester_subject=8)
    assert foreign == {"status": "not_found"}

    async def fake_executor(action_type, args):
        return {"status": "ok", "action": action_type,
                "echo": sorted(args)}

    ran = await tool_executor.confirm_action(
        store, staged["token"], True, requester_subject=7,
        executor=fake_executor)
    assert ran["status"] == "ok" and ran["action"] == "click"

    # No local path: approval without an agent executor is
    # unavailability, never a server-side click.
    staged = tool_executor.stage_computer_action(
        "scroll", {"direction": "up"}, store, owner_subject=7)
    local = await tool_executor.confirm_action(
        store, staged["token"], True, requester_subject=7,
        executor=None)
    assert local["status"] == "unavailable"

    # Shape and denylist failures stage nothing (no dangling tokens).
    for name, args in (("click", {"x": 9999, "y": 0}),
                       ("press_keys", {"keys": "alt+f4"})):
        before = len(store)
        try:
            tool_executor.stage_computer_action(
                name, args, store, owner_subject=7)
        except (ValueError, tool_executor.ToolBlocked):
            pass
        else:
            raise AssertionError(f"{name} staged {args}")
        assert len(store) == before


async def test_open_app_wait_survives_stage_and_confirm():
    """F25: the staged wait spec survives confirm forwarding and the
    agent-side Wall 2 re-normalization (it is its own fixed point)."""
    store = tool_executor.PendingActionStore()
    staged = tool_executor.stage_computer_action(
        "open_app",
        {"name": "spotify", "window": "process:spotify",
         "wait_label": "Play"},
        store, owner_subject=7)
    assert staged["status"] == "pending_confirmation"
    seen = {}

    async def fake_executor(action_type, args):
        seen["action"], seen["args"] = action_type, args
        return {"status": "ok", "action": action_type}

    result = await tool_executor.confirm_action(
        store, staged["token"], True, requester_subject=7,
        executor=fake_executor)
    assert result["status"] == "ok"
    assert seen["action"] == "open_app"
    assert seen["args"] == {
        "name": "spotify", "args": "", "window": "process:spotify",
        "wait_kind": None, "wait_label": "Play",
        "timeout_ms": tool_executor.OPEN_APP_WAIT_DEFAULT_MS,
        "wait_window": True}
    assert tool_executor.normalize_computer_args(
        "open_app", seen["args"]) == seen["args"]


async def test_runner_refusals_never_touch_input():
    from invincible.agent import runner

    cases = {
        "click": {"x": 9999, "y": 0},
        "type_text": {"text": ""},
        "press_keys": {"keys": "win+l"},
        "scroll": {"direction": "sideways"},
        "drag": {},
        "open_app": {"name": ""},
        "open_url": {"url": "not-a-url"},
    }
    for job_type, args in cases.items():
        result = await runner.execute_job(
            {"type": job_type, "args": dict(args)})
        assert result.get("error", "") != (
            f"Unknown job type: {job_type}"), job_type
        assert result["status"] in ("error", "blocked",
                                    "unavailable"), (job_type, result)
    blocked = await runner.execute_job(
        {"type": "press_keys", "args": {"keys": "ctrl+alt+del"}})
    assert blocked["status"] in ("blocked", "unavailable"), blocked


async def test_execute_call_unavailable_without_agent():
    import invincible.core.webchat_agent as webchat_agent
    from invincible.core.principal import Principal

    outcome: dict = {}
    seen = []

    async def audit(name, meta=None):
        seen.append(name)

    async for _event in webchat_agent._execute_call(
            "click", {"x": 1, "y": 2}, call_id="c0", mode="manual",
            ctx_principal=Principal(user_id=9, project_id=1,
                                    kind="api_key"),
            pending_store=tool_executor.PendingActionStore(),
            executor=None,
            waiter=webchat_agent.ApprovalWaiter(), audit=audit,
            outcome=outcome):
        pass
    assert outcome["ok"] is False
    assert outcome["result"]["status"] == "unavailable"
    assert seen == []


async def test_password_probe_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert await tool_executor._focus_is_password() is None


# --- DB-backed webchat flow (fake provider + fake agent executor) ---

def _fn_call(call_id, name, args):
    import json as _json

    return {"id": call_id, "type": "function",
            "function": {"name": name,
                         "arguments": _json.dumps(args)}}


async def _collect_approving(client, body, uid, count):
    """Run a manual turn while approving the first ``count`` tokens.

    Answers every pending token (not just the first) so multi-call
    turns complete; bounded so a regression fails fast, never hangs.
    """
    import json as _json

    from invincible.core.webchat_agent import ApprovalWaiter
    from invincible.main import app

    waiter: ApprovalWaiter = app.state.webchat_approvals
    answered: list = []

    async def approver():
        for _ in range(400):
            await asyncio.sleep(0.05)
            for token in waiter.pending_tokens(uid):
                if token not in answered:
                    answered.append(token)
                    await client.post(
                        "/dashboard/chat/approve",
                        json={"token": token, "approve": True},
                    )
            if len(answered) >= count:
                return

    async def _run():
        task = asyncio.create_task(approver())
        resp = await client.post("/dashboard/chat/stream", json=body)
        await task
        return resp

    resp = await asyncio.wait_for(_run(), timeout=60)
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
    return events


def _fake_executor_factory(calls):
    async def fake(action_type, args):
        calls.append((action_type, args))
        return {"status": "ok", "action": action_type}

    return fake


def _patch_executor(monkeypatch, ran):
    """Fake the paired agent at the endpoint boundary (endpoints/chat
    binds build_agent_executor at import, so patch it there)."""
    import invincible.endpoints.chat as chat_endpoint

    monkeypatch.setattr(chat_endpoint, "build_agent_executor",
                        lambda registry, subject, routing_on:
                        _fake_executor_factory(ran))


async def test_manual_click_approval_card(
    client, byok_env, router_setter, monkeypatch
):
    import httpx

    from tests.test_webchat_agent import agent_user, provider_body, tool_body

    bodies_calls = [
        tool_body("w1", [_fn_call("c1", "click",
                                  {"x": 500, "y": 200})]),
        provider_body("w1", content="clicked it"),
    ]
    state = {"i": 0}

    def handler(request):
        resp = bodies_calls[min(state["i"], len(bodies_calls) - 1)]
        state["i"] += 1
        return httpx.Response(200, json=resp)

    router_setter({"w1.example.com": handler})
    ran = []
    _patch_executor(monkeypatch, ran)
    uid, _ = await agent_user(client, "clickcard@example.com")
    events = await _collect_approving(client, {
        "session_id": "web-click", "message": "click there",
        "mode": "manual"}, uid, 1)
    approvals = [d for n, d in events if n == "approval"]
    assert len(approvals) == 1
    assert approvals[0]["action"] == "click"
    assert approvals[0]["summary"] == "Click 500,200 (left)"
    assert "500" in approvals[0]["detail"]
    results = [d for n, d in events if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is True
    assert results[0]["name"] == "click"
    assert ran and ran[0][0] == "click"


async def test_two_clicks_still_surface_two_approvals(
    client, byok_env, router_setter, monkeypatch
):
    """Batch-trap regression: two approval-gated calls in one turn must
    take the sequential path (two approval cards), never the parallel
    batch path (which swallows approval events and hangs to decline)."""
    import httpx

    from tests.test_webchat_agent import agent_user, provider_body, tool_body

    bodies_calls = [
        tool_body("w1", [_fn_call("c1", "click", {"x": 10, "y": 10}),
                         _fn_call("c2", "click", {"x": 20, "y": 20})]),
        provider_body("w1", content="clicked twice"),
    ]
    state = {"i": 0}

    def handler(request):
        resp = bodies_calls[min(state["i"], len(bodies_calls) - 1)]
        state["i"] += 1
        return httpx.Response(200, json=resp)

    router_setter({"w1.example.com": handler})
    ran = []
    _patch_executor(monkeypatch, ran)
    uid, _ = await agent_user(client, "batchclick@example.com")
    events = await _collect_approving(client, {
        "session_id": "web-batch", "message": "click both",
        "mode": "manual"}, uid, 2)
    approvals = [d for n, d in events if n == "approval"]
    assert len(approvals) == 2, [
        (n, (d or {}).get("action")) for n, d in events]
    assert [a["action"] for a in approvals] == ["click", "click"]
    results = [d for n, d in events if n == "tool_result"]
    assert len(results) == 2 and all(r["ok"] for r in results)
    assert len(ran) == 2


async def test_auto_click_runs_without_approval(
    client, byok_env, router_setter, monkeypatch
):
    import httpx

    from tests.test_webchat_agent import agent_user, provider_body, tool_body

    bodies_calls = [
        tool_body("w1", [_fn_call("c1", "press_keys",
                                  {"keys": "ctrl+s"})]),
        provider_body("w1", content="saved"),
    ]
    state = {"i": 0}

    def handler(request):
        resp = bodies_calls[min(state["i"], len(bodies_calls) - 1)]
        state["i"] += 1
        return httpx.Response(200, json=resp)

    router_setter({"w1.example.com": handler})
    ran = []
    _patch_executor(monkeypatch, ran)
    await agent_user(client, "autoclick@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-auto-click", "message": "save it",
        "mode": "auto"})
    assert resp.status_code == 200, resp.text
    import json as _json

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
    assert not [d for n, d in events if n == "approval"]
    results = [d for n, d in events if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is True
    assert ran and ran[0][0] == "press_keys"


async def test_plan_mode_offers_no_computer_use(
    client, byok_env, router_setter, monkeypatch
):

    from tests.test_webchat_agent import (
        agent_user,
        offered_tool_names,
        provider_body,
        scripted,
        tool_body,
    )

    bodies, handler = scripted([
        tool_body("w1", [_fn_call("c1", "read_file",
                                  {"path": "a.txt"})]),
        provider_body("w1", content="plan only"),
    ])
    router_setter({"w1.example.com": handler})
    import invincible.core.tool_executor as te

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "hello"}

    monkeypatch.setattr(te, "read_file", fake_read)
    await agent_user(client, "planpack@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-plan-pack", "message": "plan clicks",
        "mode": "plan"})
    assert resp.status_code == 200, resp.text
    offered = offered_tool_names(bodies)
    assert "click" not in offered
    assert "look_at_screen" in offered


def test_result_ok_unavailable_is_false():
    from invincible.core.tool_dispatch import result_ok

    assert result_ok({"status": "unavailable", "reason": "x"}) is False
    assert result_ok({"status": "ok"}) is True
    assert result_ok({"status": "error"}) is False


async def test_computer_stale_id_returns_error_not_raise(monkeypatch):
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")

    async def fake_look():
        return {"status": "look", "app": "Brave",
                "controls": [{"id": "C1", "kind": "button",
                              "label": "Go", "x": 10, "y": 10}]}

    monkeypatch.setattr(te, "_look_at_screen", fake_look)
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})
    result = await te._computer_act(
        "click", {"control": "C30", "button": "left"})
    assert result["status"] == "error"
    assert "stale" in result["error"]


async def test_computer_ok_carries_verified_false(monkeypatch):
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})

    seen = {}

    async def fake_resolve(target, *, on_point=None):
        return {"x": 1, "y": 2}, {"x": 1, "y": 2}

    async def fake_dwell(seconds):
        seen["dwell"] = seconds

    monkeypatch.setattr(te, "_computer_resolve", fake_resolve)
    monkeypatch.setattr(te, "_computer_dwell", fake_dwell)
    monkeypatch.setattr(te, "_computer_click_at",
                        lambda api, x, y, button, double: None)
    result = await te._computer_act(
        "click", {"x": 1, "y": 2, "button": "left"})
    assert result["status"] == "ok"
    assert result["verified"] is False


def test_move_to_uses_virtualdesk_flag(monkeypatch):
    import invincible.core.tool_executor as te

    seen = {}

    def fake_input(api, *, dx=0, dy=0, data=0, flags=0):
        seen["flags"] = flags
        seen["dx"] = dx
        seen["dy"] = dy
        return ("input", flags)

    def fake_send(api, inputs):
        seen["inputs"] = inputs

    monkeypatch.setattr(te, "_computer_mouse_input", fake_input)
    monkeypatch.setattr(te, "_computer_send", fake_send)
    te._computer_move_to({}, 500, 500)
    assert seen["flags"] & te._COMPUTER_MOUSE_VIRTUALDESK
    assert seen["flags"] & te._COMPUTER_MOUSE_ABSOLUTE
    assert seen["dx"] == te._computer_abs(500)
    assert seen["dy"] == te._computer_abs(500)


def test_press_chord_main_rides_vk_not_unicode(monkeypatch):
    """Live find: ctrl+z arrived as bare z — Unicode strokes ignore
    held modifiers, so chord mains must ride wVk."""
    import invincible.core.tool_executor as te

    calls = []

    def fake_key_input(api, *, vk=0, scan=0, flags=0):
        calls.append({"vk": vk, "scan": scan, "flags": flags})
        return ("key", len(calls))

    monkeypatch.setattr(te, "_computer_key_input", fake_key_input)
    monkeypatch.setattr(te, "_computer_send",
                        lambda api, inputs: None)
    te._computer_press({}, frozenset({"ctrl", "z"}))
    assert calls[0] == {"vk": 0x11, "scan": 0, "flags": 0}
    assert calls[1] == {"vk": 0x5A, "scan": 0, "flags": 0}
    assert calls[2] == {"vk": 0x5A, "scan": 0, "flags": 0x0002}
    assert calls[3] == {"vk": 0x11, "scan": 0, "flags": 0x0002}


def test_press_bare_key_keeps_unicode_path(monkeypatch):
    import invincible.core.tool_executor as te

    calls = []

    def fake_key_input(api, *, vk=0, scan=0, flags=0):
        calls.append({"vk": vk, "scan": scan, "flags": flags})
        return ("key", len(calls))

    monkeypatch.setattr(te, "_computer_key_input", fake_key_input)
    monkeypatch.setattr(te, "_computer_send",
                        lambda api, inputs: None)
    te._computer_press({}, frozenset({"z"}))
    assert len(calls) == 2
    assert all(call["vk"] == 0 and call["scan"] != 0
               for call in calls)


def test_resolve_delay_base_and_estimator():
    te = tool_executor
    base = 0.35
    assert te._resolve_delay(base, None, {"x": 1, "y": 2}) == base
    assert te._resolve_delay(base, lambda p: 0.9, {"x": 1, "y": 2}) == 0.9
    assert te._resolve_delay(base, lambda p: 0.1, {"x": 1, "y": 2}) == base
    assert te._resolve_delay(base, _raising_estimator,
                             {"x": 1, "y": 2}) == base
    assert te._resolve_delay("bogus", None, {"x": 1, "y": 2}) == 0.0
    assert te._resolve_delay(base, lambda p: float("nan"),
                             {"x": 1, "y": 2}) == base
    assert te._resolve_delay(base, lambda p: float("inf"),
                             {"x": 1, "y": 2}) == base


def _raising_estimator(point):
    raise RuntimeError("estimator died")


def test_overlay_delay_fn_covers_planned_flight(monkeypatch):
    from invincible.agent import overlay, runner

    vrect = (0, 0, 1920, 1080)
    monkeypatch.setattr(overlay, "virtual_screen", lambda: vrect)
    monkeypatch.setattr(runner, "_OVERLAY_POINTER_PX", None)
    delay_for = runner._overlay_delay_fn(False)
    dwell = delay_for({"x": 900, "y": 500})
    parked = overlay.pointer_home(vrect)
    flight = overlay.plan_flight(
        (float(parked[0]), float(parked[1])),
        overlay.grid_to_pixel(900, 500, vrect),
        (0.0, 0.0), 0.0, vrect, False)
    assert dwell >= float(flight["duration"])
    assert dwell <= runner._OVERLAY_DWELL_MAX_SECONDS
    assert dwell >= overlay.OVERLAY_PRE_ACTION_DWELL_SECONDS
    # Malformed points fall back to the base dwell, never raise.
    assert delay_for({"x": "bad", "y": 1}) == (
        overlay.OVERLAY_PRE_ACTION_DWELL_SECONDS)


def test_overlay_delay_fn_drag_sums_legs(monkeypatch):
    from invincible.agent import overlay, runner

    monkeypatch.setattr(overlay, "virtual_screen",
                        lambda: (0, 0, 1920, 1080))
    monkeypatch.setattr(runner, "_OVERLAY_POINTER_PX", None)
    delay_for = runner._overlay_delay_fn(True)
    first = delay_for({"x": 100, "y": 100})
    total = delay_for({"x": 900, "y": 800})
    assert total >= first
    assert total <= runner._OVERLAY_DRAG_DWELL_MAX_SECONDS


def test_resolve_drag_delay_covers_both_legs():
    te = tool_executor

    seen: list = []

    def fake_estimator(point):
        seen.append((point["x"], point["y"]))
        return 0.5 if len(seen) == 1 else 1.4

    start = {"x": 100, "y": 100}
    end = {"x": 900, "y": 800}
    dwell = te._resolve_drag_delay(0.35, fake_estimator, start, end)
    assert [tuple(p) for p in seen] == [(100, 100), (900, 800)]
    assert dwell == 1.4
    assert te._resolve_drag_delay(0.35, None, start, end) == 0.35
    assert te._resolve_drag_delay(
        0.35, _raising_estimator, start, end) == 0.35


def test_overlay_note_settled_resets_pointer(monkeypatch):
    from invincible.agent import runner

    monkeypatch.setattr(runner, "_OVERLAY_POINTER_PX", (10.0, 20.0))
    runner._overlay_note_settled()
    assert runner._OVERLAY_POINTER_PX is None


async def test_drag_act_estimates_both_legs(monkeypatch):
    """Item-1d: the drag branch estimates pointer->from then from->to so
    the single dwell covers the full two-leg glide."""
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})

    async def fake_resolve(target, *, on_point=None):
        point = {"x": target.get("x", target.get("x1",
                                                 target.get("x2"))),
                 "y": target.get("y", target.get("y1",
                                                 target.get("y2")))}
        if on_point is not None:
            on_point(point)
        return point, point

    monkeypatch.setattr(te, "_computer_resolve", fake_resolve)
    monkeypatch.setattr(te, "_computer_dwell",
                        lambda seconds: asyncio.sleep(0))
    monkeypatch.setattr(te, "_computer_drag",
                        lambda api, x1, y1, x2, y2: None)
    seen: list = []

    def fake_delay_for(point):
        seen.append((point["x"], point["y"]))
        return 0.5

    result = await te._computer_act(
        "drag", {"x1": 100, "y1": 100, "x2": 900, "y2": 800},
        pre_action_delay=0.35, delay_for=fake_delay_for)
    assert result["status"] == "ok"
    assert seen == [(100, 100), (900, 800)]


async def test_click_ok_exposes_effect_inputs(monkeypatch):
    """Item-4: the runner's click effect reads x/y/button off the ok
    result — the effect contract pins them here (core untouched)."""
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})

    async def fake_resolve(target, *, on_point=None):
        return {"x": 940, "y": 27}, {"x": 940, "y": 27}

    monkeypatch.setattr(te, "_computer_resolve", fake_resolve)

    async def fake_dwell(seconds):
        pass

    monkeypatch.setattr(te, "_computer_dwell", fake_dwell)
    monkeypatch.setattr(te, "_computer_click_at",
                        lambda api, x, y, button, double: None)
    result = await te._computer_act(
        "click", {"x": 940, "y": 27, "button": "right"})
    assert result["status"] == "ok"
    assert result["verified"] is False
    assert isinstance(result["x"], int)
    assert isinstance(result["y"], int)
    assert result["button"] == "right"


async def test_type_ok_keeps_feedback_shape(monkeypatch):
    """Item-4: type feedback rides the call args (text) — the ok
    result keeps chars/submit/version honesty markers intact."""
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})

    async def fake_dwell(seconds):
        pass

    async def fake_probe():
        return False

    monkeypatch.setattr(te, "_computer_dwell", fake_dwell)
    monkeypatch.setattr(te, "_focus_is_password", fake_probe)
    monkeypatch.setattr(te, "_computer_type",
                        lambda api, text: None)
    result = await te._computer_act(
        "type_text", {"text": "hello"})
    assert result["status"] == "ok"
    assert result["chars"] == 5
    assert result["submit"] is False
    assert result["verified"] is False


async def test_effect_inputs_absent_on_error(monkeypatch):
    """Item-4: error results carry no x/y — the runner skips the
    click effect by construction (stale ids stay errors, F13)."""
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")

    async def fake_look():
        return {"status": "look", "app": "Brave", "controls": []}

    monkeypatch.setattr(te, "_look_at_screen", fake_look)
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})
    result = await te._computer_act(
        "click", {"control": "C30", "button": "left"})
    assert result["status"] == "error"
    assert "x" not in result and "y" not in result


async def test_click_saves_and_warps_pointer(monkeypatch):
    """Item-4b (Bluey warp): save -> click -> restore, in order."""
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})
    order: list = []

    async def fake_resolve(target, *, on_point=None):
        return {"x": 5, "y": 6}, {"x": 5, "y": 6}

    async def fake_dwell(seconds):
        pass

    def fake_pointer(api):
        order.append("save")
        return (11, 22)

    def fake_click(api, x, y, button, double):
        order.append("click")

    def fake_warp(api, point):
        order.append(("warp", point))

    monkeypatch.setattr(te, "_computer_resolve", fake_resolve)
    monkeypatch.setattr(te, "_computer_dwell", fake_dwell)
    monkeypatch.setattr(te, "_computer_pointer", fake_pointer)
    monkeypatch.setattr(te, "_computer_click_at", fake_click)
    monkeypatch.setattr(te, "_computer_warp", fake_warp)
    result = await te._computer_act(
        "click", {"x": 5, "y": 6, "button": "left"})
    assert result["status"] == "ok"
    assert order == ["save", "click", ("warp", (11, 22))]


async def test_scroll_saves_and_warps_pointer(monkeypatch):
    """Item-4b: scroll hovers a point; the pointer comes back after."""
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})
    order: list = []

    async def fake_resolve(target, *, on_point=None):
        return {"x": 5, "y": 6}, {"x": 5, "y": 6}

    async def fake_dwell(seconds):
        pass

    def fake_pointer(api):
        order.append("save")
        return (11, 22)

    def fake_scroll(api, x, y, direction, amount):
        order.append("scroll")

    def fake_warp(api, point):
        order.append(("warp", point))

    monkeypatch.setattr(te, "_computer_resolve", fake_resolve)
    monkeypatch.setattr(te, "_computer_dwell", fake_dwell)
    monkeypatch.setattr(te, "_computer_pointer", fake_pointer)
    monkeypatch.setattr(te, "_computer_scroll_at", fake_scroll)
    monkeypatch.setattr(te, "_computer_warp", fake_warp)
    result = await te._computer_act(
        "scroll", {"x": 5, "y": 6, "direction": "down", "amount": 3})
    assert result["status"] == "ok"
    assert order == ["save", "scroll", ("warp", (11, 22))]


async def test_drag_saves_and_warps_pointer(monkeypatch):
    """Item-4b: a drag is one save/warp pair around both legs."""
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})
    order: list = []

    async def fake_resolve(target, *, on_point=None):
        return {"x": 100, "y": 100}, {"x": 100, "y": 100}

    async def fake_dwell(seconds):
        pass

    def fake_pointer(api):
        order.append("save")
        return (11, 22)

    def fake_drag(api, x1, y1, x2, y2):
        order.append("drag")

    def fake_warp(api, point):
        order.append(("warp", point))

    monkeypatch.setattr(te, "_computer_resolve", fake_resolve)
    monkeypatch.setattr(te, "_computer_dwell", fake_dwell)
    monkeypatch.setattr(te, "_computer_pointer", fake_pointer)
    monkeypatch.setattr(te, "_computer_drag", fake_drag)
    monkeypatch.setattr(te, "_computer_warp", fake_warp)
    result = await te._computer_act(
        "drag", {"x1": 100, "y1": 100, "x2": 900, "y2": 800})
    assert result["status"] == "ok"
    assert order == ["save", "drag", ("warp", (11, 22))]


async def test_focus_click_type_warps_pointer(monkeypatch):
    """type_text with x/y focuses first: the focus click gets the same
    save/warp treatment before any keystroke."""
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})
    order: list = []

    async def fake_resolve(target, *, on_point=None):
        return {"x": 5, "y": 6}, {"x": 5, "y": 6}

    async def fake_dwell(seconds):
        pass

    async def fake_probe():
        return False

    def fake_pointer(api):
        order.append("save")
        return (11, 22)

    def fake_click(api, x, y, button, double):
        order.append("focus")

    def fake_type(api, text):
        order.append("type")

    def fake_warp(api, point):
        order.append(("warp", point))

    monkeypatch.setattr(te, "_computer_resolve", fake_resolve)
    monkeypatch.setattr(te, "_computer_dwell", fake_dwell)
    monkeypatch.setattr(te, "_focus_is_password", fake_probe)
    monkeypatch.setattr(te, "_computer_pointer", fake_pointer)
    monkeypatch.setattr(te, "_computer_click_at", fake_click)
    monkeypatch.setattr(te, "_computer_type", fake_type)
    monkeypatch.setattr(te, "_computer_warp", fake_warp)
    result = await te._computer_act(
        "type_text", {"text": "hi", "x": 5, "y": 6})
    assert result["status"] == "ok"
    assert order == ["save", "focus", ("warp", (11, 22)), "type"]


async def test_bare_type_never_touches_pointer(monkeypatch):
    """Item-4b: bare type_text does not move the pointer — no save."""
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})

    async def fake_dwell(seconds):
        pass

    async def fake_probe():
        return False

    def boom(api):
        raise AssertionError("bare type must not save the pointer")

    monkeypatch.setattr(te, "_computer_dwell", fake_dwell)
    monkeypatch.setattr(te, "_focus_is_password", fake_probe)
    monkeypatch.setattr(te, "_computer_type", lambda api, text: None)
    monkeypatch.setattr(te, "_computer_pointer", boom)
    result = await te._computer_act("type_text", {"text": "hi"})
    assert result["status"] == "ok"


async def test_warp_failure_never_fails_the_job(monkeypatch):
    """Item-4b: the warp is best-effort — a raising restore still
    leaves the ok result (a dead save just skips the warp)."""
    import invincible.core.tool_executor as te

    monkeypatch.setattr(te.os, "name", "nt")
    monkeypatch.setattr(te, "_computer_input_api",
                        lambda: {"sentinel": True})

    async def fake_resolve(target, *, on_point=None):
        return {"x": 5, "y": 6}, {"x": 5, "y": 6}

    async def fake_dwell(seconds):
        pass

    def boom(api, point):
        raise RuntimeError("warp exploded")

    monkeypatch.setattr(te, "_computer_resolve", fake_resolve)
    monkeypatch.setattr(te, "_computer_dwell", fake_dwell)
    monkeypatch.setattr(te, "_computer_pointer", lambda api: (1, 2))
    monkeypatch.setattr(te, "_computer_click_at",
                        lambda api, x, y, button, double: None)
    monkeypatch.setattr(te, "_computer_warp", boom)
    result = await te._computer_act("click", {"x": 5, "y": 6})
    assert result["status"] == "ok"
    assert result["x"] == 5 and result["y"] == 6


def test_pointer_helpers_never_raise_on_broken_api():
    import invincible.core.tool_executor as te

    assert te._computer_pointer({}) is None
    assert te._computer_pointer({"ctypes": None,
                                 "user32": None}) is None
    te._computer_warp({}, (1, 2))
    te._computer_warp({"user32": None}, "junk")
    te._computer_warp({}, None)


def test_pointer_save_and_warp_byte_round_trip():
    """POINT is two 32-bit longs: the read/write pair is byte-exact."""
    import ctypes
    import struct

    import invincible.core.tool_executor as te

    seen: dict = {}

    class _User32:
        @staticmethod
        def GetCursorPos(buffer):
            ctypes.memmove(buffer, struct.pack("<ii", 12, 34), 8)
            return 1

        @staticmethod
        def SetCursorPos(x, y):
            seen["warp"] = (x, y)
            return 1

    api = {"ctypes": ctypes, "user32": _User32()}
    assert te._computer_pointer(api) == (12, 34)
    te._computer_warp(api, (12, 34))
    assert seen["warp"] == (12, 34)

    class _Dead:
        @staticmethod
        def GetCursorPos(buffer):
            return 0

    assert te._computer_pointer({"ctypes": ctypes,
                                 "user32": _Dead()}) is None
