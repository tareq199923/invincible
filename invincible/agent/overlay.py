# invincible/agent/overlay.py
"""B1 head-cursor overlay (F11): a Bmax-head cursor on the paired PC.

A transparent, click-through, always-on-top window draws an original
soft-white rounded head with two ink eyes (no Disney likeness - the
canvas echo of the ``.baymax-dot`` CSS concept in
``invincible/templates/static/app.css``). It glides to each Pack 3
target point BEFORE the action executes (so the user watches Bmax
"do" the click/type), idles docked at a screen corner, and works
through a queued point tour (``point()``) on Bluey-style holds when
it is otherwise free — tools never wait on the head. Its eyes
track the real mouse at ~30Hz.

Stdlib only (tkinter + ctypes + threading + subprocess) - no new
dependencies. Failure isolation is a requirement: the window runs in a
CHILD process (the ``_look_main`` subprocess pattern from
``core/tool_executor.py`` is the precedent) fed by best-effort stdin
JSON-lines; the parent never blocks on it and every send is wrapped,
so a dead overlay means jobs continue headless. The runner announces
resolved grid points through ``on_point`` callbacks - the overlay adds
ZERO registry surface (no MCP/webchat tool, no golden/drift churn).

Multi-monitor: geometry resolves against the virtual screen
(per-monitor origin offsets), never assuming primary-only like F09's
``look_grid`` does.
"""
import contextlib
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time

OVERLAY_GRID_MAX = 1000

# Look: Bluey-like berry head (original shades, no copied art).
OVERLAY_HEAD_PX = 72
OVERLAY_HEAD_FILL = "#7d9bff"
OVERLAY_HEAD_OUTLINE = "#ffffff"
OVERLAY_BERRY_LIGHT = "#b9cbff"
OVERLAY_BERRY_MID = "#7d9bff"
OVERLAY_BERRY_DEEP = "#5568d8"
OVERLAY_SHINE_FILL = "#ffffff"
OVERLAY_GLOW_FILL = "#7d9bff"
OVERLAY_GLOW = True
OVERLAY_EYE_FILL = "#20242b"
OVERLAY_EYE_DX = 14
OVERLAY_EYE_DY = -6
OVERLAY_EYE_R = 7
OVERLAY_EYE_TRACK_R = 9

# Motion: ~30Hz cursor poll, ease-out glide, choreographed point
# queue + quiet-based homing, pre-action dwell (the dwell lives in
# the runner - it only waits when an overlay is alive, so headless
# jobs pay zero added latency).
OVERLAY_POLL_SECONDS = 1.0 / 30.0
# Settle/idle glides + bubble timing only — target glides carry their
# own Bluey plan_flight duration (see _overlay_delay_fn in runner.py,
# which stretches the pre-action dwell to cover it).
OVERLAY_GLIDE_SECONDS = 0.30
OVERLAY_PRE_ACTION_DWELL_SECONDS = 0.35
# Point-queue choreography (Item-2, Bluey RealtimeHost port): every
# queued tour point holds 1.0s flight allowance + 2.2s minimum; the
# head goes home 1.2s after a request once quiet, else after 5.0s
# quiet. These replace the old fixed idle-settle delay — homing now
# flows through choreo_step so a queued tour is never cut short.
OVERLAY_POINT_MIN_HOLD_SECONDS = 2.2
OVERLAY_POINT_FLIGHT_SECONDS = 1.0
OVERLAY_IDLE_HOME_SECONDS = 5.0
OVERLAY_HOME_QUIET_SECONDS = 1.2
OVERLAY_POINT_LABEL_CAP = 60
OVERLAY_DOCK_MARGIN = 24
OVERLAY_DOCK_CORNER = "br"
OVERLAY_DOCK_LIFT = 48
OVERLAY_TRANSPARENT_KEY = "magenta"

# F16 Bluey states (child-side drawing only — no job-logic touch,
# no added headless latency): press squish on target arrival, hover
# grow while gliding, fading trail, coordinate bubble near the head.
OVERLAY_SQUISH_SECONDS = 0.18
OVERLAY_SQUISH_RX = 1.08
OVERLAY_SQUISH_RY = 0.82
OVERLAY_HOVER_SCALE = 1.08
OVERLAY_TRAIL_POINTS = 8
OVERLAY_BUBBLE_SECONDS = 1.5

# Item-4 fly-click feedback (child-side drawing only — same contract:
# no job-logic touch, dead overlay swallows everything). Bluey
# CursorOverlay softLand/clickEffect/typedEffect/bubble, same numbers.
OVERLAY_LAND_RING_PX = 30
OVERLAY_LAND_RING_SECONDS = 0.7
OVERLAY_CLICK_RING_PX = 38
OVERLAY_CLICK_RING_SECONDS = 0.65
OVERLAY_CLICK_BUBBLE_SECONDS = 1.4
OVERLAY_GLYPH_RISE_PX = 46.0
OVERLAY_GLYPH_LIFE_SECONDS = 0.9
OVERLAY_GLYPH_CAP = 6
OVERLAY_EFFECT_TEXT_CAP = 24
OVERLAY_PRESS_LAND = 0.05
OVERLAY_PRESS_CLICK = 0.14
OVERLAY_PRESS_TYPE = 0.035
OVERLAY_RING_LEFT_FILL = OVERLAY_BERRY_MID
OVERLAY_RING_RIGHT_FILL = OVERLAY_BERRY_DEEP

# Win32 GetSystemMetrics indices (verified live on Windows - never
# trusted from memory, per the F09 vtable lesson).
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x20
_WS_EX_LAYERED = 0x80000


def _clamp_grid(value) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if isinstance(value, bool) or not math.isfinite(number):
        return 0
    return min(max(int(round(number)), 0), OVERLAY_GRID_MAX)


def grid_to_pixel(gx, gy, vrect: tuple) -> tuple[int, int]:
    """0-1000 grid point to virtual-screen pixels (pure, hermetic).

    ``vrect`` is ``(origin_x, origin_y, width, height)`` - the origin
    keeps secondary monitors correct. Inverse of ``pixel_to_grid``;
    the conceptual inverse of ``look_grid`` in core/tool_executor.py
    (which folds a pixel rect onto the grid).
    """
    ox, oy, width, height = vrect
    if width <= 0 or height <= 0:
        return (int(ox), int(oy))
    cx, cy = _clamp_grid(gx), _clamp_grid(gy)
    return (int(ox + round(cx / OVERLAY_GRID_MAX * width)),
            int(oy + round(cy / OVERLAY_GRID_MAX * height)))


def pixel_to_grid(px, py, vrect: tuple) -> tuple[int, int]:
    """Virtual-screen pixels back to the 0-1000 grid (pure, hermetic)."""
    ox, oy, width, height = vrect
    if width <= 0 or height <= 0:
        return (0, 0)
    try:
        fx, fy = float(px), float(py)
    except (TypeError, ValueError):
        return (0, 0)
    if not math.isfinite(fx) or not math.isfinite(fy):
        return (0, 0)
    return (_clamp_grid((fx - ox) / width * OVERLAY_GRID_MAX),
            _clamp_grid((fy - oy) / height * OVERLAY_GRID_MAX))


def ease_waypoints(ax, ay, bx, by, steps: int) -> list:
    """Ease-out-cubic waypoints from a to b (pure, hermetic).

    Returns ``steps`` ``(x, y)`` float points excluding the start and
    ending EXACTLY on b. Ease-out (fast start, soft landing) reads as
    a deliberate move rather than a mechanical slide.
    """
    try:
        count = max(1, int(steps))
    except (TypeError, ValueError):
        count = 1
    points = []
    for i in range(1, count + 1):
        t = i / count
        eased = 1.0 - (1.0 - t) ** 3
        points.append((ax + (bx - ax) * eased, ay + (by - ay) * eased))
    points[-1] = (float(bx), float(by))
    return points


def bowed_waypoints(ax, ay, bx, by, steps: int) -> list:
    """Bowed ease-out waypoints (pure, hermetic, Bluey-style curve).

    Same timing as ease_waypoints but the path arcs through a control
    point offset perpendicular to travel (at most 8% of distance,
    capped at 48px, bowed upward for sideways moves). Ends EXACTLY on
    b. Settle-to-dock keeps the straight ease; only target glides bow.
    """
    try:
        count = max(1, int(steps))
    except (TypeError, ValueError):
        count = 1
    try:
        fx, fy, tx, ty = float(ax), float(ay), float(bx), float(by)
    except (TypeError, ValueError):
        return [(float(bx), float(by))]
    dx, dy = tx - fx, ty - fy
    dist = math.hypot(dx, dy)
    if dist == 0 or not math.isfinite(dist):
        return [(tx, ty)] * count if count > 1 else [(tx, ty)]
    nx, ny = -dy / dist, dx / dist
    if ny > 0:
        nx, ny = -nx, -ny
    bow = min(dist * 0.08, 48.0)
    cx, cy = (fx + tx) / 2 + nx * bow, (fy + ty) / 2 + ny * bow
    points = []
    for i in range(1, count + 1):
        t = i / count
        eased = 1.0 - (1.0 - t) ** 3
        u = 1.0 - eased
        x = u * u * fx + 2 * u * eased * cx + eased * eased * tx
        y = u * u * fy + 2 * u * eased * cy + eased * eased * ty
        points.append((x, y))
    points[-1] = (tx, ty)
    return points


# Bluey cubic-bezier timing curves (port of CursorOverlay.swift
# TimingCurve: CSS-style cubic-bezier maps time 0..1 to progress 0..1).
TIMING_LAUNCH = (0.42, 0.0, 0.22, 1.0)
TIMING_REDIRECT = (0.25, 0.25, 0.22, 1.0)
TIMING_STEADY = (0.42, 0.0, 0.38, 1.0)
_TIMING_BY_NAME = {
    "launch": TIMING_LAUNCH,
    "redirect": TIMING_REDIRECT,
    "steady": TIMING_STEADY,
}


def _cubic_coord(t: float, a: float, b: float) -> float:
    u = 1.0 - t
    return 3 * u * u * t * a + 3 * u * t * t * b + t * t * t


def _cubic_deriv(t: float, a: float, b: float) -> float:
    u = 1.0 - t
    return 3 * u * u * a + 6 * u * t * (b - a) + 3 * t * t * (1 - b)


def _timing_param(curve, x: float) -> float:
    try:
        x1, _y1, x2, _y2 = (float(v) for v in curve)
    except (TypeError, ValueError):
        return min(max(float(x), 0.0), 1.0) if isinstance(
            x, (int, float)) and math.isfinite(x) else 0.0
    try:
        target = min(max(float(x), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(target):
        return 0.0
    t = target
    for _ in range(8):
        err = _cubic_coord(t, x1, x2) - target
        if abs(err) < 1e-7:
            return t
        d = _cubic_deriv(t, x1, x2)
        if abs(d) < 1e-7:
            break
        t -= err / d
    low, high, t = 0.0, 1.0, target
    for _ in range(40):
        value = _cubic_coord(t, x1, x2)
        if abs(value - target) < 1e-7:
            break
        if value < target:
            low = t
        else:
            high = t
        t = (low + high) / 2
    return t


def timing_value(curve, x) -> float:
    """Bezier progress 0..1 for time ``x`` (pure, hermetic).

    Port of ``TimingCurve.value(at:)``. Malformed curves clamp to
    linear time; out-of-range ``x`` clamps to 0/1.
    """
    try:
        fx = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(fx):
        return 0.0
    if fx <= 0:
        return 0.0
    if fx >= 1:
        return 1.0
    try:
        x1, y1, x2, y2 = (float(v) for v in curve)
    except (TypeError, ValueError):
        return fx
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        return fx
    return _cubic_coord(_timing_param((x1, y1, x2, y2), fx), y1, y2)


def timing_rate(curve, x) -> float:
    """d(progress)/d(time) at ``x`` (pure, hermetic).

    Port of ``TimingCurve.rate(at:)``. Zero on degenerate slopes.
    """
    try:
        fx = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(fx):
        return 0.0
    clamped = min(max(fx, 0.0), 1.0)
    try:
        x1, y1, x2, y2 = (float(v) for v in curve)
    except (TypeError, ValueError):
        return 0.0
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        return 0.0
    t = _timing_param((x1, y1, x2, y2), clamped)
    dx = _cubic_deriv(t, x1, x2)
    dy = _cubic_deriv(t, y1, y2)
    if abs(dx) <= 1e-7:
        return 0.0
    return dy / dx


def flight_position(from_xy, c1_xy, c2_xy, to_xy, e) -> tuple[float, float]:
    """Cubic-bezier point at eased progress ``e`` (pure, hermetic).

    Port of ``Flight.position``. Malformed inputs fall back to ``to``.
    """
    try:
        fx, fy = float(from_xy[0]), float(from_xy[1])
        ax, ay = float(c1_xy[0]), float(c1_xy[1])
        bx, by = float(c2_xy[0]), float(c2_xy[1])
        tx, ty = float(to_xy[0]), float(to_xy[1])
        fe = float(e)
    except (TypeError, ValueError, IndexError):
        try:
            return (float(to_xy[0]), float(to_xy[1]))
        except (TypeError, ValueError, IndexError):
            return (0.0, 0.0)
    vals = (fx, fy, ax, ay, bx, by, tx, ty, fe)
    if not all(math.isfinite(v) for v in vals):
        return (tx, ty)
    u = 1.0 - fe
    a = u * u * u
    b = 3 * u * u * fe
    c = 3 * u * fe * fe
    d = fe * fe * fe
    return (a * fx + b * ax + c * bx + d * tx,
            a * fy + b * ay + c * by + d * ty)


def flight_tangent(from_xy, c1_xy, c2_xy, to_xy, e) -> tuple[float, float]:
    """Cubic-bezier derivative at ``e`` (pure, hermetic).

    Port of ``Flight.tangent``. Malformed inputs yield ``(0, 0)``.
    """
    try:
        fx, fy = float(from_xy[0]), float(from_xy[1])
        ax, ay = float(c1_xy[0]), float(c1_xy[1])
        bx, by = float(c2_xy[0]), float(c2_xy[1])
        tx, ty = float(to_xy[0]), float(to_xy[1])
        fe = float(e)
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0)
    vals = (fx, fy, ax, ay, bx, by, tx, ty, fe)
    if not all(math.isfinite(v) for v in vals):
        return (0.0, 0.0)
    u = 1.0 - fe
    a = 3 * u * u
    b = 6 * u * fe
    c = 3 * fe * fe
    return (a * (ax - fx) + b * (bx - ax) + c * (tx - bx),
            a * (ay - fy) + b * (by - ay) + c * (ty - by))


def plan_flight(from_xy, goal_xy, velocity_xy=(0.0, 0.0),
                now: float = 0.0, bounds=(0, 0, 1920, 1080),
                dragging: bool = False) -> dict:
    """Plan a Bluey-style trip (pure, hermetic).

    Port of ``CursorEngine.plan(to:)``. ``bounds`` is the virtual-screen
    ``(ox, oy, w, h)`` rect (F15 parity: negative origins kept; the
    middle is ``ox + w/2``). ``now`` is the caller's own monotonic
    clock (the child stamps its own — clocks never cross the pipe).
    ``velocity_xy`` is px/s, init ``(0, 0)``. ``dragging`` forces a
    straight steady trip (bow 0). Returns a flight dict with
    ``timing`` in ``launch|redirect|steady``; zero-distance plans land
    immediately with ``duration`` 0. Never raises.
    """
    try:
        fx, fy = float(from_xy[0]), float(from_xy[1])
        gx, gy = float(goal_xy[0]), float(goal_xy[1])
    except (TypeError, ValueError, IndexError):
        return {"from": (0.0, 0.0), "c1": (0.0, 0.0),
                "c2": (0.0, 0.0), "to": (0.0, 0.0),
                "start": 0.0, "duration": 0.0,
                "timing": "launch", "pointing": True}
    try:
        vx, vy = float(velocity_xy[0]), float(velocity_xy[1])
    except (TypeError, ValueError, IndexError):
        vx, vy = 0.0, 0.0
    try:
        start = float(now)
    except (TypeError, ValueError):
        start = 0.0
    try:
        ox, oy, bw, bh = (float(v) for v in bounds)
    except (TypeError, ValueError):
        ox, oy, bw, bh = 0.0, 0.0, 1920.0, 1080.0
    if not all(math.isfinite(v) for v in (fx, fy, gx, gy)):
        gx, gy = (v if math.isfinite(v) else 0.0 for v in (gx, gy))
        fx, fy = (v if math.isfinite(v) else 0.0 for v in (fx, fy))
    if not math.isfinite(start):
        start = 0.0
    if not all(math.isfinite(v) for v in (vx, vy)):
        vx, vy = 0.0, 0.0
    if not all(math.isfinite(v) for v in (ox, oy, bw, bh)):
        ox, oy, bw, bh = 0.0, 0.0, 1920.0, 1080.0
    dragging = dragging is True
    dx, dy = gx - fx, gy - fy
    dist = math.hypot(dx, dy)
    if not math.isfinite(dist) or dist <= 0.5:
        return {"from": (fx, fy), "c1": (fx, fy), "c2": (gx, gy),
                "to": (gx, gy), "start": start, "duration": 0.0,
                "timing": "launch", "pointing": True}
    duration = min(1.2, max(0.45, 0.4 + 0.12 * math.log2(1 + dist / 24)))
    if dragging:
        duration = min(1.6, max(0.6, duration * 1.45))
    direction = (dx / dist, dy / dist)
    normal = (-direction[1], direction[0])
    if abs(direction[0]) > 0.4:
        if normal[1] > 0:
            normal = (-normal[0], -normal[1])
    else:
        middle = ox + bw / 2 if bw > 0 else fx
        toward = 1.0 if middle - (fx + gx) / 2 >= 0 else -1.0
        if normal[0] * toward < 0:
            normal = (-normal[0], -normal[1])
    bow = 0.0 if dragging else min(dist * 0.1, 70.0) * 0.75
    reach = dist * 0.3
    c2 = (gx - direction[0] * reach + normal[0] * bow,
          gy - direction[1] * reach + normal[1] * bow)
    speed = math.hypot(vx, vy)
    if speed > 60:
        carry = (vx * duration / 3, vy * duration / 3)
        length = math.hypot(*carry)
        if length > dist * 0.6:
            scale = dist * 0.6 / length
            carry = (carry[0] * scale, carry[1] * scale)
        c1 = (fx + carry[0], fy + carry[1])
        timing = "redirect"
    else:
        c1 = (fx + direction[0] * reach + normal[0] * bow,
              fy + direction[1] * reach + normal[1] * bow)
        timing = "steady" if dragging else "launch"
    return {"from": (fx, fy), "c1": c1, "c2": c2, "to": (gx, gy),
            "start": start, "duration": duration,
            "timing": timing, "pointing": True}


def flight_time_remaining(flight, now) -> float:
    """Seconds until a planned flight arrives (pure, hermetic).

    Port of ``CursorEngine.timeToArrive``. Zero for landed/malformed.
    """
    try:
        start = float(flight["start"])
        duration = float(flight["duration"])
        moment = float(now)
    except (TypeError, ValueError, KeyError, AttributeError):
        return 0.0
    if not all(math.isfinite(v) for v in (start, duration, moment)):
        return 0.0
    return max(0.0, start + duration - moment)


def press_scale(now, press_start, depth: float = 0.05) -> float:
    """Uniform press squish factor (pure, hermetic, overlay-only).

    Port of ``CursorEngine.pressScale``: quick press-in over 0.12s,
    then a spring back with a tiny rebound over 0.30s (total 0.42s).
    Returns 1.0 outside the window or on malformed input.
    """
    try:
        moment, origin = float(now), float(press_start)
        amount = float(depth)
    except (TypeError, ValueError):
        return 1.0
    if not all(math.isfinite(v) for v in (moment, origin, amount)):
        return 1.0
    t = moment - origin
    if t < 0 or t >= 0.42:
        return 1.0
    if t < 0.12:
        return 1 - amount * math.sin(t / 0.12 * math.pi / 2)
    r = (t - 0.12) / 0.3
    return 1 - amount * math.cos(r * math.pi * 1.5) * math.exp(-r * 3.2)


def ring_frame(now, start, duration, radius) -> tuple | None:
    """Expanding ring frame (pure, hermetic, overlay-only).

    Returns ``(radius_px, width_px)`` while alive, None when expired.
    The ring grows 1.0→1.25x as its outline thins 3→1 — Tk canvas has
    no per-item alpha, so thinning reads as the fade.
    """
    try:
        moment, origin = float(now), float(start)
        life, base = float(duration), float(radius)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (moment, origin, life, base)):
        return None
    if life <= 0 or base <= 0:
        return None
    t = (moment - origin) / life
    if t < 0 or t >= 1:
        return None
    width = 3 if t < 0.5 else (2 if t < 0.8 else 1)
    return (base * (1 + 0.25 * t), width)


def glyph_frame(now, start, life, rise) -> float | None:
    """Rise offset px for a typed glyph (pure, hermetic, overlay-only).

    Ease-out quad over ``life`` seconds climbing ``rise`` px, then
    None (expired — the glyph pops out instead of fading, since Tk
    text items carry no alpha).
    """
    try:
        moment, origin = float(now), float(start)
        span, climb = float(life), float(rise)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (moment, origin, span, climb)):
        return None
    if span <= 0:
        return None
    t = (moment - origin) / span
    if t < 0 or t >= 1:
        return None
    return climb * (1 - (1 - t) * (1 - t))


def effect_glyph_chars(text: str) -> list:
    """Typed-feedback glyphs: first non-space chars, capped (pure).

    Bluey's ``typedEffect`` skips whitespace and shows every char;
    ours caps the live set (Tk items are pooled, not transient layers).
    Non-string input gives []; empty text gives []. Never raises.
    """
    if not isinstance(text, str):
        return []
    try:
        collapsed = " ".join(text.split())
    except Exception:
        return []
    return [ch for ch in collapsed if not ch.isspace()][
        :OVERLAY_GLYPH_CAP]


def hover_offset(now, landed_at, is_home: bool = False,
                 settled: bool = True) -> float:
    """Gentle vertical hover while holding a point (pure, hermetic).

    Port of ``CursorEngine.hover``: zero at home or mid-flight, then
    a 2.4Hz sine at 1.6px eased in over 0.35→0.95s after landing.
    Overlay-only affordance; never raises.
    """
    try:
        moment, landed = float(now), float(landed_at)
    except (TypeError, ValueError):
        return 0.0
    if not all(math.isfinite(v) for v in (moment, landed)):
        return 0.0
    if is_home is True or settled is not True:
        return 0.0
    since = moment - landed
    if since < 0:
        return 0.0
    amount = min(1.0, max(0.0, (since - 0.35) / 0.6))
    if amount <= 0:
        return 0.0
    return math.sin(since * 2.4) * 1.6 * amount


def tip_alpha(*, flight_active: bool = False, dragging: bool = False,
              to_target: bool = False, settled: bool = True,
              press_age=None) -> float:
    """Tip-polygon visibility 0.0/1.0 (pure, hermetic, overlay-only).

    1.0 while working: a flight is active, a drag is held, or the
    press window (``0 <= press_age < 0.42``, same as ``press_scale``)
    is open. 0.0 docked/idle/home/settled. Tk has no per-item alpha
    so the caller shows/hides the polygon. Never raises.

    ``to_target``/``settled`` are frozen (backward compat, ignored):
    docked-ness is already encoded in ``flight_active``/``dragging``/
    ``press_age``, so they stay accepted but unread.
    """
    try:
        if flight_active is True or dragging is True:
            return 1.0
        if press_age is not None:
            age = float(press_age)
            if math.isfinite(age) and 0 <= age < 0.42:
                return 1.0
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def eye_offset(hx, hy, cx, cy, r_max: float) -> tuple[int, int]:
    """Pupil offset from head center toward the cursor (pure, hermetic).

    The full vector when the cursor is near, clamped to ``r_max``
    magnitude when far - the eyes glance toward the mouse without
    leaving the head. ``(0, 0)`` when coincident or ``r_max <= 0``.
    """
    try:
        dx, dy, cap = float(hx), float(hy), float(r_max)
        vx, vy = float(cx) - dx, float(cy) - dy
    except (TypeError, ValueError):
        return (0, 0)
    if cap <= 0:
        return (0, 0)
    dist = math.hypot(vx, vy)
    if dist == 0 or dist <= cap:
        return (int(round(vx)), int(round(vy)))
    return (int(round(vx / dist * cap)), int(round(vy / dist * cap)))


def dock_corner(vrect: tuple, head_px: int,
                corner: str = OVERLAY_DOCK_CORNER,
                margin: int = OVERLAY_DOCK_MARGIN) -> tuple[int, int]:
    """Top-left pixel of the idle-docked head box (pure, hermetic).

    Bottom corners ride ``OVERLAY_DOCK_LIFT`` px above the full vrect
    bottom so the head clears the Windows taskbar; top corners are
    unchanged. Signature frozen.
    """
    ox, oy, width, height = vrect
    try:
        size = max(1, int(head_px))
    except (TypeError, ValueError):
        size = 1
    try:
        pad = max(0, int(margin))
    except (TypeError, ValueError):
        pad = 0
    spots = {
        "br": (ox + width - pad - size,
               oy + height - pad - size - OVERLAY_DOCK_LIFT),
        "bl": (ox + pad, oy + height - pad - size - OVERLAY_DOCK_LIFT),
        "tr": (ox + width - pad - size, oy + pad),
        "tl": (ox + pad, oy + pad),
    }
    picked = spots.get(str(corner).lower(), spots["br"])
    return (int(picked[0]), int(picked[1]))


def head_radii(radius: float, *, hovering: bool = False,
               squished: bool = False) -> tuple[float, float]:
    """Head (rx, ry) for the F16 Bluey states (pure, hermetic).

    Squish wins over hover: on target arrival the head presses
    (wider + flatter for ``OVERLAY_SQUISH_SECONDS``); while gliding
    to a target it hovers slightly grown; idle draws the base circle.
    """
    try:
        base = float(radius)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    if base <= 0:
        return (0.0, 0.0)
    if squished:
        return (base * OVERLAY_SQUISH_RX, base * OVERLAY_SQUISH_RY)
    if hovering:
        grown = base * OVERLAY_HOVER_SCALE
        return (grown, grown)
    return (base, base)


def trail_push(trail: list, x: float, y: float,
               cap: int = OVERLAY_TRAIL_POINTS) -> list:
    """Append one head position, oldest-first, capped (pure, hermetic).

    Never raises on malformed input; non-finite points are ignored so
    a bad poll never poisons the trail.
    """
    try:
        items = list(trail or [])
    except TypeError:
        items = []
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return items[-max(1, cap - 1):] if cap else items
    if not math.isfinite(fx) or not math.isfinite(fy):
        return items
    try:
        limit = max(1, int(cap))
    except (TypeError, ValueError):
        limit = OVERLAY_TRAIL_POINTS
    items.append((fx, fy))
    return items[-limit:]


def bubble_text_for(x, y) -> str | None:
    """Coordinate bubble label for a target point (pure, hermetic).

    None for malformed points (the job already validated its args —
    the overlay never second-guesses execution, same contract as
    ``Overlay.announce``).
    """
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, (int, float)) or not isinstance(
            y, (int, float)):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    if not 0 <= x <= OVERLAY_GRID_MAX or not 0 <= y <= OVERLAY_GRID_MAX:
        return None
    return f"\u2316 {int(x)},{int(y)}"


def choreo_state(now: float) -> dict:
    """Fresh point-queue choreography state (pure data, hermetic)."""
    try:
        stamp = float(now)
    except (TypeError, ValueError):
        stamp = 0.0
    if not math.isfinite(stamp):
        stamp = 0.0
    return {"queue": [], "hold_until": stamp, "home_requested": False,
            "speaking": False, "quiet_since": stamp}


def _choreo_grid(x, y) -> tuple | None:
    """Validated 0-1000 grid ints for a queued point (None when bad)."""
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, (int, float)) or not isinstance(
            y, (int, float)):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    if not 0 <= x <= OVERLAY_GRID_MAX or not 0 <= y <= OVERLAY_GRID_MAX:
        return None
    return (int(x), int(y))


def choreo_point(state: dict, x, y, label: str = "") -> bool:
    """Enqueue a tour point; clears any home request (pure, hermetic).

    Bluey's ``point()``: the spot joins the queue and the head works
    through it when the hold expires — tools never wait on it.
    Labels collapse to one line (capped). Never raises; False when
    the point or state is malformed.
    """
    try:
        grid = _choreo_grid(x, y)
        if grid is None:
            return False
        text = " ".join(str(label or "").split())
        if len(text) > OVERLAY_POINT_LABEL_CAP:
            text = text[:OVERLAY_POINT_LABEL_CAP] + "…"
        state["queue"].append((grid[0], grid[1], text))
        state["home_requested"] = False
        return True
    except (AttributeError, TypeError):
        return False


def choreo_request_home(state: dict) -> None:
    """Flag a trip home once the head is quiet (pure, never raises)."""
    with contextlib.suppress(AttributeError, TypeError):
        state["home_requested"] = True


def choreo_set_speaking(state: dict, on: bool, now: float) -> None:
    """Track reply speech (pure, never raises).

    While speaking, homing pauses; the moment speech stops stamps
    quiet (Bluey's ``setSpeaking``). ``on`` must be a real bool —
    anything else is ignored, like ``confirm_action``'s approve.
    """
    try:
        flag = on is True
        if state.get("speaking") and not flag:
            try:
                stamp = float(now)
            except (TypeError, ValueError):
                stamp = state.get("quiet_since", 0.0)
            if math.isfinite(stamp):
                state["quiet_since"] = stamp
        state["speaking"] = flag
    except (AttributeError, TypeError):
        pass


def choreo_take_control(state: dict) -> None:
    """Drop the queued tour: a real action flies now (pure, Item-1c).

    ``announce()`` (a click/drag point) preempts the visual tour —
    the head goes where the action lands instead. Never raises.
    """
    with contextlib.suppress(AttributeError, TypeError):
        state["queue"].clear()


def choreo_go_home(state: dict) -> None:
    """Clear a completed trip home (pure, never raises)."""
    with contextlib.suppress(AttributeError, TypeError):
        state["queue"].clear()
        state["home_requested"] = False


def choreo_step(state: dict, now: float) -> tuple:
    """One 1/30s choreography decision (pure, hermetic, never raises).

    Returns ``("launch", (x, y, label))`` to fly a queued point (the
    caller stamps the hold), ``("dock", None)`` to glide home, or
    ``(None, None)`` to hold position. Holds run 1.0s flight
    allowance + 2.2s minimum; home fires 1.2s after a request once
    quiet, else after 5.0s quiet; speaking pauses homing.
    """
    try:
        now_f = float(now)
        hold_until = float(state["hold_until"])
        home_requested = state["home_requested"] is True
        speaking = state["speaking"] is True
        quiet_since = float(state["quiet_since"])
        queued = state["queue"]
    except (AttributeError, TypeError, ValueError):
        return (None, None)
    if not math.isfinite(now_f) or not math.isfinite(hold_until):
        return (None, None)
    if now_f < hold_until:
        return (None, None)
    try:
        items = list(queued)
    except TypeError:
        return (None, None)
    if items:
        try:
            state["queue"].pop(0)
        except (AttributeError, IndexError, TypeError):
            return (None, None)
        state["hold_until"] = now_f + OVERLAY_POINT_FLIGHT_SECONDS \
            + OVERLAY_POINT_MIN_HOLD_SECONDS
        if not speaking and math.isfinite(quiet_since):
            state["quiet_since"] = now_f
        return ("launch", items[0])
    try:
        quiet_for = 0.0 if speaking else now_f - quiet_since
    except (TypeError, ValueError):
        return (None, None)
    if not math.isfinite(quiet_for):
        return (None, None)
    if (home_requested and quiet_for > OVERLAY_HOME_QUIET_SECONDS) \
            or quiet_for > OVERLAY_IDLE_HOME_SECONDS:
        return ("dock", None)
    return (None, None)


def berry_shades() -> tuple[str, str, str]:
    """Light→mid→deep fills for the stacked-oval gradient (pure)."""
    return (OVERLAY_BERRY_LIGHT, OVERLAY_BERRY_MID, OVERLAY_BERRY_DEEP)


def teardrop_points(hx: float, hy: float, rx: float, ry: float) -> list:
    """Tip triangle points above-left of the head (pure, hermetic).

    Empty list for malformed input; otherwise 3 (x, y) points. The tip
    reads as Bluey's teardrop peak without copying its exact path.
    """
    try:
        fx, fy, arx, ary = float(hx), float(hy), float(rx), float(ry)
    except (TypeError, ValueError):
        return []
    if not all(math.isfinite(v) for v in (fx, fy, arx, ary)):
        return []
    if arx <= 0 or ary <= 0:
        return []
    return [
        (fx - arx * 0.95, fy - ary * 0.75),
        (fx - arx * 0.10, fy - ary * 1.30),
        (fx + arx * 0.25, fy - ary * 0.70),
    ]


def virtual_screen() -> tuple[int, int, int, int]:
    """Virtual-screen ``(ox, oy, w, h)`` via GetSystemMetrics.

    Windows only; raises OSError elsewhere (callers gate on
    ``os.name``). Falls back to the primary monitor when the virtual
    metrics read empty.
    """
    if os.name != "nt":
        raise OSError("overlay metrics read only on Windows")
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    rect = (user32.GetSystemMetrics(_SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(_SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    if rect[2] <= 0 or rect[3] <= 0:
        rect = (0, 0, user32.GetSystemMetrics(_SM_CXSCREEN),
                user32.GetSystemMetrics(_SM_CYSCREEN))
    return (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))


def cursor_pos() -> tuple[int, int]:
    """Live cursor position via GetCursorPos (Windows only, read-only)."""
    if os.name != "nt":
        raise OSError("cursor polling only runs on Windows")
    import ctypes

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    user32 = ctypes.windll.user32
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
    user32.GetCursorPos.restype = ctypes.c_int
    point = _Point()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise OSError("GetCursorPos failed")
    return (int(point.x), int(point.y))


class Overlay:
    """Parent-side handle to the overlay child process.

    Best-effort by contract: ``start``/``announce``/``point``/
    ``request_home``/``set_speaking``/``settle`` never raise, and
    ``alive`` is False whenever there is no window (off Windows,
    spawn failure, dead child). Jobs check ``alive`` and skip the
    pre-action dwell when it is False.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self) -> bool:
        """Spawn the overlay child (idempotent). Never raises."""
        if os.name != "nt":
            return False
        if self.alive:
            return True
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-c",
                 "from invincible.agent.overlay import overlay_main; "
                 "overlay_main()"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                creationflags=getattr(
                    subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            self._proc = None
            return False
        return True

    def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

    def announce(self, point: dict, dragging: bool = False) -> None:
        """Glide the head to a resolved 0-1000 grid point. Never raises.

        takeControl: a real action flies NOW and drops any queued tour
        (the child clears its point queue on receipt). Malformed
        points are ignored (the job already validated its own args -
        the overlay never second-guesses execution).
        ``dragging`` selects the straight steady trip; omitted/False
        keeps the classic bowed glide (backward compat).
        """
        if not isinstance(point, dict):
            return
        x, y = point.get("x"), point.get("y")
        if isinstance(x, bool) or isinstance(y, bool):
            return
        if not isinstance(x, (int, float)) or not isinstance(
                y, (int, float)):
            return
        if not math.isfinite(x) or not math.isfinite(y):
            return
        if not 0 <= x <= OVERLAY_GRID_MAX or not 0 <= y <= OVERLAY_GRID_MAX:
            return
        payload: dict = {"type": "target", "x": int(x), "y": int(y)}
        if dragging is True:
            payload["dragging"] = True
        self._send(payload)

    def point(self, point: dict, label: str = "") -> None:
        """Queue a tour point; the head catches up visually. Never raises.

        Bluey's ``point()``: appends to the child queue and clears any
        home request — tools run immediately, the head glides through
        queued spots on its own holds. Same validation as
        ``announce``; malformed points are ignored.
        """
        grid = _choreo_grid(
            point.get("x") if isinstance(point, dict) else None,
            point.get("y") if isinstance(point, dict) else None)
        if grid is None:
            return
        payload: dict = {"type": "point", "x": grid[0], "y": grid[1]}
        text = " ".join(str(label or "").split())
        if text:
            if len(text) > OVERLAY_POINT_LABEL_CAP:
                text = text[:OVERLAY_POINT_LABEL_CAP] + "…"
            payload["label"] = text
        self._send(payload)

    def request_home(self) -> None:
        """Ask the head home once quiet (sets the flag, never raises)."""
        self._send({"type": "home"})

    def set_speaking(self, on: bool) -> None:
        """Track reply speech: homing pauses while true (never raises).

        ``on`` must be a real bool — anything else sends False, like
        ``confirm_action`` treats non-true approve as deny.
        """
        self._send({"type": "speaking", "on": on is True})

    def effect(self, kind: str, point=None, button: str = "left",
               text: str = "") -> None:
        """Post-action click/type feedback (child draw only, never raises).

        Sent after a successful computer action: ``click`` squishes +
        rings + bubbles the coordinates (right button gets the deep
        tint), ``type`` squishes + floats up to 6 glyphs of ``text``.
        Unknown kinds and malformed points are ignored — the job
        already validated its args and its result stands regardless.
        """
        if kind == "click":
            grid = _choreo_grid(
                point.get("x") if isinstance(point, dict) else None,
                point.get("y") if isinstance(point, dict) else None)
            if grid is None:
                return
            payload: dict = {"type": "effect", "kind": "click",
                             "x": grid[0], "y": grid[1],
                             "button": ("right" if button == "right"
                                        else "left")}
            self._send(payload)
            return
        if kind == "type":
            chars = effect_glyph_chars(text)
            if not chars:
                return
            self._send({"type": "effect", "kind": "type",
                        "text": "".join(chars)})
            return

    def settle(self) -> None:
        """Ask the head to dock now (idle timeout docks anyway)."""
        self._send({"type": "settle"})

    def stop(self) -> None:
        """Terminate the child. Never raises; safe to call twice."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                try:
                    assert proc.stdin is not None
                    proc.stdin.write(json.dumps({"type": "quit"}) + "\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                proc.wait(timeout=2)
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.kill()


def _stdin_reader(inbox: queue.Queue) -> None:
    """Feed stdin JSON-lines into the queue; parent death (EOF) quits."""
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("type") in (
                "target", "point", "home", "speaking", "effect",
                "settle", "quit"):
            with contextlib.suppress(queue.Full):
                inbox.put_nowait(message)
    with contextlib.suppress(queue.Full):
        inbox.put_nowait({"type": "quit"})


def overlay_main() -> None:
    """Child-process entry: own the Tk window, read stdin commands.

    Exits quietly (code 0, headless) on every failure path - off
    Windows, no tkinter, no display, or a lost click-through flag
    (fail closed: an overlay that could intercept clicks must never
    stay up).
    """
    if os.name != "nt":
        return
    try:
        import tkinter as tk
    except Exception:
        return
    try:
        _run_window(tk)
    except Exception:
        return


def _run_window(tk) -> None:
    import ctypes

    vrect = virtual_screen()
    ox, oy, width, height = vrect
    root = tk.Tk()
    try:
        root.overrideredirect(True)
        root.geometry(f"{width}x{height}+{ox}+{oy}")
        root.configure(bg=OVERLAY_TRANSPARENT_KEY)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", OVERLAY_TRANSPARENT_KEY)
        canvas = tk.Canvas(root, width=width, height=height,
                           bg=OVERLAY_TRANSPARENT_KEY,
                           highlightthickness=0, bd=0)
        canvas.pack()
        root.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id())
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.GetCursorPos.argtypes = [ctypes.c_void_p]
        user32.GetCursorPos.restype = ctypes.c_int
        before = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, _GWL_EXSTYLE,
                              before | _WS_EX_TRANSPARENT | _WS_EX_LAYERED)
        after = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if not after & _WS_EX_TRANSPARENT:
            raise OSError("click-through flag did not stick")
    except Exception:
        with contextlib.suppress(Exception):
            root.destroy()
        raise

    radius = OVERLAY_HEAD_PX // 2
    dock = dock_corner(vrect, OVERLAY_HEAD_PX)
    head_xy = [float(dock[0] + radius), float(dock[1] + radius)]
    light, mid, deep = berry_shades()
    glow = canvas.create_oval(0, 0, 1, 1, fill=OVERLAY_GLOW_FILL,
                              outline=OVERLAY_GLOW_FILL, width=1)
    with contextlib.suppress(Exception):
        canvas.itemconfigure(glow, stipple="gray25")
    if not OVERLAY_GLOW:
        canvas.coords(glow, -10, -10, -10, -10)
    head_deep = canvas.create_oval(0, 0, 1, 1, fill=deep,
                                   outline=OVERLAY_HEAD_OUTLINE, width=3)
    head_mid = canvas.create_oval(0, 0, 1, 1, fill=mid,
                                  outline="", width=0)
    head_light = canvas.create_oval(0, 0, 1, 1, fill=light,
                                    outline="", width=0)
    tip = canvas.create_polygon(0, 0, 1, 1, 2, 2, fill=mid,
                                outline=OVERLAY_HEAD_OUTLINE, width=2)
    shine = canvas.create_oval(0, 0, 1, 1, fill=OVERLAY_SHINE_FILL,
                               outline=OVERLAY_SHINE_FILL)
    eye_l = canvas.create_oval(0, 0, 1, 1, fill=OVERLAY_EYE_FILL,
                               outline=OVERLAY_EYE_FILL)
    eye_r = canvas.create_oval(0, 0, 1, 1, fill=OVERLAY_EYE_FILL,
                               outline=OVERLAY_EYE_FILL)
    trail_ids = [
        canvas.create_oval(-10, -10, -10, -10,
                           fill=OVERLAY_HEAD_OUTLINE,
                           outline=OVERLAY_HEAD_OUTLINE)
        for _ in range(OVERLAY_TRAIL_POINTS)
    ]
    bubble_box = canvas.create_rectangle(-10, -10, -10, -10,
                                         fill=OVERLAY_HEAD_FILL,
                                         outline=OVERLAY_HEAD_OUTLINE)
    bubble_label = canvas.create_text(-10, -10, text="",
                                      fill=OVERLAY_EYE_FILL,
                                      font=("Segoe UI", 9))
    click_ring = canvas.create_oval(-10, -10, -10, -10,
                                    outline=OVERLAY_RING_LEFT_FILL,
                                    width=3)
    glyph_ids = [
        canvas.create_text(-10, -10, text="",
                           fill=OVERLAY_BERRY_DEEP,
                           font=("Segoe UI", 22, "bold"))
        for _ in range(OVERLAY_GLYPH_CAP)
    ]
    inbox: queue.Queue = queue.Queue(maxsize=16)
    reader = threading.Thread(target=_stdin_reader, args=(inbox,),
                              daemon=True)
    reader.start()
    try:
        cursor = cursor_pos()
    except OSError:
        cursor = (ox + width // 2, oy + height // 2)
    steps = max(2, int(round(
        OVERLAY_GLIDE_SECONDS / OVERLAY_POLL_SECONDS)))
    now0 = time.monotonic()
    state = {"path": [], "to_target": False,
             "trail": [], "squash_until": 0.0,
             "bubble_until": 0.0, "bubble_text": "",
             "flight": None, "velocity": [0.0, 0.0],
             "landed_at": now0 - 10.0,
             "press_start": now0 - 10.0, "press_depth": 0.0,
             "dragging": False, "choreo": choreo_state(now0),
             "ring": None, "glyphs": []}

    def _dock() -> None:
        target = (dock[0] + radius, dock[1] + radius)
        state["to_target"] = False
        state["flight"] = None
        state["velocity"] = [0.0, 0.0]
        state["dragging"] = False
        state["path"] = ease_waypoints(
            head_xy[0], head_xy[1],
            float(target[0]), float(target[1]), steps)

    def _launch(gx, gy, label, dragging,
                bubble_seconds: float) -> None:
        now_child = time.monotonic()
        px, py = grid_to_pixel(gx, gy, vrect)
        state["flight"] = plan_flight(
            (head_xy[0], head_xy[1]),
            (float(px), float(py)),
            tuple(state["velocity"]),
            now_child, vrect, dragging)
        state["path"] = []
        state["to_target"] = True
        state["dragging"] = dragging
        text = label or bubble_text_for(gx, gy)
        if text:
            state["bubble_text"] = text
            state["bubble_until"] = now_child + bubble_seconds

    def _spawn_ring(x, y, radius, duration, tint) -> None:
        state["ring"] = {"x": float(x), "y": float(y),
                         "start": time.monotonic(),
                         "duration": duration, "radius": radius,
                         "tint": tint}

    def _spawn_land_ring() -> None:
        _spawn_ring(head_xy[0], head_xy[1], OVERLAY_LAND_RING_PX,
                    OVERLAY_LAND_RING_SECONDS, OVERLAY_RING_LEFT_FILL)

    def _effect(message: dict) -> None:
        now_fx = time.monotonic()
        kind = message.get("kind")
        if kind == "click":
            grid = _choreo_grid(message.get("x"), message.get("y"))
            if grid is None:
                return
            state["press_start"] = now_fx
            state["press_depth"] = OVERLAY_PRESS_CLICK
            tint = (OVERLAY_RING_RIGHT_FILL
                    if message.get("button") == "right"
                    else OVERLAY_RING_LEFT_FILL)
            _spawn_ring(head_xy[0], head_xy[1], OVERLAY_CLICK_RING_PX,
                        OVERLAY_CLICK_RING_SECONDS, tint)
            label = bubble_text_for(*grid)
            if label:
                state["bubble_text"] = label
                state["bubble_until"] = (
                    now_fx + OVERLAY_CLICK_BUBBLE_SECONDS)
        elif kind == "type":
            chars = effect_glyph_chars(message.get("text", ""))
            if not chars:
                return
            state["press_start"] = now_fx
            state["press_depth"] = OVERLAY_PRESS_TYPE
            base_x = head_xy[0] + radius * 0.6
            base_y = head_xy[1] - radius
            state["glyphs"] = [
                {"x": base_x + i * 8.0, "y": base_y,
                 "char": ch, "start": now_fx}
                for i, ch in enumerate(chars)]

    def _draw() -> None:
        hx, hy = head_xy
        now = time.monotonic()
        squished = now < state["squash_until"]
        hovering = (state["flight"] is not None or bool(state["path"])) \
            and bool(state["to_target"])
        rx, ry = head_radii(radius, hovering=hovering,
                            squished=squished)
        with contextlib.suppress(Exception):
            ps = press_scale(now, state["press_start"],
                             state["press_depth"])
            if math.isfinite(ps) and ps > 0:
                rx, ry = rx * ps, ry * ps
        settled = state["flight"] is None and not state["path"]
        is_home = (not bool(state["to_target"]) and settled)
        with contextlib.suppress(Exception):
            dy = hover_offset(now, state["landed_at"],
                              is_home=is_home, settled=settled)
            if math.isfinite(dy):
                hy = hy + dy
        trail = state["trail"]
        for i, dot in enumerate(trail_ids):
            if i < len(trail):
                tx, ty = trail[i]
                size = 2.0 + 4.0 * (i + 1) / max(len(trail), 1)
                canvas.coords(dot, tx - size, ty - size,
                              tx + size, ty + size)
            else:
                canvas.coords(dot, -10, -10, -10, -10)
        ex, ey = eye_offset(hx, hy, cursor[0], cursor[1],
                            OVERLAY_EYE_TRACK_R)
        gr = max(rx, ry) * 1.35
        canvas.coords(glow, hx - gr, hy - gr, hx + gr, hy + gr)
        canvas.coords(head_deep, hx - rx, hy - ry, hx + rx, hy + ry)
        canvas.coords(head_mid, hx - rx * 0.86, hy - ry * 0.86,
                      hx + rx * 0.86, hy + ry * 0.86)
        canvas.coords(head_light, hx - rx * 0.62, hy - ry * 0.72,
                      hx + rx * 0.45, hy + ry * 0.05)
        pts = teardrop_points(hx, hy, rx, ry)
        try:
            tip_on = tip_alpha(
                flight_active=state["flight"] is not None,
                dragging=state["dragging"] is True,
                to_target=bool(state["to_target"]),
                settled=settled,
                press_age=now - float(state["press_start"])) >= 0.5
        except Exception:
            tip_on = False
        if pts and tip_on:
            flat = [c for p in pts for c in p]
            canvas.coords(tip, *flat)
        else:
            canvas.coords(tip, -10, -10, -10, -10, -10, -10)
        sr = max(rx, ry) * 0.16
        canvas.coords(shine, hx - rx * 0.42 - sr, hy - ry * 0.52 - sr,
                      hx - rx * 0.42 + sr, hy - ry * 0.52 + sr)
        for item, side in ((eye_l, -1), (eye_r, 1)):
            ecx = hx + side * OVERLAY_EYE_DX + ex
            ecy = hy + OVERLAY_EYE_DY + ey
            canvas.coords(item, ecx - OVERLAY_EYE_R, ecy - OVERLAY_EYE_R,
                          ecx + OVERLAY_EYE_R, ecy + OVERLAY_EYE_R)
        if now < state["bubble_until"] and state["bubble_text"]:
            label = state["bubble_text"]
            top = hy - ry - 34
            half = min(120.0, 8.0 + len(label) * 3.6)
            canvas.coords(bubble_box, hx - half, top - 11,
                          hx + half, top + 11)
            canvas.coords(bubble_label, hx, top)
            canvas.itemconfigure(bubble_label, text=label)
        else:
            canvas.coords(bubble_box, -10, -10, -10, -10)
            canvas.coords(bubble_label, -10, -10)
        ring = state["ring"]
        if isinstance(ring, dict):
            frame = None
            with contextlib.suppress(Exception):
                frame = ring_frame(now, ring["start"],
                                   ring["duration"], ring["radius"])
            if frame is None:
                state["ring"] = None
                canvas.coords(click_ring, -10, -10, -10, -10)
            else:
                with contextlib.suppress(Exception):
                    canvas.itemconfigure(click_ring,
                                         outline=ring["tint"])
                    canvas.coords(click_ring, hx - frame[0], hy - frame[0],
                                  hx + frame[0], hy + frame[0])
                    canvas.itemconfigure(click_ring, width=frame[1])
        else:
            canvas.coords(click_ring, -10, -10, -10, -10)
        live = state["glyphs"]
        if live:
            kept = []
            for i, glyph in enumerate(live):
                if not isinstance(glyph, dict):
                    continue
                rise = None
                with contextlib.suppress(Exception):
                    rise = glyph_frame(now, glyph["start"],
                                       OVERLAY_GLYPH_LIFE_SECONDS,
                                       OVERLAY_GLYPH_RISE_PX)
                if rise is None:
                    continue
                kept.append(glyph)
                if i < len(glyph_ids):
                    with contextlib.suppress(Exception):
                        canvas.coords(glyph_ids[i], glyph["x"],
                                      glyph["y"] - rise)
                        canvas.itemconfigure(
                            glyph_ids[i], text=glyph["char"])
            state["glyphs"] = kept
            for j in range(len(kept), len(glyph_ids)):
                canvas.coords(glyph_ids[j], -10, -10)

    def _tick() -> None:
        nonlocal cursor
        try:
            while True:
                message = inbox.get_nowait()
                kind = message.get("type")
                if kind == "quit":
                    root.destroy()
                    return
                if kind == "settle":
                    with contextlib.suppress(Exception):
                        _dock()
                        choreo_go_home(state["choreo"])
                elif kind == "target":
                    with contextlib.suppress(Exception):
                        choreo_take_control(state["choreo"])
                        _launch(message.get("x"), message.get("y"), "",
                                message.get("dragging", False) is True,
                                OVERLAY_GLIDE_SECONDS
                                + OVERLAY_BUBBLE_SECONDS)
                elif kind == "point":
                    with contextlib.suppress(Exception):
                        choreo_point(state["choreo"], message.get("x"),
                                     message.get("y"),
                                     message.get("label", ""))
                elif kind == "home":
                    with contextlib.suppress(Exception):
                        choreo_request_home(state["choreo"])
                elif kind == "speaking":
                    with contextlib.suppress(Exception):
                        choreo_set_speaking(
                            state["choreo"],
                            message.get("on") is True,
                            time.monotonic())
                elif kind == "effect":
                    with contextlib.suppress(Exception):
                        _effect(message)
        except queue.Empty:
            pass
        with contextlib.suppress(OSError):
            cursor = cursor_pos()
        if state["flight"] is not None:
            with contextlib.suppress(Exception):
                flight = state["flight"]
                now = time.monotonic()
                duration = float(flight["duration"])
                if duration <= 0:
                    head_xy[0], head_xy[1] = flight["to"]
                    state["flight"] = None
                    state["velocity"] = [0.0, 0.0]
                    state["dragging"] = False
                    state["landed_at"] = now
                    if state["to_target"]:
                        state["to_target"] = False
                        state["squash_until"] = now + OVERLAY_SQUISH_SECONDS
                        state["press_start"] = now
                        state["press_depth"] = OVERLAY_PRESS_LAND
                        _spawn_land_ring()
                else:
                    p = min(1.0, max(0.0, (now - float(flight["start"]))
                                            / duration))
                    curve = _TIMING_BY_NAME.get(
                        flight.get("timing"), TIMING_LAUNCH)
                    e = timing_value(curve, p)
                    pos = flight_position(
                        flight["from"], flight["c1"],
                        flight["c2"], flight["to"], e)
                    head_xy[0], head_xy[1] = pos
                    if p >= 1:
                        head_xy[0], head_xy[1] = flight["to"]
                        state["flight"] = None
                        state["velocity"] = [0.0, 0.0]
                        state["dragging"] = False
                        state["landed_at"] = now
                        if state["to_target"]:
                            state["to_target"] = False
                            state["squash_until"] = (
                                now + OVERLAY_SQUISH_SECONDS)
                            state["press_start"] = now
                            state["press_depth"] = OVERLAY_PRESS_LAND
                            _spawn_land_ring()
                    else:
                        rate = timing_rate(curve, p) / duration
                        tangent = flight_tangent(
                            flight["from"], flight["c1"],
                            flight["c2"], flight["to"], e)
                        state["velocity"] = [tangent[0] * rate,
                                             tangent[1] * rate]
        elif state["path"]:
            head_xy[0], head_xy[1] = state["path"].pop(0)
            if not state["path"]:
                now = time.monotonic()
                if state["to_target"]:
                    state["to_target"] = False
                    state["squash_until"] = now + OVERLAY_SQUISH_SECONDS
        else:
            action, payload = None, None
            with contextlib.suppress(Exception):
                action, payload = choreo_step(
                    state["choreo"], time.monotonic())
            if action == "launch":
                with contextlib.suppress(Exception):
                    _launch(payload[0], payload[1], payload[2], False,
                            OVERLAY_POINT_FLIGHT_SECONDS
                            + OVERLAY_POINT_MIN_HOLD_SECONDS)
            elif action == "dock":
                home = (float(dock[0] + radius),
                        float(dock[1] + radius))
                if (head_xy[0], head_xy[1]) != home:
                    with contextlib.suppress(Exception):
                        _dock()
                with contextlib.suppress(Exception):
                    choreo_go_home(state["choreo"])
        state["trail"] = trail_push(
            state["trail"], head_xy[0], head_xy[1])
        _draw()
        root.after(int(OVERLAY_POLL_SECONDS * 1000), _tick)

    _draw()
    _tick()
    root.mainloop()
