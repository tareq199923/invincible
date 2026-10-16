# tests/test_look_at_screen.py
"""F09 look_at_screen: frontmost-window controls via Windows UI
Automation. Pure helpers are hermetic; the COM walk runs isolated in
a child process (raw UIA calls can fail fatal, which must never take
down the agent loop); live shape runs only on Windows."""

import os

import pytest

from invincible.core import harness_tools, tool_executor


def test_look_kind_name_known_and_fallback():
    assert tool_executor.look_kind_name(50000) == "button"
    assert tool_executor.look_kind_name(50004) == "text field"
    assert tool_executor.look_kind_name(50005) == "link"
    assert tool_executor.look_kind_name(50019) == "tab item"
    assert tool_executor.look_kind_name(999999) == "control"


def test_look_grid_center_and_clamp():
    assert tool_executor.look_grid((100, 100, 200, 200), 1000, 1000) == (
        200, 200)
    assert tool_executor.look_grid((-500, -500, 200, 200), 1000, 1000) == (
        0, 0)
    assert tool_executor.look_grid((900, 900, 500, 500), 1000, 1000) == (
        1000, 1000)
    assert tool_executor.look_grid((0, 0, 10, 10), 0, 0) == (0, 0)


def test_look_grid_virtual_matches_overlay_round_trip():
    from invincible.agent import overlay

    # Secondary monitor left of primary: origin offset applies, and
    # the overlay inverts the mapping exactly.
    vrect = (-1920, 0, 3840, 1080)
    grid = tool_executor.look_grid_virtual((-960, 270, 100, 60), vrect)
    assert grid == (263, 277)
    px, py = overlay.grid_to_pixel(*grid, vrect)
    back = overlay.pixel_to_grid(px, py, vrect)
    assert back == grid
    # Primary-only vrect matches the legacy helper.
    single = (0, 0, 1920, 1080)
    assert tool_executor.look_grid_virtual(
        (100, 100, 200, 200), single) == tool_executor.look_grid(
        (100, 100, 200, 200), 1920, 1080)
    assert tool_executor.look_grid_virtual((0, 0, 10, 10), (0, 0, 0, 0)) == (
        0, 0)


async def test_look_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    out = await tool_executor._look_at_screen()
    assert out["status"] == "unavailable"
    assert "paired Windows machine" in out["reason"]


def test_look_at_screen_registered_agent_only():
    tool = harness_tools.get_tool("look_at_screen")
    assert tool.read_only is True
    assert tool.needs_approval is False
    assert tool.mcp is True
    assert tool.webchat is True
    assert tool.agent_job == "look_at_screen"
    assert tool.server_executable is False
    assert "look_at_screen" in harness_tools.agent_only_names()
    assert "look_at_screen" not in harness_tools.explore_names()
    for mode in ("plan", "manual", "auto"):
        assert "look_at_screen" in harness_tools.webchat_names_for_mode(
            mode)


@pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation only")
async def test_look_live_shape_on_windows():
    from invincible.agent import runner

    out = await runner.execute_job({"type": "look_at_screen", "args": {}})
    assert out["status"] == "look"
    assert isinstance(out.get("app"), (str, type(None)))
    assert isinstance(out["controls"], list)
    assert isinstance(out["mouse"], dict)
    assert isinstance(out["size"], dict)
    for control in out["controls"][:5]:
        assert set(control) == {"id", "kind", "label", "x", "y"}
        assert 0 <= control["x"] <= 1000
        assert 0 <= control["y"] <= 1000


async def test_webchat_streams_look_result(
    client, byok_env, router_setter, monkeypatch
):
    import json as _json

    import httpx

    from tests.test_webchat_agent import agent_user

    def fn_call(call_id, name, args):
        return {"id": call_id, "type": "function",
                "function": {"name": name,
                             "arguments": _json.dumps(args)}}

    script = [
        {"id": "cmpl-tools", "model": "w1-model",
         "choices": [{"message": {"role": "assistant", "content": None,
                                  "tool_calls": [
                                      fn_call("c1", "look_at_screen",
                                              {})]}}]},
        {"id": "cmpl-text", "model": "w1-model",
         "choices": [{"message": {"role": "assistant",
                                  "content": "saw it"}}]},
    ]
    state = {"i": 0}

    def handler(request):
        resp = script[min(state["i"], len(script) - 1)]
        state["i"] += 1
        return httpx.Response(200, json=resp)

    router_setter({"w1.example.com": handler})

    async def fake_look(executor):
        return {"status": "look", "app": "App",
                "controls": [{"id": "C1", "kind": "button",
                              "label": "OK", "x": 500, "y": 500}],
                "mouse": {"x": 1, "y": 1},
                "size": {"w": 100, "h": 100}}

    import invincible.core.webchat_agent as webchat_agent

    monkeypatch.setattr(webchat_agent, "_run_look_at_screen", fake_look)
    await agent_user(client, "look@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-look", "message": "look around",
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
    assert results[0]["name"] == "look_at_screen"
    assert "C1" in results[0]["preview"]
