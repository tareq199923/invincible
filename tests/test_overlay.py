# tests/test_overlay.py
"""F11 B1 head-cursor overlay: Bmax-head cursor on the paired PC.

Pure geometry/animation helpers are hermetic; the overlay handle
(Overlay) is best-effort by contract (never raises, dead overlay =
headless jobs); the runner announces resolved Pack 3 points around
each execute_job with a pre-action dwell only when an overlay is
alive. Live Win32 metrics run only on Windows and only read state -
the real mouse is never driven."""

import asyncio
import json
import math
import os

import pytest

from invincible.agent import overlay, runner
from invincible.core import harness_tools, tool_executor


def test_grid_to_pixel_primary():
    vrect = (0, 0, 1920, 1080)
    assert overlay.grid_to_pixel(0, 0, vrect) == (0, 0)
    assert overlay.grid_to_pixel(1000, 1000, vrect) == (1920, 1080)
    assert overlay.grid_to_pixel(500, 500, vrect) == (960, 540)


def test_grid_to_pixel_multi_monitor_origin():
    # Secondary monitor left of primary: the origin offset applies.
    vrect = (-1920, 0, 1920, 1080)
    assert overlay.grid_to_pixel(0, 0, vrect) == (-1920, 0)
    assert overlay.grid_to_pixel(1000, 0, vrect) == (0, 0)
    assert overlay.grid_to_pixel(500, 500, vrect) == (-960, 540)


def test_grid_to_pixel_clamp_and_degenerate():
    vrect = (10, 20, 1000, 500)
    assert overlay.grid_to_pixel(-50, 2000, vrect) == (10, 520)
    assert overlay.grid_to_pixel("bad", None, vrect) == (10, 20)
    assert overlay.grid_to_pixel(500, 500, (0, 0, 0, 0)) == (0, 0)
    assert overlay.grid_to_pixel(500, 500, (5, 6, -3, 10)) == (5, 6)


def test_pixel_grid_round_trip():
    for vrect in ((0, 0, 1920, 1080), (-1920, -200, 2560, 1440),
                  (0, 0, 3840, 2160)):
        for point in ((0, 0), (1, 1), (250, 750), (500, 500),
                      (999, 999), (1000, 1000), (333, 666)):
            pixels = overlay.grid_to_pixel(*point, vrect)
            assert overlay.pixel_to_grid(*pixels, vrect) == point


def test_head_radii_idle_hover_squish():
    assert overlay.head_radii(36) == (36.0, 36.0)
    grown = overlay.head_radii(36, hovering=True)
    assert grown[0] > 36.0 and grown[0] == grown[1]
    squished = overlay.head_radii(36, hovering=True, squished=True)
    assert squished[0] > 36.0 > squished[1]
    assert overlay.head_radii(0) == (0.0, 0.0)
    assert overlay.head_radii("bad") == (0.0, 0.0)


def test_trail_push_caps_and_ignores_bad_points():
    trail = []
    for i in range(20):
        trail = overlay.trail_push(trail, float(i), 0.0)
    assert len(trail) == overlay.OVERLAY_TRAIL_POINTS
    assert trail[-1] == (19.0, 0.0)
    same = overlay.trail_push(trail, float("nan"), 0.0)
    assert same == trail
    assert overlay.trail_push("bad", 1.0, 2.0)[-1] == (1.0, 2.0)


def test_bubble_text_for_coords_and_malformed():
    assert overlay.bubble_text_for(500, 200) == "\u2316 500,200"
    assert overlay.bubble_text_for(0, 0) == "\u2316 0,0"
    for bad in ((True, 1), (1, False), ("x", 1), (1, None),
                (float("nan"), 1), (-1, 1), (1, 2000)):
        assert overlay.bubble_text_for(*bad) is None


def test_ease_waypoints_shape():
    path = overlay.ease_waypoints(0.0, 0.0, 90.0, 30.0, 9)
    assert len(path) == 9
    assert path[-1] == (90.0, 30.0)
    assert path[0] != (0.0, 0.0)
    for x, y in path:
        assert 0.0 <= x <= 90.0
        assert 0.0 <= y <= 30.0
    # Ease-out: consecutive gaps shrink (soft landing).
    gaps = [math.dist(path[i], path[i + 1])
            for i in range(len(path) - 1)]
    for before, after in zip(gaps, gaps[1:], strict=False):
        assert after <= before + 1e-9
    assert overlay.ease_waypoints(1.0, 2.0, 3.0, 4.0, 1) == [(3.0, 4.0)]
    assert overlay.ease_waypoints(1.0, 2.0, 3.0, 4.0, 0) == [(3.0, 4.0)]


def test_bowed_waypoints_arc_and_land():
    path = overlay.bowed_waypoints(0.0, 0.0, 200.0, 0.0, 9)
    assert len(path) == 9
    assert path[-1] == (200.0, 0.0)
    # Sideways moves bow upward (negative y), never below the line.
    assert any(y < 0 for _, y in path[:-1])
    assert all(y <= 0.001 for _, y in path)
    # Bounded bow: at most 8% / 48px.
    assert min(y for _, y in path) >= -16.001
    # Ease-out still holds: gaps shrink toward landing.
    gaps = [math.dist(path[i], path[i + 1])
            for i in range(len(path) - 1)]
    for before, after in zip(gaps, gaps[1:], strict=False):
        assert after <= before + 6.0
    assert overlay.bowed_waypoints(1.0, 2.0, 1.0, 2.0, 3) == [
        (1.0, 2.0)] * 3


def test_berry_look_helpers():
    light, mid, deep = overlay.berry_shades()
    assert (light, mid, deep) == (
        overlay.OVERLAY_BERRY_LIGHT,
        overlay.OVERLAY_BERRY_MID,
        overlay.OVERLAY_BERRY_DEEP,
    )
    pts = overlay.teardrop_points(100.0, 100.0, 36.0, 36.0)
    assert len(pts) == 3
    assert min(y for _, y in pts) < 100.0 - 36.0 * 0.7
    assert overlay.teardrop_points(0, 0, 0, 0) == []
    assert overlay.teardrop_points("a", 0, 1, 1) == []


def test_eye_offset_toward_cursor_and_clamp():
    assert overlay.eye_offset(100, 100, 103, 104, 9) == (3, 4)
    assert overlay.eye_offset(100, 100, 100, 100, 9) == (0, 0)
    assert overlay.eye_offset(100, 100, 500, 100, 9) == (9, 0)
    far = overlay.eye_offset(0, 0, 300, 400, 10)
    assert math.dist((0, 0), far) <= 10 + 1e-9
    # Same heading as the raw vector (3-4-5 scaled to radius 10).
    assert far == (6, 8)
    assert overlay.eye_offset(0, 0, 50, 50, 0) == (0, 0)
    assert overlay.eye_offset(0, 0, 50, 50, -3) == (0, 0)


def test_dock_corner_all_corners():
    vrect = (0, 0, 1920, 1080)
    assert overlay.dock_corner(vrect, 72) == (1824, 984)
    assert overlay.dock_corner(vrect, 72, "bl") == (24, 984)
    assert overlay.dock_corner(vrect, 72, "tr") == (1824, 24)
    assert overlay.dock_corner(vrect, 72, "tl") == (24, 24)
    assert overlay.dock_corner(vrect, 72, "nope") == (1824, 984)
    # Negative-origin rect docks relative to its own corner.
    assert overlay.dock_corner((-1920, 0, 1920, 1080), 72) == (-96, 984)


def test_overlay_handle_never_raises_when_dead():
    stillborn = overlay.Overlay()
    assert stillborn.alive is False
    stillborn.announce({"x": 250, "y": 750})
    stillborn.settle()
    stillborn.stop()
    stillborn.stop()
    for garbage in (None, "target", 42, [], {"x": "a", "y": 1},
                    {"x": True, "y": 1}, {"x": 1}, {"y": 1},
                    {"x": -1, "y": 50}, {"x": 50, "y": 1001},
                    {"x": float("inf"), "y": 1},
                    {"x": float("nan"), "y": 1}):
        stillborn.announce(garbage)
    assert stillborn.alive is False


def test_overlay_start_noop_off_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    handle = overlay.Overlay()
    assert handle.start() is False
    assert handle.alive is False


class _FakeStdin:
    def __init__(self, fail=False):
        self.lines: list[str] = []
        self.fail = fail

    def write(self, text):
        if self.fail:
            raise BrokenPipeError("child is gone")
        self.lines.append(text)

    def flush(self):
        pass


class _FakeProc:
    def __init__(self, stdin):
        self.stdin = stdin
        self.killed = False
        self.waited = False

    def poll(self):
        return None if not self.killed else 1

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0


def test_announce_serializes_grid_point_to_child():
    handle = overlay.Overlay()
    stdin = _FakeStdin()
    handle._proc = _FakeProc(stdin)
    handle.announce({"x": 250, "y": 750})
    assert [json.loads(line) for line in stdin.lines] == [
        {"type": "target", "x": 250, "y": 750}]
    handle.settle()
    assert json.loads(stdin.lines[-1]) == {"type": "settle"}
    before = len(stdin.lines)
    handle.announce({"x": 5000, "y": 1})
    assert len(stdin.lines) == before


def test_send_failure_never_raises_and_marks_dead():
    handle = overlay.Overlay()
    proc = _FakeProc(_FakeStdin(fail=True))
    handle._proc = proc
    handle.announce({"x": 1, "y": 2})
    handle.settle()
    assert proc.killed is True


def test_stop_says_quit_then_waits():
    handle = overlay.Overlay()
    stdin = _FakeStdin()
    proc = _FakeProc(stdin)
    handle._proc = proc
    handle.stop()
    assert json.loads(stdin.lines[-1]) == {"type": "quit"}
    assert proc.waited is True
    assert handle.alive is False


def test_overlay_enabled_default_on_kill_switch(monkeypatch):
    from invincible.core.settings import settings

    monkeypatch.delenv("INVINCIBLE_OVERLAY", raising=False)
    assert settings.overlay_enabled() is True
    for off in ("0", "off", "false", "OFF", "False"):
        monkeypatch.setenv("INVINCIBLE_OVERLAY", off)
        assert settings.overlay_enabled() is False
    for on in ("1", "true", "yes", ""):
        monkeypatch.setenv("INVINCIBLE_OVERLAY", on)
        assert settings.overlay_enabled() is True


@pytest.fixture
def _clean_overlay_state(monkeypatch):
    monkeypatch.setattr(runner, "_OVERLAY", None)
    monkeypatch.setattr(runner, "_OVERLAY_RETRY_AT", 0.0)
    yield
    monkeypatch.setattr(runner, "_OVERLAY", None)
    monkeypatch.setattr(runner, "_OVERLAY_RETRY_AT", 0.0)


def test_ensure_overlay_none_off_windows(
        monkeypatch, _clean_overlay_state):
    monkeypatch.setattr(os, "name", "posix")
    assert runner.ensure_overlay() is None


def test_ensure_overlay_none_when_killed(
        monkeypatch, _clean_overlay_state):
    monkeypatch.setenv("INVINCIBLE_OVERLAY", "0")
    assert runner.ensure_overlay() is None


async def test_execute_job_computer_headless_no_dwell(
        monkeypatch, _clean_overlay_state):
    seen = {}

    async def fake_act(name, args, *, on_point=None,
                       pre_action_delay=0.0):
        seen["on_point"] = on_point
        seen["delay"] = pre_action_delay
        return {"status": "ok", "action": name}

    monkeypatch.setattr(runner, "ensure_overlay", lambda: None)
    monkeypatch.setattr(tool_executor, "_computer_act", fake_act)
    out = await runner.execute_job(
        {"type": "click", "args": {"x": 10, "y": 20}})
    assert out == {"status": "ok", "action": "click"}
    assert seen["on_point"] is None
    assert seen["delay"] == 0.0


async def test_execute_job_computer_announces_and_settles(
        monkeypatch, _clean_overlay_state):
    announced: list = []
    settled: list = []

    class _Live:
        alive = True

        def announce(self, point):
            announced.append(point)

        def settle(self):
            settled.append(True)

    seen = {}

    async def fake_act(name, args, *, on_point=None,
                       pre_action_delay=0.0):
        seen["on_point"] = on_point
        seen["delay"] = pre_action_delay
        if on_point is not None:
            on_point({"x": 111, "y": 222})
        return {"status": "ok", "action": name}

    live = _Live()
    monkeypatch.setattr(runner, "ensure_overlay", lambda: live)
    monkeypatch.setattr(tool_executor, "_computer_act", fake_act)
    out = await runner.execute_job(
        {"type": "click", "args": {"x": 111, "y": 222}})
    assert out == {"status": "ok", "action": "click"}
    assert seen["on_point"] == live.announce
    assert seen["delay"] == overlay.OVERLAY_PRE_ACTION_DWELL_SECONDS
    assert announced == [{"x": 111, "y": 222}]
    assert settled == [True]


async def test_resolve_announces_bare_point_and_survives_hook():
    announced: list = []

    def raising(point):
        raise RuntimeError("overlay died mid-glide")

    point, echo = await tool_executor._computer_resolve(
        {"x": 100, "y": 200}, on_point=raising)
    assert point == {"x": 100, "y": 200}
    assert echo == {"x": 100, "y": 200}
    point, _ = await tool_executor._computer_resolve(
        {"x": 100, "y": 200}, on_point=announced.append)
    assert announced == [{"x": 100, "y": 200}]


async def test_resolve_announces_control_target(monkeypatch):
    announced: list = []

    async def fake_look():
        return {"status": "look", "controls": [
            {"id": "C3", "kind": "button", "label": "OK",
             "x": 300, "y": 400}]}

    monkeypatch.setattr(tool_executor, "_look_at_screen", fake_look)
    point, echo = await tool_executor._computer_resolve(
        {"control": "C3"}, on_point=announced.append)
    assert point == {"x": 300, "y": 400}
    assert echo["control"] == "C3"
    assert announced == [{"x": 300, "y": 400}]


async def test_computer_dwell_skips_zero_and_sleeps_delay(monkeypatch):
    slept: list = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await tool_executor._computer_dwell(0)
    await tool_executor._computer_dwell(0.0)
    await tool_executor._computer_dwell("bogus")
    assert slept == []
    await tool_executor._computer_dwell(0.35)
    assert slept == [0.35]


def test_no_registry_surface_for_overlay():
    assert "overlay" not in harness_tools.agent_job_names()
    assert "overlay" not in harness_tools.agent_only_names()
    assert harness_tools.get_tool("overlay") is None


@pytest.mark.skipif(os.name != "nt", reason="Win32 metrics only")
def test_overlay_live_metrics_on_windows():
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    rect = overlay.virtual_screen()
    assert len(rect) == 4
    assert all(isinstance(v, int) for v in rect)
    assert rect[2] > 0 and rect[3] > 0
    # Constants verified live: the virtual metrics reproduce exactly.
    assert rect[0] == user32.GetSystemMetrics(76)
    assert rect[1] == user32.GetSystemMetrics(77)
    assert rect[2] == user32.GetSystemMetrics(78)
    assert rect[3] == user32.GetSystemMetrics(79)
    x, y = overlay.cursor_pos()
    assert isinstance(x, int) and isinstance(y, int)
