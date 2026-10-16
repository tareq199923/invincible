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


def _near_items(*specs):
    """(id, kind, label, rect) rows for look_near pins."""
    return [(cid, kind, label, rect)
            for cid, kind, label, rect in specs]


def test_look_near_smallest_area_wins():
    outer = ("C1", "group", "Panel", (100, 100, 400, 300))
    inner = ("C2", "button", "Save", (150, 150, 80, 30))
    assert tool_executor.look_near(
        (190, 165), _near_items(outer, inner)) == {
        "id": "C2", "kind": "button", "label": "Save"}
    # C order breaks exact-area ties (stable, frozen).
    twin_a = ("C1", "button", "A", (100, 100, 80, 30))
    twin_b = ("C2", "button", "B", (100, 100, 80, 30))
    assert tool_executor.look_near(
        (140, 115), _near_items(twin_a, twin_b))["id"] == "C1"


def test_look_near_inset_tolerance_and_nearest():
    box = ("C1", "button", "OK", (100, 100, 80, 30))
    # 5px past the edge still counts as a contain (Bluey -6 inset).
    assert tool_executor.look_near(
        (185, 115), _near_items(box))["id"] == "C1"
    # Outside tolerance: nearest edge within 60px wins.
    assert tool_executor.look_near(
        (230, 115), _near_items(box))["id"] == "C1"
    # Past 60px: omitted, never an error.
    assert tool_executor.look_near(
        (500, 500), _near_items(box)) is None
    assert tool_executor.look_near((10, 10), []) is None
    assert tool_executor.look_near(("x", "y"), _near_items(box)) is None


def test_look_near_skips_junk_rects():
    junk = ("C1", "button", "Junk", (0, 0, 0, 0))
    good = ("C2", "button", "Go", (100, 100, 80, 30))
    assert tool_executor.look_near(
        (140, 115), _near_items(junk, good))["id"] == "C2"


def test_look_mouse_hint_shape_and_sanitize():
    hint = tool_executor.look_mouse_hint(
        233, 689, {"id": "C1", "kind": "button", "label": "Save"})
    assert "@233,689" in hint
    assert 'on C1 "Save"' in hint
    assert "this" in hint and "\n" not in hint
    bare = tool_executor.look_mouse_hint(1, 1, None)
    assert "@1,1" in bare and "on " not in bare
    assert "\n" not in bare
    # AX newlines collapse to one line; long labels cap like previews.
    messy = tool_executor.look_mouse_hint(
        5, 5, {"id": "C9", "kind": "text",
               "label": "line one\nline\ttwo"})
    assert "\n" not in messy and "line one line two" in messy
    long_hint = tool_executor.look_mouse_hint(
        5, 5, {"id": "C9", "kind": "text", "label": "z" * 200})
    assert "\n" not in long_hint and long_hint.endswith("pointer.")


def test_look_assemble_hint_near_and_truncation():
    vrect = (0, 0, 1000, 1000)
    kept = [{"kind": "button", "label": "Save",
             "rect": (100, 100, 200, 60)}]
    out = tool_executor._look_assemble(
        "App", kept, vrect, (200, 130), False)
    assert out["controls"] == [
        {"id": "C1", "kind": "button", "label": "Save",
         "x": 200, "y": 130}]
    assert out["mouse"] == {"x": 200, "y": 130}
    assert out["near"] == {"id": "C1", "kind": "button",
                           "label": "Save"}
    assert "@200,130" in out["mouse_hint"]
    assert 'on C1 "Save"' in out["mouse_hint"]
    assert out["truncated"] is False
    flagged = tool_executor._look_assemble(
        "App", kept, vrect, (200, 130), True)
    assert flagged["truncated"] is True
    assert flagged["near"]["id"] == "C1"


def test_look_assemble_missing_mouse_omits_hint():
    vrect = (0, 0, 1000, 1000)
    kept = [{"kind": "button", "label": "Save",
             "rect": (100, 100, 200, 60)}]
    out = tool_executor._look_assemble("App", kept, vrect, None, False)
    assert "mouse_hint" not in out
    assert out["near"] is None
    assert out["truncated"] is False
    # Legacy mouse-field shape kept; controls untouched.
    assert set(out["mouse"]) == {"x", "y"}
    assert [c["id"] for c in out["controls"]] == ["C1"]


def test_look_space_parity_scaled_negative_origin():
    # 150%-scale-style virtual desktop with a secondary monitor left
    # of primary: near (pixel space) and look_grid_virtual (grid
    # space) must agree on the same vector, and the overlay must fold
    # it back identically (F15 cannot drift).
    from invincible.agent import overlay

    vrect = (-1920, 0, 3840, 1080)
    rect = (-960, 270, 100, 60)
    kept = [{"kind": "button", "label": "Go", "rect": rect}]
    grid = tool_executor.look_grid_virtual(rect, vrect)
    assert grid == (263, 277)
    mouse_px = (-960 + 50, 270 + 30)
    out = tool_executor._look_assemble(
        "App", kept, vrect, mouse_px, False)
    assert out["near"]["id"] == "C1"
    assert out["mouse"] == {"x": grid[0], "y": grid[1]}
    assert f"@{grid[0]},{grid[1]}" in out["mouse_hint"]
    px, py = overlay.grid_to_pixel(*grid, vrect)
    assert overlay.pixel_to_grid(px, py, vrect) == grid


def test_look_result_shape_additive_drift_guard():
    # Shape-drift tripwire for the Item-3 payload: exactly these keys,
    # no more — a new key must extend this pin deliberately.
    out = tool_executor._look_assemble(
        "App",
        [{"kind": "button", "label": "OK", "rect": (10, 10, 40, 20)}],
        (0, 0, 1000, 1000), (30, 20), False)
    assert set(out) == {"app", "controls", "mouse", "size", "near",
                        "truncated", "mouse_hint"}
    assert set(out["near"]) == {"id", "kind", "label"}
    quiet = tool_executor._look_assemble(
        "App",
        [{"kind": "button", "label": "OK", "rect": (10, 10, 40, 20)}],
        (0, 0, 1000, 1000), None, False)
    assert set(quiet) == {"app", "controls", "mouse", "size", "near",
                          "truncated"}
