# tests/test_overlay.py
"""F11 B1 head-cursor overlay: Bmax-head cursor on the paired PC.

Pure geometry/animation helpers are hermetic; the overlay handle
(Overlay) is best-effort by contract (never raises, dead overlay =
headless jobs); the runner announces resolved Pack 3 points around
each execute_job with a pre-action dwell only when an overlay is
alive. Live Win32 metrics run only on Windows and only read state -
the real mouse is never driven."""

import asyncio
import io
import json
import math
import os
import queue
import time

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


def test_shell_look_helpers():
    light, mid, deep = overlay.shell_shades()
    assert (light, mid, deep) == (
        overlay.OVERLAY_SHELL_LIGHT,
        overlay.OVERLAY_SHELL_MID,
        overlay.OVERLAY_SHELL_DEEP,
    )
    pts = overlay.teardrop_points(100.0, 100.0, 36.0, 36.0)
    assert len(pts) == 3
    assert min(y for _, y in pts) < 100.0 - 36.0 * 0.7
    assert overlay.teardrop_points(0, 0, 0, 0) == []
    assert overlay.teardrop_points("a", 0, 1, 1) == []


def test_overlay_wears_the_web_bmax_skin():
    """F20: the desktop head's skin IS the web face's default skin."""
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    js = (repo / "invincible" / "templates" / "static"
          / "robot-head.js").read_text(encoding="utf-8")
    # The three web DEFAULTS the overlay copies verbatim stay in sync.
    assert f'color: "{overlay.OVERLAY_SHELL_MID}"' in js
    assert f'trimColor: "{overlay.OVERLAY_HEAD_OUTLINE}"' in js
    assert f'screenColor: "{overlay.OVERLAY_EYE_FILL}"' in js
    # And the frozen values are the navy / slate / pink trio.
    assert overlay.OVERLAY_SHELL_MID == "#0d142a"
    assert overlay.OVERLAY_HEAD_OUTLINE == "#4d5b83"
    assert overlay.OVERLAY_EYE_FILL == "#ff5fa6"
    # The LED glow and the primary ring ride the LED colour; the
    # secondary ring rides the trim.
    assert overlay.OVERLAY_GLOW_FILL == overlay.OVERLAY_EYE_FILL
    assert overlay.OVERLAY_RING_LEFT_FILL == overlay.OVERLAY_EYE_FILL
    assert overlay.OVERLAY_RING_RIGHT_FILL == overlay.OVERLAY_HEAD_OUTLINE
    # The berry blue is fully retired - no stragglers in the module.
    src = (repo / "invincible" / "agent" / "overlay.py").read_text(
        encoding="utf-8")
    assert "BERRY" not in src and "berry" not in src


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
    lift = overlay.OVERLAY_DOCK_LIFT
    assert overlay.dock_corner(vrect, 72) == (1824, 984 - lift)
    assert overlay.dock_corner(vrect, 72, "bl") == (24, 984 - lift)
    assert overlay.dock_corner(vrect, 72, "tr") == (1824, 24)
    assert overlay.dock_corner(vrect, 72, "tl") == (24, 24)
    assert overlay.dock_corner(vrect, 72, "nope") == (1824, 984 - lift)
    # Negative-origin rect docks relative to its own corner.
    assert overlay.dock_corner((-1920, 0, 1920, 1080), 72) == (-96, 984 - lift)


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
                       pre_action_delay=0.0, delay_for=None):
        seen["on_point"] = on_point
        seen["delay"] = pre_action_delay
        seen["delay_for"] = delay_for
        return {"status": "ok", "action": name}

    monkeypatch.setattr(runner, "ensure_overlay", lambda: None)
    monkeypatch.setattr(tool_executor, "_computer_act", fake_act)
    out = await runner.execute_job(
        {"type": "click", "args": {"x": 10, "y": 20}})
    assert out == {"status": "ok", "action": "click"}
    assert seen["on_point"] is None
    assert seen["delay"] == 0.0
    assert seen["delay_for"] is None


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
                       pre_action_delay=0.0, delay_for=None):
        seen["on_point"] = on_point
        seen["delay"] = pre_action_delay
        seen["delay_for"] = delay_for
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
    assert callable(seen["delay_for"])
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


def test_timing_curves_easing_values():
    for curve in (overlay.TIMING_LAUNCH, overlay.TIMING_REDIRECT,
                  overlay.TIMING_STEADY):
        assert overlay.timing_value(curve, 0) == 0.0
        assert overlay.timing_value(curve, 1) == 1.0
        assert overlay.timing_value(curve, -0.5) == 0.0
        assert overlay.timing_value(curve, 1.5) == 1.0
        mid = overlay.timing_value(curve, 0.5)
        assert 0.0 < mid < 1.0
        assert overlay.timing_rate(curve, 0.5) >= 0.0
    # Launch starts gently (lags linear early), then outruns it by mid.
    assert overlay.timing_value(overlay.TIMING_LAUNCH, 0.25) < 0.25
    assert overlay.timing_value(overlay.TIMING_LAUNCH, 0.5) > 0.5
    # Redirect keeps incoming speed: runs ahead of the gentle launch early.
    assert (overlay.timing_value(overlay.TIMING_REDIRECT, 0.25)
            > overlay.timing_value(overlay.TIMING_LAUNCH, 0.25))
    # Monotonic: samples never step back.
    for curve in (overlay.TIMING_LAUNCH, overlay.TIMING_REDIRECT,
                  overlay.TIMING_STEADY):
        samples = [overlay.timing_value(curve, i / 20) for i in range(21)]
        assert all(b + 1e-9 >= a
                   for a, b in zip(samples, samples[1:], strict=False))


def test_flight_position_tangent_exact_land():
    from_xy, c1, c2, to_xy = (0.0, 0.0), (60.0, -20.0), (140.0, -20.0), \
        (200.0, 0.0)
    assert overlay.flight_position(from_xy, c1, c2, to_xy, 0.0) == from_xy
    assert overlay.flight_position(from_xy, c1, c2, to_xy, 1.0) == to_xy
    mid = overlay.flight_position(from_xy, c1, c2, to_xy, 0.5)
    assert mid[1] < 0  # bowed above the chord
    tangent = overlay.flight_tangent(from_xy, c1, c2, to_xy, 0.0)
    assert tangent[0] > 0  # leaves toward the goal
    assert overlay.flight_tangent("bad", c1, c2, to_xy, 0.5) == (0.0, 0.0)


def test_plan_flight_bow_direction_and_bounds():
    bounds = (0, 0, 1920, 1080)
    sideways = overlay.plan_flight((100.0, 500.0), (500.0, 500.0),
                                   (0.0, 0.0), 0.0, bounds)
    assert sideways["timing"] == "launch"
    assert sideways["c1"][1] < 500.0 and sideways["c2"][1] < 500.0
    left = overlay.plan_flight((200.0, 100.0), (200.0, 700.0),
                               (0.0, 0.0), 0.0, bounds)
    right = overlay.plan_flight((1700.0, 100.0), (1700.0, 700.0),
                                (0.0, 0.0), 0.0, bounds)
    assert left["c1"][0] > 200.0 and left["c2"][0] > 200.0
    assert right["c1"][0] < 1700.0 and right["c2"][0] < 1700.0
    # Negative-origin virtual rect keeps the middle math correct.
    neg = (-1920, 0, 1920, 1080)
    neg_plan = overlay.plan_flight((-1500.0, 100.0), (-1500.0, 700.0),
                                   (0.0, 0.0), 5.0, neg)
    assert neg_plan["c1"][0] > -1500.0  # toward ox+w/2 = -960
    assert neg_plan["start"] == 5.0
    assert overlay.plan_flight((1.0, 1.0), (1.2, 1.1), (0.0, 0.0),
                               0.0, bounds)["duration"] == 0.0


def test_plan_flight_velocity_carry_no_kink():
    bounds = (0, 0, 1920, 1080)
    fast = overlay.plan_flight((100.0, 100.0), (500.0, 100.0),
                               (800.0, 0.0), 0.0, bounds)
    assert fast["timing"] == "redirect"
    # c1 carries forward along the incoming velocity (no kink).
    assert fast["c1"][0] > 100.0 and abs(fast["c1"][1] - 100.0) < 1e-6
    first = overlay.flight_position(
        fast["from"], fast["c1"], fast["c2"], fast["to"],
        overlay.timing_value(overlay.TIMING_REDIRECT, 0.05))
    dx, dy = first[0] - 100.0, first[1] - 100.0
    assert dx > 0 and abs(math.degrees(math.atan2(dy, dx))) < 30.0
    slow = overlay.plan_flight((100.0, 100.0), (500.0, 100.0),
                               (0.0, 0.0), 0.0, bounds)
    assert slow["timing"] == "launch"
    # Carry capped at 0.6 * distance.
    huge = overlay.plan_flight((0.0, 0.0), (100.0, 0.0),
                               (10000.0, 0.0), 0.0, bounds)
    assert huge["c1"][0] - 0.0 <= 60.0 + 1e-6


def test_plan_flight_dragging_straight_steady_and_land():
    bounds = (0, 0, 1920, 1080)
    drag = overlay.plan_flight((0.0, 0.0), (400.0, 0.0),
                               (0.0, 0.0), 3.0, bounds, True)
    assert drag["timing"] == "steady"
    assert drag["c1"][1] == 0.0 and drag["c2"][1] == 0.0
    assert drag["duration"] >= overlay.plan_flight(
        (0.0, 0.0), (400.0, 0.0), (0.0, 0.0), 3.0, bounds)["duration"]
    # Sampling a flight lands exactly on the goal.
    for flight in (drag, overlay.plan_flight(
            (10.0, 20.0), (300.0, 400.0), (0.0, 0.0), 0.0, bounds)):
        end = overlay.flight_position(
            flight["from"], flight["c1"], flight["c2"], flight["to"], 1.0)
        assert end == flight["to"]
    assert abs(overlay.flight_time_remaining(drag, 3.0)
                 - drag["duration"]) < 1e-9
    assert overlay.flight_time_remaining(drag, 3.0 + drag["duration"]) == 0.0


def test_press_scale_and_hover_boundaries():
    assert overlay.press_scale(10.0, 0.0) == 1.0
    assert overlay.press_scale(-1.0, 0.0) == 1.0
    assert overlay.press_scale(0.42, 0.0) == 1.0
    dipped = overlay.press_scale(0.06, 0.0, 0.14)
    assert 1 - 0.14 - 1e-9 <= dipped < 1.0
    assert overlay.press_scale("bad", 0.0) == 1.0
    assert overlay.hover_offset(10.0, 0.0, is_home=True, settled=True) == 0.0
    assert overlay.hover_offset(10.0, 0.0, is_home=False,
                                settled=False) == 0.0
    assert overlay.hover_offset(0.2, 0.0, settled=True) == 0.0
    held = overlay.hover_offset(1.5, 0.0, settled=True)
    assert abs(held) <= 1.6 + 1e-9


def test_announce_dragging_ipc_backward_compat():
    handle = overlay.Overlay()
    stdin = _FakeStdin()
    handle._proc = _FakeProc(stdin)
    handle.announce({"x": 10, "y": 20})
    assert json.loads(stdin.lines[-1]) == {"type": "target", "x": 10, "y": 20}
    handle.announce({"x": 10, "y": 20}, dragging=True)
    assert json.loads(stdin.lines[-1]) == {
        "type": "target", "x": 10, "y": 20, "dragging": True}


async def test_execute_job_drag_announces_dragging(monkeypatch,
                                                   _clean_overlay_state):
    seen = {}

    async def fake_act(name, args, *, on_point=None,
                       pre_action_delay=0.0, delay_for=None):
        seen["on_point"] = on_point
        seen["delay_for"] = delay_for
        return {"status": "ok", "action": name}

    announced: list = []

    class _Live:
        alive = True

        def announce(self, point, dragging=False):
            announced.append((point, dragging))

        def settle(self):
            pass

    monkeypatch.setattr(runner, "ensure_overlay", lambda: _Live())
    monkeypatch.setattr(tool_executor, "_computer_act", fake_act)
    out = await runner.execute_job(
        {"type": "drag", "args": {"x": 1, "y": 2}})
    assert out == {"status": "ok", "action": "drag"}
    seen["on_point"]({"x": 1, "y": 2})
    assert announced == [({"x": 1, "y": 2}, True)]


def test_tip_alpha_docked_zero_flying_one():
    assert overlay.tip_alpha() == 0.0
    assert overlay.tip_alpha(flight_active=False, dragging=False,
                             to_target=False, settled=True,
                             press_age=10.0) == 0.0
    assert overlay.tip_alpha(flight_active=True, to_target=True,
                             settled=False, press_age=10.0) == 1.0
    assert overlay.tip_alpha(dragging=True, settled=True) == 1.0
    assert overlay.tip_alpha(flight_active=False, press_age="bad") == 0.0


def test_tip_alpha_press_window_then_hidden():
    assert overlay.tip_alpha(press_age=0.0) == 1.0
    assert overlay.tip_alpha(press_age=0.1) == 1.0
    assert overlay.tip_alpha(press_age=0.41) == 1.0
    assert overlay.tip_alpha(press_age=0.42) == 0.0
    assert overlay.tip_alpha(press_age=1.0) == 0.0
    assert overlay.tip_alpha(press_age=-0.1) == 0.0


def test_dock_lift_clears_taskbar():
    vrect = (0, 0, 1920, 1080)
    assert overlay.OVERLAY_DOCK_LIFT == 48
    dock = overlay.dock_corner(vrect, overlay.OVERLAY_HEAD_PX)
    bottom = dock[1] + overlay.OVERLAY_HEAD_PX
    assert bottom <= vrect[1] + vrect[3] - overlay.OVERLAY_DOCK_LIFT
    # Top corners unaffected by the lift.
    assert overlay.dock_corner(vrect, 72, "tl") == (24, 24)
    assert overlay.tip_alpha() == 0.0  # docked = round head, tip hidden


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


def test_ring_frame_expand_thin_and_expiry():
    assert overlay.ring_frame(0.0, 0.0, 0.7, 30) == (30.0, 3)
    mid = overlay.ring_frame(0.35, 0.0, 0.7, 30)
    assert mid is not None and mid[0] > 30.0 and mid[1] == 2
    late = overlay.ring_frame(0.6, 0.0, 0.7, 30)
    assert late is not None and late[1] == 1
    assert overlay.ring_frame(0.7, 0.0, 0.7, 30) is None
    assert overlay.ring_frame(-0.1, 0.0, 0.7, 30) is None
    assert overlay.ring_frame("bad", 0.0, 0.7, 30) is None
    assert overlay.ring_frame(0.1, 0.0, 0.0, 30) is None
    assert overlay.ring_frame(0.1, 0.0, 0.7, 0) is None


def test_effect_anchor_grid_to_pixel_and_junk():
    """Item-4b: click effects anchor at the clicked grid point — the
    pure seam the child draw uses, pinned off-device."""
    vrect = (100, 50, 1600, 900)
    assert overlay.effect_anchor((0, 0), vrect) == (
        overlay.grid_to_pixel(0, 0, vrect))
    assert overlay.effect_anchor((250, 750), vrect) == (
        overlay.grid_to_pixel(250, 750, vrect))
    for junk in (None, (), (1,), ("a", 1), (True, 1), (1, None),
                 (1, 1001), (float("nan"), 1), (1, float("inf"))):
        assert overlay.effect_anchor(junk, vrect) is None
    # Malformed vrect: None, never raises (a 4-char string unpacks as
    # four values — the grid math guard catches it).
    assert overlay.effect_anchor((1, 1), "junk") is None
    assert overlay.effect_anchor((1, 1), (0, 0)) is None


def test_glyph_frame_rise_ease_out_and_expiry():
    assert overlay.glyph_frame(0.0, 0.0, 0.9, 46.0) == 0.0
    mid = overlay.glyph_frame(0.45, 0.0, 0.9, 46.0)
    assert mid is not None and 0 < mid < 46.0
    # Ease-out: most of the climb happens early.
    assert overlay.glyph_frame(0.45, 0.0, 0.9, 46.0) > 46.0 / 2
    assert overlay.glyph_frame(0.9, 0.0, 0.9, 46.0) is None
    assert overlay.glyph_frame(-0.1, 0.0, 0.9, 46.0) is None
    assert overlay.glyph_frame("bad", 0.0, 0.9, 46.0) is None
    assert overlay.glyph_frame(0.1, 0.0, 0.0, 46.0) is None


def test_effect_glyph_chars_skip_space_and_cap():
    assert overlay.effect_glyph_chars("hi you") == ["h", "i", "y", "o",
                                                   "u"]
    assert overlay.effect_glyph_chars("  a\nb\t c ") == ["a", "b", "c"]
    assert overlay.effect_glyph_chars("") == []
    assert overlay.effect_glyph_chars(None) == []
    assert overlay.effect_glyph_chars(42) == []
    long_text = "z" * 200
    assert overlay.effect_glyph_chars(long_text) == ["z"] * 6
    assert len(overlay.effect_glyph_chars("abcdefghij")) == 6


def test_effect_serializes_click_and_type_to_child():
    handle = overlay.Overlay()
    stdin = _FakeStdin()
    handle._proc = _FakeProc(stdin)
    handle.effect("click", {"x": 250, "y": 750}, button="left")
    assert json.loads(stdin.lines[-1]) == {
        "type": "effect", "kind": "click", "x": 250, "y": 750,
        "button": "left"}
    handle.effect("click", {"x": 10, "y": 20}, button="right")
    assert json.loads(stdin.lines[-1])["button"] == "right"
    handle.effect("click", {"x": 10, "y": 20}, button="middle")
    assert json.loads(stdin.lines[-1])["button"] == "left"
    handle.effect("type", text="hi you")
    assert json.loads(stdin.lines[-1]) == {
        "type": "effect", "kind": "type", "text": "hiyou"}
    before = len(stdin.lines)
    for bad in ({"kind": "frobnicate"}, {"kind": "click"},
                {"kind": "click", "point": None}):
        handle.effect(bad.get("kind"), bad.get("point"),
                      text=bad.get("text", ""))
    handle.effect("click", {"x": "a", "y": 1})
    handle.effect("click", {"x": 5000, "y": 1})
    handle.effect("type", text="   ")
    handle.effect("type", text="")
    assert len(stdin.lines) == before


def test_effect_never_raises_when_dead():
    stillborn = overlay.Overlay()
    stillborn.effect("click", {"x": 1, "y": 2}, button="right")
    stillborn.effect("type", text="hello")
    stillborn.effect("bogus", {"x": 1, "y": 2})
    assert stillborn.alive is False


async def test_execute_job_click_ok_sends_effect(
        monkeypatch, _clean_overlay_state):
    effects: list = []
    settled: list = []

    class _Live:
        alive = True

        def announce(self, point, dragging=False):
            pass

        def settle(self):
            settled.append(True)

        def effect(self, kind, point=None, button="left", text=""):
            effects.append({"kind": kind, "point": point,
                            "button": button, "text": text})

    async def fake_act(name, args, *, on_point=None,
                       pre_action_delay=0.0, delay_for=None):
        return {"status": "ok", "action": "click", "button": "right",
                "double": False, "verified": False,
                "x": 111, "y": 222}

    monkeypatch.setattr(runner, "ensure_overlay", lambda: _Live())
    monkeypatch.setattr(tool_executor, "_computer_act", fake_act)
    out = await runner.execute_job(
        {"type": "click",
         "args": {"x": 111, "y": 222, "button": "right"}})
    assert out["status"] == "ok"
    assert out["verified"] is False
    assert effects == [{"kind": "click", "point": {"x": 111, "y": 222},
                        "button": "right", "text": ""}]
    assert settled == [True]


async def test_execute_job_type_ok_sends_text_effect(
        monkeypatch, _clean_overlay_state):
    effects: list = []

    class _Live:
        alive = True

        def announce(self, point, dragging=False):
            pass

        def settle(self):
            pass

        def effect(self, kind, point=None, button="left", text=""):
            effects.append({"kind": kind, "text": text})

    async def fake_act(name, args, *, on_point=None,
                       pre_action_delay=0.0, delay_for=None):
        return {"status": "ok", "action": "type_text", "chars": 5,
                "submit": False, "verified": False}

    monkeypatch.setattr(runner, "ensure_overlay", lambda: _Live())
    monkeypatch.setattr(tool_executor, "_computer_act", fake_act)
    out = await runner.execute_job(
        {"type": "type_text", "args": {"text": "hello"}})
    assert out["status"] == "ok"
    assert effects == [{"kind": "type", "text": "hello"}]


async def test_execute_job_error_sends_no_effect(
        monkeypatch, _clean_overlay_state):
    effects: list = []

    class _Live:
        alive = True

        def announce(self, point, dragging=False):
            pass

        def settle(self):
            pass

        def effect(self, kind, point=None, button="left", text=""):
            effects.append(kind)

    async def fake_act(name, args, *, on_point=None,
                       pre_action_delay=0.0, delay_for=None):
        return {"status": "error", "error": "stale id C30"}

    monkeypatch.setattr(runner, "ensure_overlay", lambda: _Live())
    monkeypatch.setattr(tool_executor, "_computer_act", fake_act)
    out = await runner.execute_job(
        {"type": "click", "args": {"control": "C30"}})
    assert out["status"] == "error"
    assert effects == []


async def test_execute_job_effect_failure_keeps_result(
        monkeypatch, _clean_overlay_state):
    settled: list = []

    class _Flaky:
        alive = True

        def announce(self, point, dragging=False):
            pass

        def settle(self):
            settled.append(True)

        def effect(self, kind, point=None, button="left", text=""):
            raise RuntimeError("overlay died mid-effect")

    async def fake_act(name, args, *, on_point=None,
                       pre_action_delay=0.0, delay_for=None):
        return {"status": "ok", "action": "click", "button": "left",
                "double": False, "verified": False,
                "x": 5, "y": 6}

    monkeypatch.setattr(runner, "ensure_overlay", lambda: _Flaky())
    monkeypatch.setattr(tool_executor, "_computer_act", fake_act)
    out = await runner.execute_job(
        {"type": "click", "args": {"x": 5, "y": 6}})
    assert out == {"status": "ok", "action": "click", "button": "left",
                   "double": False, "verified": False,
                   "x": 5, "y": 6}
    assert settled == [True]


def _choreo(*pts):
    state = overlay.choreo_state(1000.0)
    for pt in pts:
        assert overlay.choreo_point(state, *pt) is True
    return state


def test_choreo_queue_order_and_minimum_hold():
    state = _choreo((10, 20, "a"), (30, 40, "b"))
    action, item = overlay.choreo_step(state, 1000.0)
    assert (action, item) == ("launch", (10, 20, "a"))
    # Hold is flight 1.0 + minimum 2.2: nothing pops mid-hold.
    assert overlay.choreo_step(state, 1001.5) == (None, None)
    # Second point still queued in order after the hold expires.
    action, item = overlay.choreo_step(state, 1004.0)
    assert (action, item) == ("launch", (30, 40, "b"))


def test_choreo_point_clears_home_request():
    state = overlay.choreo_state(0.0)
    overlay.choreo_request_home(state)
    assert state["home_requested"] is True
    assert overlay.choreo_point(state, 5, 5) is True
    assert state["home_requested"] is False


def test_choreo_home_on_quiet_and_on_request():
    # Empty queue, hold expired, never spoke: home after 5.0s quiet.
    state = overlay.choreo_state(0.0)
    assert overlay.choreo_step(state, 5.0) == (None, None)
    assert overlay.choreo_step(state, 5.01) == ("dock", None)
    # Requested home: 1.2s of quiet suffices, not before.
    requested = overlay.choreo_state(0.0)
    overlay.choreo_request_home(requested)
    assert overlay.choreo_step(requested, 1.2) == (None, None)
    assert overlay.choreo_step(requested, 1.21) == ("dock", None)


def test_choreo_speaking_blocks_home_then_stamps_quiet():
    state = overlay.choreo_state(0.0)
    overlay.choreo_set_speaking(state, True, 10.0)
    assert overlay.choreo_step(state, 100.0) == (None, None)
    overlay.choreo_set_speaking(state, False, 100.0)
    assert overlay.choreo_step(state, 101.19) == (None, None)
    assert overlay.choreo_step(state, 101.21) == (None, None)
    # Requested home after speech stops: quiet counts from the stop.
    overlay.choreo_request_home(state)
    assert overlay.choreo_step(state, 101.19) == (None, None)
    assert overlay.choreo_step(state, 101.21) == ("dock", None)


def test_choreo_take_control_clears_queue():
    state = _choreo((1, 2, "a"), (3, 4, "b"))
    overlay.choreo_request_home(state)
    overlay.choreo_take_control(state)
    assert state["queue"] == []
    assert state["home_requested"] is False
    assert overlay.choreo_step(state, 9999.0) == ("dock", None)


def test_choreo_go_home_clears_flag_and_queue():
    state = _choreo((1, 2, "a"))
    overlay.choreo_request_home(state)
    overlay.choreo_go_home(state)
    assert state["queue"] == [] and state["home_requested"] is False


def test_choreo_label_sanitize_and_junk_never_raises():
    state = overlay.choreo_state(0.0)
    assert overlay.choreo_point(state, 7, 8, "line\none\ttwo") is True
    assert state["queue"] == [(7, 8, "line one two")]
    assert overlay.choreo_point(state, 9, 9, "z" * 200) is True
    assert state["queue"][-1][2].endswith("…")
    for bad_x, bad_y in ((None, 1), ("x", 1), (True, 1), (1, None),
                         (1, "y"), (-1, 5), (5, 1001),
                         (float("nan"), 1), (1, float("inf"))):
        assert overlay.choreo_point(state, bad_x, bad_y) is False
    assert overlay.choreo_step(None, 0.0) == (None, None)
    assert overlay.choreo_step("junk", "junk") == (None, None)
    overlay.choreo_request_home(None)
    overlay.choreo_take_control(None)
    overlay.choreo_go_home(None)
    overlay.choreo_set_speaking(None, True, 0.0)
    overlay.choreo_set_speaking({}, "yes", "junk")


def test_point_request_home_speaking_serialize_to_child():
    handle = overlay.Overlay()
    stdin = _FakeStdin()
    handle._proc = _FakeProc(stdin)
    handle.point({"x": 250, "y": 750}, label="Save")
    assert json.loads(stdin.lines[-1]) == {
        "type": "point", "x": 250, "y": 750, "label": "Save"}
    handle.point({"x": 10, "y": 20})
    assert json.loads(stdin.lines[-1]) == {"type": "point", "x": 10,
                                           "y": 20}
    handle.request_home()
    assert json.loads(stdin.lines[-1]) == {"type": "home"}
    handle.set_speaking(True)
    assert json.loads(stdin.lines[-1]) == {"type": "speaking",
                                           "on": True}
    handle.set_speaking("yes")
    assert json.loads(stdin.lines[-1]) == {"type": "speaking",
                                           "on": False}
    before = len(stdin.lines)
    for garbage in (None, "point", 42, [], {"x": "a", "y": 1},
                    {"x": True, "y": 1}, {"x": 1}, {"y": 1},
                    {"x": -1, "y": 50}, {"x": 50, "y": 1001},
                    {"x": float("inf"), "y": 1},
                    {"x": float("nan"), "y": 1}):
        handle.point(garbage)
    assert len(stdin.lines) == before


def test_choreography_senders_never_raise_when_dead():
    stillborn = overlay.Overlay()
    stillborn.point({"x": 1, "y": 2}, label="ok")
    stillborn.request_home()
    stillborn.set_speaking(True)
    assert stillborn.alive is False


async def test_execute_job_look_queues_near_point(
        monkeypatch, _clean_overlay_state):
    queued: list = []
    vrect = (0, 0, 1000, 1000)

    class _Live:
        alive = True

        def point(self, point, label=""):
            queued.append((point, label))

    async def fake_look():
        return {"status": "look", "app": "App",
                "controls": [
                    {"id": "C1", "kind": "button", "label": "Go",
                     "x": 100, "y": 200},
                    {"id": "C2", "kind": "link", "label": "Docs",
                     "x": 300, "y": 400}],
                "mouse": {"x": 100, "y": 200},
                "size": {"w": 1000, "h": 1000},
                "near": {"id": "C2", "kind": "link",
                         "label": "Docs"},
                "truncated": False}

    monkeypatch.setattr(runner, "ensure_overlay", lambda: _Live())
    monkeypatch.setattr(tool_executor, "_look_at_screen", fake_look)
    monkeypatch.setattr(overlay, "virtual_screen", lambda: vrect)
    monkeypatch.setattr(runner, "_OVERLAY_HEAD_PX", None)
    out = await runner.execute_job({"type": "look_at_screen",
                                    "args": {}})
    # Result shape untouched; the tour point rides alongside.
    assert out["status"] == "look"
    assert out["near"] == {"id": "C2", "kind": "link",
                           "label": "Docs"}
    assert queued == [({"x": 300, "y": 400}, "Docs")]
    # Item-4b: the queued tour point seeds the dwell head estimate.
    assert overlay.grid_to_pixel(300, 400, vrect) == (
        runner._OVERLAY_HEAD_PX)


async def test_execute_job_look_without_near_skips_queue(
        monkeypatch, _clean_overlay_state):
    queued: list = []

    class _Live:
        alive = True

        def point(self, point, label=""):
            queued.append((point, label))

    async def fake_look():
        return {"status": "look", "app": "App", "controls": [],
                "mouse": {"x": 1, "y": 1},
                "size": {"w": 10, "h": 10}, "near": None,
                "truncated": False}

    monkeypatch.setattr(runner, "ensure_overlay", lambda: _Live())
    monkeypatch.setattr(tool_executor, "_look_at_screen", fake_look)
    out = await runner.execute_job({"type": "look_at_screen",
                                    "args": {}})
    assert out["status"] == "look"
    assert queued == []


async def test_execute_job_look_headless_result_intact(
        monkeypatch, _clean_overlay_state):
    async def fake_look():
        return {"status": "look", "app": "App", "controls": [],
                "mouse": {"x": 0, "y": 0},
                "size": {"w": 1, "h": 1}, "near": None,
                "truncated": False}

    monkeypatch.setattr(runner, "ensure_overlay", lambda: None)
    monkeypatch.setattr(tool_executor, "_look_at_screen", fake_look)
    out = await runner.execute_job({"type": "look_at_screen",
                                    "args": {}})
    assert out["status"] == "look"
    assert set(out) == {"status", "app", "controls", "mouse", "size",
                        "near", "truncated"}


# --- Item-7 face: pure resolvers (hermetic) -------------------------


def test_face_kind_persistent_expiry_and_junk():
    now = 500.0
    # Persistent kinds hold until replaced (their until is not a timer).
    assert overlay.face_kind({"kind": "working", "until": None}, now) \
        == "working"
    assert overlay.face_kind({"kind": "searching", "until": None}, now) \
        == "searching"
    assert overlay.face_kind({"kind": "working", "until": now - 99.0},
                             now) == "working"
    # Bursts expire into idle.
    assert overlay.face_kind({"kind": "happy", "until": now + 0.5},
                             now) == "happy"
    assert overlay.face_kind({"kind": "happy", "until": now}, now) == "idle"
    assert overlay.face_kind({"kind": "error", "until": now + 0.5},
                             now) == "error"
    assert overlay.face_kind({"kind": "error", "until": now - 0.5},
                             now) == "idle"
    # A burst replaces a persistent kind outright (single slot: one job
    # at a time, so nothing can be squeezed into a stale "working").
    assert overlay.face_kind({"kind": "happy", "until": now + 1.0},
                             now, quiet_since=0.0) == "happy"
    # idle and sleeping are never taken from the wire.
    assert overlay.face_kind({"kind": "idle", "until": None}, now) == "idle"
    assert overlay.face_kind({"kind": "sleeping", "until": None},
                             now) == "idle"
    # Junk in any argument reads as idle, never raises.
    for junk in (None, {}, "working", 5, [], {"kind": None},
                 {"kind": "bogus"}, {"kind": True}, {"kind": "happy"},
                 {"kind": "happy", "until": "soon"},
                 {"kind": "happy", "until": float("nan")},
                 {"kind": "error", "until": None}):
        assert overlay.face_kind(junk, now) == "idle"
    for junk_now in (None, "soon", float("nan"), True):
        assert overlay.face_kind({"kind": "happy", "until": now + 1.0},
                                 junk_now) == "idle"


def test_face_kind_sleeping_after_quiet_window():
    now = 9000.0
    window = overlay.OVERLAY_FACE_SLEEP_AFTER_SECONDS
    # No quiet stamp: unknown is not quiet.
    assert overlay.face_kind(None, now) == "idle"
    assert overlay.face_kind({}, now) == "idle"
    # Under the window idle, at and over it sleeping.
    assert overlay.face_kind(None, now,
                             quiet_since=now - window + 0.01) == "idle"
    assert overlay.face_kind(None, now,
                             quiet_since=now - window) == "sleeping"
    assert overlay.face_kind(None, now,
                             quiet_since=now - window * 2.0) == "sleeping"
    # An active face always beats a stale quiet stamp.
    assert overlay.face_kind({"kind": "working", "until": None}, now,
                             quiet_since=0.0) == "working"
    # Junk stamps never sleep.
    for junk in (None, "soon", float("nan"), float("inf"), True):
        assert overlay.face_kind(None, now, quiet_since=junk) == "idle"


def test_face_geometry_idle_matches_today():
    geom = overlay.face_geometry("idle", 0.0)
    assert geom["eyes"] == "oval"
    assert geom["track"] is True
    assert geom["sweep"] == 0.0
    assert geom["stroke"] == overlay.OVERLAY_FACE_EYE_STROKE
    # The idle box IS the frozen eye constants, so the pre-Item-7 draw
    # (ecx = hx + side*EYE_DX + ex, ecy = hy + EYE_DY + ey) is reproduced
    # exactly - every F16/Item-1..4b pin and look stays put.
    assert (geom["dx"], geom["dy"]) == (float(overlay.OVERLAY_EYE_DX),
                                        float(overlay.OVERLAY_EYE_DY))
    assert geom["rx"] == geom["ry"] == float(overlay.OVERLAY_EYE_R)
    for hx, hy, ex, ey in ((100.0, 100.0, 0, 0), (52.0, 30.0, 3, -4)):
        for side in (-1, 1):
            before = (hx + side * overlay.OVERLAY_EYE_DX + ex,
                      hy + overlay.OVERLAY_EYE_DY + ey)
            after = (hx + side * geom["dx"] + geom["sweep"] + ex,
                     hy + geom["dy"] + ey)
            assert after == before
    # A blink only squashes ry, never rx/dx, and a full blink is a slit.
    shut = overlay.face_geometry("idle", 0.0, blink=1.0)
    half = overlay.face_geometry("idle", 0.0, blink=0.5)
    assert shut["rx"] == geom["rx"] and shut["dx"] == geom["dx"]
    assert geom["ry"] > half["ry"] > shut["ry"] > 0.0
    assert shut["ry"] <= 1.2
    # A junk blink reads as open.
    assert overlay.face_geometry("idle", 0.0, blink="soon")["ry"] \
        == geom["ry"]
    assert overlay.face_geometry("idle", 0.0,
                                 blink=float("nan"))["ry"] == geom["ry"]


def test_face_geometry_states_shapes():
    now = 7.5
    radius = float(overlay.OVERLAY_EYE_R)
    # working: still oval and tracking, but squinted and lifted.
    work = overlay.face_geometry("working", now)
    assert work["eyes"] == "oval" and work["track"] is True
    assert work["ry"] < work["rx"] < radius
    assert work["dy"] < overlay.OVERLAY_EYE_DY
    # searching: the eyes sweep both ways, bounded by the gain.
    sweeps = [overlay.face_geometry("searching", i / 40.0)["sweep"]
              for i in range(240)]
    assert max(sweeps) > 0.0 > min(sweeps)
    assert max(abs(s) for s in sweeps) <= overlay.OVERLAY_FACE_SWEEP_PX
    # happy / sleeping / error change the eye SHAPE and drop tracking;
    # a blink never touches a closed-eye shape.
    happy = overlay.face_geometry("happy", now)
    assert (happy["eyes"], happy["track"], happy["sweep"]) == \
        ("arc_top", False, 0.0)
    assert overlay.face_geometry("happy", now, blink=1.0)["ry"] \
        == overlay.OVERLAY_FACE_ARC_EYE_RY
    sleep = overlay.face_geometry("sleeping", now)
    assert (sleep["eyes"], sleep["track"]) == ("arc_bottom", False)
    assert sleep["dy"] > overlay.OVERLAY_EYE_DY
    err = overlay.face_geometry("error", now)
    assert (err["eyes"], err["track"]) == ("x", False)
    assert err["rx"] == err["ry"] == overlay.OVERLAY_FACE_X_EYE_R
    # Every state stays finite and inside the head, junk times included.
    for kind in overlay.FACE_STATES:
        for moment in (0.0, 1.5, 999.0, float("nan"), "soon", None):
            geom = overlay.face_geometry(kind, moment)
            for key in ("dx", "dy", "rx", "ry", "sweep", "stroke"):
                assert isinstance(geom[key], float)
                assert math.isfinite(geom[key])
            head = overlay.OVERLAY_HEAD_PX
            assert abs(geom["dx"]) + geom["rx"] + abs(geom["sweep"]) < head
            assert abs(geom["dy"]) + geom["ry"] < head / 2.0
    # Unknown kinds fall back to the resting face.
    assert overlay.face_geometry("bogus", now)["eyes"] == "oval"
    assert overlay.face_geometry(None, None)["eyes"] == "oval"


def test_face_blink_period_and_bounds():
    period = overlay.OVERLAY_FACE_BLINK_PERIOD_SECONDS
    window = overlay.OVERLAY_FACE_BLINK_SECONDS
    mid = window / 2.0
    # Open between blinks, shut in the middle of the window.
    assert overlay.face_blink(0.0) == 0.0
    assert overlay.face_blink(window) == 0.0
    assert overlay.face_blink(window + 0.5) == 0.0
    assert overlay.face_blink(mid) > 0.9
    # Eased both ways - never a step at the window edges.
    assert 0.0 < overlay.face_blink(mid / 2.0) < 0.75
    # Periodic and bounded.
    assert overlay.face_blink(period + mid) == pytest.approx(
        overlay.face_blink(mid), abs=1e-9)
    assert all(0.0 <= overlay.face_blink(i / 30.0) <= 1.0
               for i in range(int(period * 30) + 1))
    # Junk times read as open, never raise.
    for junk in ("soon", None, True, float("nan"), float("inf")):
        assert overlay.face_blink(junk) == 0.0


def test_face_apply_wire_slot_and_junk():
    now = 100.0
    # Bursts stamp their end on the child's clock.
    assert overlay.face_apply("happy", now) == {
        "kind": "happy",
        "until": now + overlay.OVERLAY_FACE_HAPPY_SECONDS}
    assert overlay.face_apply("error", now) == {
        "kind": "error",
        "until": now + overlay.OVERLAY_FACE_ERROR_SECONDS}
    # Persistent kinds hold until the next face message.
    assert overlay.face_apply("working", now) == {"kind": "working",
                                                  "until": None}
    assert overlay.face_apply("searching", now) == {"kind": "searching",
                                                    "until": None}
    # idle clears with an expired stamp (reads idle immediately).
    assert overlay.face_apply("idle", now) == {"kind": "idle",
                                               "until": now}
    # Unknown, derived and junk states never move the slot.
    for junk in (None, "sleeping", "bogus", True, 5, [], {}, ""):
        assert overlay.face_apply(junk, now) is None
    for junk_now in (None, "soon", float("nan"), float("inf"), True):
        assert overlay.face_apply("happy", junk_now) is None
    # ... and what it produces is exactly what face_kind reads back.
    for wire in overlay.FACE_WIRE_STATES:
        slot = overlay.face_apply(wire, now)
        assert slot is not None
        assert overlay.face_kind(slot, now) == wire


def test_face_wire_states_and_inbound_allowlist():
    # The wire vocabulary is the full set minus the child-derived one.
    assert set(overlay.FACE_WIRE_STATES) == set(overlay.FACE_STATES) - {
        "sleeping"}
    # The child's stdin allowlist must carry "face": _stdin_reader drops
    # unknown types silently, so a missing entry is a silent no-op.
    assert "face" in overlay._INBOUND_TYPES
    assert set(overlay._INBOUND_TYPES) == {
        "target", "point", "home", "speaking", "effect", "settle", "quit",
        "face"}


def test_face_sender_serializes_and_junk_never_raises():
    stdin = _FakeStdin()
    handle = overlay.Overlay()
    handle._proc = _FakeProc(stdin)
    for state in overlay.FACE_WIRE_STATES:
        handle.face(state)
    assert [json.loads(line) for line in stdin.lines] == [
        {"type": "face", "state": state}
        for state in overlay.FACE_WIRE_STATES]
    before = len(stdin.lines)
    for junk in (None, "sleeping", "bogus", True, 5, [], {}):
        handle.face(junk)
    assert len(stdin.lines) == before
    # A dead handle stays silent and never raises.
    stillborn = overlay.Overlay()
    stillborn.face("working")
    assert stillborn.alive is False


# --- Item-7 face: runner wiring (hermetic) --------------------------


class _FaceRecorder:
    """Overlay stand-in recording faces.

    Deliberately has NO point/announce/effect methods: the code under
    test must swallow their absence exactly like an Item-1..4b-era stub,
    which is the same isolation a dead overlay gets in production.
    """

    def __init__(self):
        self.calls = []
        self.settled = 0

    @property
    def alive(self):
        return True

    def face(self, state):
        self.calls.append(state)

    def settle(self):
        self.settled += 1


async def test_execute_job_face_sequence_ok_and_error(
        monkeypatch, _clean_overlay_state):
    recorder = _FaceRecorder()
    monkeypatch.setattr(runner, "_OVERLAY", recorder)

    async def fake_run(command, timeout):
        return {"status": "ok", "stdout": "", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(tool_executor, "_run_command", fake_run)
    out = await runner.execute_job({"type": "execute_bash",
                                    "args": {"command": "echo hi"}})
    assert out["status"] == "ok"
    assert recorder.calls == ["working", "happy"]

    async def fake_error(command, timeout):
        return {"status": "error", "error": "boom"}

    monkeypatch.setattr(tool_executor, "_run_command", fake_error)
    recorder.calls.clear()
    out = await runner.execute_job({"type": "execute_bash",
                                    "args": {"command": "echo hi"}})
    assert out["status"] == "error"
    assert recorder.calls == ["working", "error"]

    async def fake_blocked(command, timeout):
        raise tool_executor.ToolBlocked("denied")

    monkeypatch.setattr(tool_executor, "_run_command", fake_blocked)
    recorder.calls.clear()
    out = await runner.execute_job({"type": "execute_bash",
                                    "args": {"command": "echo hi"}})
    assert out["status"] == "blocked"
    assert recorder.calls == ["working", "error"]

    async def fake_unavailable(command, timeout):
        return {"status": "unavailable", "error": "not windows"}

    monkeypatch.setattr(tool_executor, "_run_command", fake_unavailable)
    recorder.calls.clear()
    out = await runner.execute_job({"type": "execute_bash",
                                    "args": {"command": "echo hi"}})
    assert out["status"] == "unavailable"
    assert recorder.calls == ["working", "error"]


async def test_execute_job_face_look_searching_and_raise_idle(
        monkeypatch, _clean_overlay_state):
    recorder = _FaceRecorder()
    monkeypatch.setattr(runner, "_OVERLAY", recorder)
    # The look branch calls ensure_overlay itself for its tour point;
    # a face never needs that (and tests must not spawn a real head).
    monkeypatch.setattr(runner, "ensure_overlay", lambda: None)

    async def fake_look():
        return {"status": "look", "app": "App", "controls": [],
                "mouse": {"x": 0, "y": 0}, "size": {"w": 1, "h": 1},
                "near": None, "truncated": False}

    monkeypatch.setattr(tool_executor, "_look_at_screen", fake_look)
    out = await runner.execute_job({"type": "look_at_screen", "args": {}})
    assert out["status"] == "look"
    assert recorder.calls == ["searching", "happy"]

    # A raising job must not leave "working" stuck on the head.
    async def fake_boom(command, timeout):
        raise RuntimeError("boom")

    monkeypatch.setattr(tool_executor, "_run_command", fake_boom)
    recorder.calls.clear()
    with pytest.raises(RuntimeError):
        await runner.execute_job({"type": "execute_bash",
                                  "args": {"command": "echo hi"}})
    assert recorder.calls == ["working", "idle"]

    # Cancellation clears it the same way, then re-raises.
    async def fake_cancel(command, timeout):
        raise asyncio.CancelledError()

    monkeypatch.setattr(tool_executor, "_run_command", fake_cancel)
    recorder.calls.clear()
    with pytest.raises(asyncio.CancelledError):
        await runner.execute_job({"type": "execute_bash",
                                  "args": {"command": "echo hi"}})
    assert recorder.calls == ["working", "idle"]


async def test_execute_job_face_no_live_overlay_silent(
        monkeypatch, _clean_overlay_state):
    async def fake_run(command, timeout):
        return {"status": "ok", "stdout": "", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(tool_executor, "_run_command", fake_run)
    monkeypatch.setattr(runner, "ensure_overlay", lambda: None)

    # No handle at all (headless / off-Windows / kill-switched).
    assert runner._OVERLAY is None
    out = await runner.execute_job({"type": "execute_bash",
                                    "args": {"command": "echo hi"}})
    assert out["status"] == "ok"

    class _Dead:
        alive = False

        def face(self, state):  # pragma: no cover - must never run
            raise AssertionError("a dead overlay must not be painted")

    monkeypatch.setattr(runner, "_OVERLAY", _Dead())
    out = await runner.execute_job({"type": "execute_bash",
                                    "args": {"command": "echo hi"}})
    assert out["status"] == "ok"

    # A stub so old it has no .face at all: swallowed, job unaffected.
    class _Bare:
        alive = True

    monkeypatch.setattr(runner, "_OVERLAY", _Bare())
    out = await runner.execute_job({"type": "execute_bash",
                                    "args": {"command": "echo hi"}})
    assert out["status"] == "ok"


async def test_execute_job_happy_burst_replaces_working(
        monkeypatch, _clean_overlay_state):
    recorder = _FaceRecorder()
    monkeypatch.setattr(runner, "_OVERLAY", recorder)

    async def fake_run(command, timeout):
        return {"status": "ok", "stdout": "", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(tool_executor, "_run_command", fake_run)
    for _ in range(2):
        await runner.execute_job({"type": "execute_bash",
                                  "args": {"command": "echo hi"}})
    # One slot, no queueing: the next job's working simply replaces the
    # burst the last job left - a burst never outranks a live job.
    assert recorder.calls == ["working", "happy", "working", "happy"]
    # The rule itself (claim honesty): only a named non-failure status
    # earns a happy burst.
    assert runner._result_face({"status": "ok"}) == "happy"
    assert runner._result_face({"status": "written"}) == "happy"
    assert runner._result_face({"status": "look"}) == "happy"
    assert runner._result_face({"status": "error"}) == "error"
    assert runner._result_face({"status": "blocked"}) == "error"
    assert runner._result_face({"status": "unavailable"}) == "error"
    assert runner._result_face({"no": "status"}) == "error"
    assert runner._result_face(None) == "error"
    assert runner._result_face("ok") == "error"
    # ... and the start rule: look jobs search, everything else works.
    assert runner._job_face("look_at_screen") == "searching"
    assert runner._job_face("execute_bash") == "working"
    assert runner._job_face(None) == "working"


def test_handoff_face_happy_when_live(monkeypatch, _clean_overlay_state):
    recorder = _FaceRecorder()
    monkeypatch.setattr(runner, "ensure_overlay", lambda: recorder)
    runner.handle_handoff({"task": "move the report"})
    assert recorder.calls == ["happy"]
    assert recorder.settled == 1

    class _Dead:
        alive = False

        def settle(self):  # pragma: no cover - must never run
            raise AssertionError("a dead overlay must not be touched")

    monkeypatch.setattr(runner, "ensure_overlay", lambda: _Dead())
    runner.handle_handoff({"task": "x"})
    runner.handle_handoff("junk")
    runner.handle_handoff(None)


# --- Item-8: A1 spawn guard + lifecycle hygiene ----------------------


def test_stdin_reader_eof_enqueues_quit(monkeypatch):
    fake = io.StringIO('{"type": "target", "x": 10, "y": 20}\n'
                       '{"type": "bogus"}\n')
    monkeypatch.setattr(overlay.sys, "stdin", fake)
    inbox = queue.Queue(maxsize=16)
    overlay._stdin_reader(inbox)
    # Known verb passes through; unknown verbs are dropped; EOF (the
    # designed parent-death path) always ends with a quit.
    assert inbox.get_nowait() == {"type": "target", "x": 10, "y": 20}
    assert inbox.get_nowait() == {"type": "quit"}


def test_stdin_reader_eof_quit_survives_full_inbox(monkeypatch):
    monkeypatch.setattr(overlay.sys, "stdin", io.StringIO(""))
    inbox = queue.Queue(maxsize=4)
    for _ in range(4):
        inbox.put_nowait({"type": "point", "x": 1, "y": 1})
    overlay._stdin_reader(inbox)
    drained = []
    while not inbox.empty():
        drained.append(inbox.get_nowait())
    # The quit made room by dropping the oldest unread message: a
    # silently dropped quit would leave an orphan head running.
    assert {"type": "quit"} in drained
    assert len(drained) == 4


def test_destroy_window_never_raises():
    class _Root:
        def __init__(self, boom_destroy=False, boom_quit=False):
            self.destroyed = 0
            self.quit_calls = 0
            self._boom_destroy = boom_destroy
            self._boom_quit = boom_quit

        def destroy(self):
            self.destroyed += 1
            if self._boom_destroy:
                raise RuntimeError("tcl gone")

        def quit(self):
            self.quit_calls += 1
            if self._boom_quit:
                raise RuntimeError("tcl gone")

    # Both teardown calls are attempted even when destroy throws, and
    # a blowing pair never raises out of the quit path.
    root = _Root(boom_destroy=True)
    overlay._destroy_window(root)
    assert (root.destroyed, root.quit_calls) == (1, 1)
    both = _Root(boom_destroy=True, boom_quit=True)
    overlay._destroy_window(both)
    assert (both.destroyed, both.quit_calls) == (1, 1)


async def test_job_paths_never_attempt_a_real_spawn(
        overlay_spawn_guard, monkeypatch, _clean_overlay_state):
    """Representative job calls that reach ensure_overlay in production
    stay headless under the conftest guard: zero spawn attempts."""

    async def fake_look():
        return {"status": "look", "controls": [], "truncated": False}

    monkeypatch.setattr(tool_executor, "_look_at_screen", fake_look)
    look = await runner.execute_job({"type": "look_at_screen", "args": {}})
    assert look["status"] == "look"

    refused = await runner.execute_job(
        {"type": "click", "args": {"x": 9999, "y": 0}})
    assert refused["status"] in ("error", "blocked", "unavailable")
    assert overlay_spawn_guard == []


async def test_guard_refuses_real_spawn_when_enabled(
        overlay_spawn_guard, monkeypatch, _clean_overlay_state):
    """With the overlay enabled, the guard refuses each real spawn and
    ensure_overlay degrades to headless with the retry cooldown
    stamped - no process is ever created."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delenv("INVINCIBLE_OVERLAY", raising=False)

    async def fake_look():
        return {"status": "look", "controls": [], "truncated": False}

    monkeypatch.setattr(tool_executor, "_look_at_screen", fake_look)
    look = await runner.execute_job({"type": "look_at_screen", "args": {}})
    assert look["status"] == "look"
    assert len(overlay_spawn_guard) == 1  # the one refused attempt
    assert time.monotonic() < runner._OVERLAY_RETRY_AT

    refused = await runner.execute_job(
        {"type": "click", "args": {"x": 9999, "y": 0}})
    assert refused["status"] in ("error", "blocked", "unavailable")
    # The cooldown blocks the follow-up attempt entirely.
    assert len(overlay_spawn_guard) == 1
    overlay_spawn_guard.clear()


def test_ensure_overlay_reuses_a_live_handle(
        monkeypatch, _clean_overlay_state):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delenv("INVINCIBLE_OVERLAY", raising=False)
    calls = []

    class _Handle:
        alive = True

        def start(self):  # pragma: no cover - must never run
            calls.append(1)
            return True

    handle = _Handle()
    monkeypatch.setattr(runner, "_OVERLAY", handle)
    assert runner.ensure_overlay() is handle
    assert calls == []


def test_ensure_overlay_cooldown_survives_fast_child_death(
        monkeypatch, _clean_overlay_state):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delenv("INVINCIBLE_OVERLAY", raising=False)
    calls = []

    class _Proc:
        code = None

        def poll(self):
            return self.code

    def start(self):
        calls.append(1)
        self._proc = _Proc()
        return True

    monkeypatch.setattr(overlay.Overlay, "start", start)
    handle = runner.ensure_overlay()
    assert handle is not None
    assert handle.alive is True
    assert time.monotonic() < runner._OVERLAY_RETRY_AT
    # A child that dies right after a good spawn must not respawn on
    # the next job: the cooldown holds.
    assert handle._proc is not None
    handle._proc.code = 1
    assert runner.ensure_overlay() is None
    assert len(calls) == 1


def test_stop_overlay_clears_the_retry_cooldown(
        monkeypatch, _clean_overlay_state):
    monkeypatch.setattr(runner, "_OVERLAY_RETRY_AT", time.monotonic() + 60)
    stopped = []

    class _Handle:
        alive = False

        def stop(self):
            stopped.append(1)

    monkeypatch.setattr(runner, "_OVERLAY", _Handle())
    runner.stop_overlay()
    # An intentional stop can bring a fresh head up immediately (WS
    # teardown -> poll fallback), so it clears the cooldown stamp.
    assert stopped == [1]
    assert runner._OVERLAY_RETRY_AT == 0.0
    assert runner._OVERLAY is None
