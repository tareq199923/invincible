# invincible/agent/overlay.py
"""B1 overlay (F11/F21): a Bmax pointer + companion face on the PC.

A transparent, click-through, always-on-top window draws two pieces
of the web Bmax character (F20/F19) - the same skin as the
robot-heads port's defaults in
``invincible/templates/static/robot-head.js`` (navy shell, slate trim,
pink LEDs). The moving piece is a cursor-like pointer: a classic
up-left arrow carrying the pink robot face with its TIP as the
hotspot, gliding to each Pack 3 target point BEFORE the action
executes (so the user watches Bmax "do" the click/type) and working
through a queued point tour (``point()``) on Bluey-style holds when
it is otherwise free. The stationary piece is a companion face - a
web-style squircle slightly left of the bottom-right corner - that
stays put, shows what the agent is doing, and sleeps when idle; the
parked pointer waits beside it. Tools never wait on either. The
face's eyes follow the pointer while it travels and the operator's
real mouse at ~30Hz while it rests.

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

# Look: the web Bmax skin (F20) - the desktop overlay wears the same
# character as the robot-heads port. SHELL_MID / HEAD_OUTLINE /
# EYE_FILL are copied verbatim from robot-head.js DEFAULTS
# (color / trimColor / screenColor) and pinned against it; DEEP and
# LIGHT are the shell gradient's shaded edge and lit patch.
OVERLAY_SHELL_MID = "#0d142a"
OVERLAY_SHELL_DEEP = "#0a0f20"
OVERLAY_SHELL_LIGHT = "#243056"
OVERLAY_HEAD_OUTLINE = "#4d5b83"
OVERLAY_SHINE_FILL = "#ffffff"
OVERLAY_GLOW_FILL = "#ff5fa6"
OVERLAY_GLOW = True
OVERLAY_EYE_FILL = "#ff5fa6"
OVERLAY_EYE_DX = 14
OVERLAY_EYE_DY = -6
OVERLAY_EYE_R = 7
OVERLAY_EYE_TRACK_R = 9

# F21 pointer + companion face: the single gliding head split into a
# moving cursor-like pointer (the arrow TIP is the hotspot - it lands
# exactly on the action pixel) and a stationary face that lives
# slightly left of the bottom-right corner. Sizes: the pointer box is
# OVERLAY_POINTER_PX tall by OVERLAY_POINTER_W wide; the face is an
# OVERLAY_FACE_PX square shifted OVERLAY_FACE_SHIFT_LEFT px left of
# the true corner (keeps the notification-toast lane clear). The
# parked pointer waits OVERLAY_PARK_GAP px beside the face's left
# edge, vertically centred on it. Eye geometry (OVERLAY_EYE_*) stays
# base-scale: the face draws it multiplied by OVERLAY_FACE_EYE_SCALE
# so the Bmax proportions hold at 110px.
OVERLAY_POINTER_PX = 36
OVERLAY_POINTER_W = 22
OVERLAY_POINTER_STROKE = 2
OVERLAY_PARK_GAP = 14
OVERLAY_FACE_PX = 110
OVERLAY_FACE_SHIFT_LEFT = 52
OVERLAY_FACE_EYE_SCALE = 1.5
OVERLAY_FACE_BOUNCE_PX = 7.0
OVERLAY_FACE_PLATE_SCALE = 0.78
OVERLAY_FACE_GLOW_SCALE = 1.20
OVERLAY_FACE_RING_SCALE = 1.06
OVERLAY_POINTER_EYE_R = 2.0
OVERLAY_POINTER_FACE_STROKE = 1.6

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
# pointer parks beside the face 1.2s after a request once quiet, else
# after 5.0s quiet. These replace the old fixed idle-settle delay -
# homing now flows through choreo_step so a queued tour is never cut
# short.
OVERLAY_POINT_MIN_HOLD_SECONDS = 2.2
OVERLAY_POINT_FLIGHT_SECONDS = 1.0
OVERLAY_IDLE_HOME_SECONDS = 5.0
OVERLAY_HOME_QUIET_SECONDS = 1.2
OVERLAY_POINT_LABEL_CAP = 60
OVERLAY_DOCK_MARGIN = 24
OVERLAY_DOCK_CORNER = "br"
OVERLAY_DOCK_LIFT = 48
# The face docks into the PRIMARY monitor's work area (screen minus the
# real taskbar), so the extra rise above that edge is small: with the
# usual 48px taskbar this sits the face 24px higher than the old
# bounding-box dock (DOCK_LIFT 48 counted the taskbar itself).
OVERLAY_WORKAREA_LIFT = 24
OVERLAY_TRANSPARENT_KEY = "magenta"

# F16 Bluey states, retargeted by F21 (child-side drawing only - no
# job-logic touch, no added headless latency): press tap on target
# arrival, fading trail, coordinate bubble near the pointer. The old
# head's hover-grow and widen/flatten squish retired with it; the
# pointer taps through the press curve alone.
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
OVERLAY_RING_LEFT_FILL = OVERLAY_EYE_FILL        # primary: LED pink
OVERLAY_RING_RIGHT_FILL = OVERLAY_HEAD_OUTLINE   # secondary: slate trim

# Item-7 face: the face shows what the agent is doing. Kinds are
# child-side only; the parent sends working/searching/error/happy/idle
# plus F22 listening/speaking and the child derives sleeping from quiet.
# Wire payloads carry no durations - the child owns every timer, like
# the effect verb. Voice states are display-only and expire on their own
# (a crashed tab must not leave the face listening forever); the normal
# browser pagehide/idle signal clears them immediately.
FACE_STATES = ("idle", "working", "searching", "listening", "speaking",
               "happy", "error", "sleeping")
FACE_WIRE_STATES = ("idle", "working", "searching", "listening",
                    "speaking", "happy", "error")
FACE_PERSISTENT_STATES = ("working", "searching")
FACE_VOICE_STATES = ("listening", "speaking")
OVERLAY_FACE_HAPPY_SECONDS = 1.2
OVERLAY_FACE_ERROR_SECONDS = 1.8
OVERLAY_FACE_VOICE_SECONDS = 300.0
OVERLAY_FACE_SLEEP_AFTER_SECONDS = 60.0
OVERLAY_FACE_BLINK_PERIOD_SECONDS = 4.4
OVERLAY_FACE_BLINK_SECONDS = 0.16
OVERLAY_FACE_BLINK_CLOSE = 0.86
OVERLAY_FACE_SWEEP_RATE = 2.0
OVERLAY_FACE_SWEEP_PX = 5.0
OVERLAY_FACE_WORK_EYE_SCALE = (0.95, 0.55)
OVERLAY_FACE_WORK_EYE_DY = -11.0
OVERLAY_FACE_LISTEN_EYE_SCALE = (1.18, 1.22)
OVERLAY_FACE_LISTEN_EYE_DY = -7.0
OVERLAY_FACE_SPEAK_EYE_SCALE = (0.96, 0.86)
OVERLAY_FACE_SPEAK_EYE_DY = -7.0
OVERLAY_FACE_MOUTH_W = 7.0
OVERLAY_FACE_MOUTH_DY = 20.0
OVERLAY_FACE_MOUTH_MIN = 1.2
OVERLAY_FACE_MOUTH_MAX = 5.0
OVERLAY_FACE_MOUTH_RATE = 8.0
OVERLAY_FACE_ARC_EYE_RY = 6.0
OVERLAY_FACE_SLEEP_EYE_DY = -4.0
OVERLAY_FACE_X_EYE_R = 6.5
OVERLAY_FACE_EYE_STROKE = 2.6

# IPC verbs the child accepts from the parent's stdin pipe. Unknown
# lines are dropped by _stdin_reader, so a new verb MUST be listed here
# or it is silently ignored (Item-7 added "face").
_INBOUND_TYPES = ("target", "point", "home", "speaking", "effect",
                  "settle", "quit", "face")

# Win32 GetSystemMetrics indices (verified live on Windows - never
# trusted from memory, per the F09 vtable lesson).
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1
_SPI_GETWORKAREA = 0x0030
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


def effect_anchor(grid, vrect: tuple) -> tuple[int, int] | None:
    """Click-effect anchor: grid point to virtual-screen pixels (pure).

    Item-4b: the click ring + coordinate bubble anchor at the clicked
    point (Bluey's point-anchored clickEffect), never at the live
    pointer - it is already gliding home when a late effect lands.
    ``None`` for a malformed grid/vrect; never raises.
    """
    try:
        gx, gy = grid
        ox, oy, width, height = vrect
        checked = _choreo_grid(gx, gy)
        if checked is None:
            return None
        px, py = grid_to_pixel(checked[0], checked[1],
                               (ox, oy, width, height))
        return (int(px), int(py))
    except (TypeError, ValueError):
        return None


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


def eye_offset(hx, hy, cx, cy, r_max: float) -> tuple[int, int]:
    """Pupil offset from face center toward a point (pure, hermetic).

    The full vector when the target is near, clamped to ``r_max``
    magnitude when far - the face's eyes glance toward the pointer or
    the mouse without leaving the face. ``(0, 0)`` when coincident or
    ``r_max <= 0``.
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


def _face_number(value) -> float | None:
    """A real finite float, or None for junk/bools (pure, never raises)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def face_kind(face, now, *, quiet_since=None) -> str:
    """Resolve the face to draw from the child's single-slot state (pure).

    ``face`` is ``{"kind": str, "until": float | None}``: the persistent
    kinds (``working``/``searching``) hold until replaced, the bursts
    (``happy``/``error``) expire into ``idle``, and a burst simply
    replaces a persistent kind - the runner runs one job at a time, so
    there is nothing to rank. F22 voice states hold only until their
    stamp (a crashed tab's last signal must not stick) and then fall
    back to idle/sleeping. ``sleeping`` is derived, never sent: it needs
    a real ``quiet_since`` monotonic stamp (no stamp, no sleep) and only
    wins when nothing else is active. Junk in any argument reads as
    ``idle``; never raises.
    """
    kind = None
    until = None
    if isinstance(face, dict):
        raw = face.get("kind")
        if isinstance(raw, str) and raw in FACE_STATES:
            kind = raw
        until = face.get("until")
    if kind in FACE_PERSISTENT_STATES:
        return kind
    if kind in FACE_VOICE_STATES:
        now_t = _face_number(now)
        until_t = _face_number(until)
        if now_t is not None and until_t is not None and now_t < until_t:
            return kind
    if kind in ("happy", "error"):
        now_t = _face_number(now)
        until_t = _face_number(until)
        if now_t is not None and until_t is not None and now_t < until_t:
            return kind
    now_t = _face_number(now)
    quiet_t = _face_number(quiet_since)
    if (now_t is not None and quiet_t is not None
            and now_t - quiet_t >= OVERLAY_FACE_SLEEP_AFTER_SECONDS):
        return "sleeping"
    return "idle"


def face_geometry(kind, now, blink=0.0) -> dict:
    """Per-state eye geometry in px against the face centre (pure).

    Values are base-scale (head-relative): the child draws them
    multiplied by ``OVERLAY_FACE_EYE_SCALE``. The idle values ARE the
    frozen eye constants (``OVERLAY_EYE_DX`` /
    ``OVERLAY_EYE_DY`` / ``OVERLAY_EYE_R``), so the resting face is
    byte-identical to the drawing that shipped before Item-7; ``track``
    says whether the cursor glance (``eye_offset``) still applies.
    ``eyes`` picks the item shape the child draws: ``oval`` (as today),
    ``arc_top`` (happy), ``arc_bottom`` (sleeping) or ``x`` (error).
    ``mouth`` is ``None`` except for the speaking pose, where it carries
    a pulsing open-mouth geometry. ``blink`` (0 open … 1 shut) only
    squashes the oval. Unknown kinds and junk times fall back to the
    resting face; never raises.
    """
    radius = float(OVERLAY_EYE_R)
    geom = {
        "eyes": "oval",
        "dx": float(OVERLAY_EYE_DX),
        "dy": float(OVERLAY_EYE_DY),
        "rx": radius,
        "ry": radius,
        "sweep": 0.0,
        "track": True,
        "stroke": float(OVERLAY_FACE_EYE_STROKE),
        "mouth": None,
    }
    if kind == "working":
        scale_x, scale_y = OVERLAY_FACE_WORK_EYE_SCALE
        geom["rx"] = radius * float(scale_x)
        geom["ry"] = radius * float(scale_y)
        geom["dy"] = float(OVERLAY_FACE_WORK_EYE_DY)
    elif kind == "searching":
        now_t = _face_number(now)
        if now_t is not None:
            geom["sweep"] = math.sin(
                now_t * OVERLAY_FACE_SWEEP_RATE) * OVERLAY_FACE_SWEEP_PX
    elif kind == "listening":
        scale_x, scale_y = OVERLAY_FACE_LISTEN_EYE_SCALE
        geom["rx"] = radius * float(scale_x)
        geom["ry"] = radius * float(scale_y)
        geom["dy"] = float(OVERLAY_FACE_LISTEN_EYE_DY)
    elif kind == "speaking":
        scale_x, scale_y = OVERLAY_FACE_SPEAK_EYE_SCALE
        geom["rx"] = radius * float(scale_x)
        geom["ry"] = radius * float(scale_y)
        geom["dy"] = float(OVERLAY_FACE_SPEAK_EYE_DY)
        now_t = _face_number(now)
        phase = (0.5 if now_t is None
                 else 0.5 + 0.5 * math.sin(
                     now_t * OVERLAY_FACE_MOUTH_RATE))
        geom["mouth"] = {
            "w": float(OVERLAY_FACE_MOUTH_W),
            "h": float(OVERLAY_FACE_MOUTH_MIN)
            + (float(OVERLAY_FACE_MOUTH_MAX)
               - float(OVERLAY_FACE_MOUTH_MIN)) * phase,
            "dy": float(OVERLAY_FACE_MOUTH_DY),
        }
    elif kind == "happy":
        geom["eyes"] = "arc_top"
        geom["ry"] = float(OVERLAY_FACE_ARC_EYE_RY)
        geom["track"] = False
    elif kind == "sleeping":
        geom["eyes"] = "arc_bottom"
        geom["ry"] = float(OVERLAY_FACE_ARC_EYE_RY)
        geom["dy"] = float(OVERLAY_FACE_SLEEP_EYE_DY)
        geom["track"] = False
    elif kind == "error":
        cross = float(OVERLAY_FACE_X_EYE_R)
        geom["eyes"] = "x"
        geom["rx"] = cross
        geom["ry"] = cross
        geom["track"] = False
    if geom["eyes"] == "oval":
        closed = _face_number(blink)
        if closed is not None:
            closed = min(1.0, max(0.0, closed))
            geom["ry"] = geom["ry"] * (
                1.0 - OVERLAY_FACE_BLINK_CLOSE * closed)
    return geom


def face_blink(now) -> float:
    """Eyelid closure 0 (open) … 1 (shut) for the oval eyes (pure).

    One short blink per ``OVERLAY_FACE_BLINK_PERIOD_SECONDS``: shut then
    open inside ``OVERLAY_FACE_BLINK_SECONDS`` (a sine, so the lid eases
    both ways), open the rest of the time. Junk times read as open;
    never raises.
    """
    period = float(OVERLAY_FACE_BLINK_PERIOD_SECONDS)
    window = float(OVERLAY_FACE_BLINK_SECONDS)
    now_t = _face_number(now)
    if now_t is None or period <= 0 or window <= 0:
        return 0.0
    phase = now_t % period
    if not 0.0 <= phase < window:
        return 0.0
    return min(1.0, max(0.0, math.sin(math.pi * (phase / window))))


def face_apply(wire_state, now) -> dict | None:
    """The slot one wire ``face`` message produces, or None for junk (pure).

    ``working``/``searching`` hold (``until`` None) until the next face
    message, ``happy``/``error`` stamp their burst's end on the child's
    clock, F22 voice states get a stale-signal watchdog, and ``idle``
    clears with an already-expired stamp. Anything outside
    ``FACE_WIRE_STATES`` (including the child-derived ``sleeping``) and
    junk times return None so the caller keeps the slot it had; never
    raises.
    """
    if not isinstance(wire_state, str) or wire_state not in FACE_WIRE_STATES:
        return None
    now_t = _face_number(now)
    if now_t is None:
        return None
    if wire_state == "happy":
        until = now_t + OVERLAY_FACE_HAPPY_SECONDS
    elif wire_state == "error":
        until = now_t + OVERLAY_FACE_ERROR_SECONDS
    elif wire_state in FACE_VOICE_STATES:
        until = now_t + OVERLAY_FACE_VOICE_SECONDS
    elif wire_state in FACE_PERSISTENT_STATES:
        until = None
    else:  # idle clears
        until = now_t
    return {"kind": wire_state, "until": until}


def dock_corner(vrect: tuple, size_px: int,
                corner: str = OVERLAY_DOCK_CORNER,
                margin: int = OVERLAY_DOCK_MARGIN,
                lift: int = OVERLAY_DOCK_LIFT) -> tuple[int, int]:
    """Top-left pixel of a ``size_px`` box in the dock corner (pure).

    Bottom corners ride ``lift`` px above the rect's bottom (default
    ``OVERLAY_DOCK_LIFT``, sized to clear the Windows taskbar when the
    rect is the full desktop); top corners are unchanged. When the
    rect is the primary work area the taskbar is already excluded and
    the caller passes the smaller ``OVERLAY_WORKAREA_LIFT``.
    ``size_px`` was named ``head_px`` before F21; all in-repo callers
    pass it positionally.
    """
    ox, oy, width, height = vrect
    try:
        size = max(1, int(size_px))
    except (TypeError, ValueError):
        size = 1
    try:
        pad = max(0, int(margin))
    except (TypeError, ValueError):
        pad = 0
    try:
        rise = max(0, int(lift))
    except (TypeError, ValueError):
        rise = OVERLAY_DOCK_LIFT
    spots = {
        "br": (ox + width - pad - size,
               oy + height - pad - size - rise),
        "bl": (ox + pad, oy + height - pad - size - rise),
        "tr": (ox + width - pad - size, oy + pad),
        "tl": (ox + pad, oy + pad),
    }
    picked = spots.get(str(corner).lower(), spots["br"])
    return (int(picked[0]), int(picked[1]))


def face_box(vrect: tuple,
             lift: int = OVERLAY_DOCK_LIFT) -> tuple[int, int]:
    """Top-left pixel of the companion face's box (pure, hermetic).

    The face lives in the bottom-right dock spot (``lift`` above the
    rect's bottom, ``OVERLAY_DOCK_MARGIN`` from the edges) shifted
    ``OVERLAY_FACE_SHIFT_LEFT`` px left so Windows notification toasts
    keep the true corner. Malformed rects read as ``(0, 0)``; never
    raises.
    """
    try:
        x, y = dock_corner(vrect, OVERLAY_FACE_PX, lift=lift)
    except (TypeError, ValueError):
        return (0, 0)
    return (int(x) - OVERLAY_FACE_SHIFT_LEFT, int(y))


def pointer_home(vrect: tuple,
                 lift: int = OVERLAY_DOCK_LIFT) -> tuple[float, float]:
    """Parked pointer TIP position: beside the face's left edge (pure).

    The tip is the hotspot, so "parked" is the tip of the pointer box
    whose right edge sits ``OVERLAY_PARK_GAP`` px left of the face,
    vertically centred against it. Malformed rects park near the
    origin; never raises.
    """
    try:
        fx, fy = face_box(vrect, lift)
        width = float(OVERLAY_POINTER_W)
        height = float(OVERLAY_POINTER_PX)
        return (float(fx) - OVERLAY_PARK_GAP - width,
                float(fy) + (OVERLAY_FACE_PX - height) / 2.0)
    except (TypeError, ValueError):
        return (0.0, 0.0)


def pointer_at_home(px, py, vrect: tuple, tolerance: float = 1.0,
                    lift: int = OVERLAY_DOCK_LIFT) -> bool:
    """Whether a pointer position is the parked spot (pure, hermetic).

    F21 hover follow-up: "home" for the hover bob means the pointer is
    actually parked beside the face - not merely "not flying to a
    target". A settled tour/click point still bobs gently; the parked
    pointer stays still. Junk reads as ``False``; never raises.
    """
    try:
        hx, hy = pointer_home(vrect, lift)
        reach = float(tolerance)
        return (math.isfinite(reach) and reach >= 0
                and abs(float(px) - hx) <= reach
                and abs(float(py) - hy) <= reach)
    except (TypeError, ValueError):
        return False


def squircle_points(cx, cy, rx, ry, samples: int = 32,
                    power: float = 3.6) -> list:
    """Superellipse ring sampled clockwise (pure, hermetic).

    The web face's shell is a ``bmax`` squircle; Tk has no squircle,
    so the child draws one as a smooth polygon through ``samples``
    points of ``|x/rx|^p + |y/ry|^p = 1`` (``power`` 3.6 reads as the
    port's corner preset - fuller than an ellipse at the diagonals).
    Non-positive radii or junk read as ``[]``; never raises.
    """
    try:
        fcx, fcy = float(cx), float(cy)
        frx, fry = float(rx), float(ry)
        count = int(samples)
        exponent_power = float(power)
    except (TypeError, ValueError):
        return []
    if not all(math.isfinite(v) for v in (fcx, fcy, frx, fry)):
        return []
    # Validate the exponent source BEFORE dividing: power=0 used to
    # escape as ZeroDivisionError under a never-raises contract.
    if (frx <= 0 or fry <= 0 or count < 3
            or not math.isfinite(exponent_power) or exponent_power <= 0):
        return []
    exponent = 2.0 / exponent_power
    count = min(count, 256)
    points = []
    for i in range(count):
        angle = 2.0 * math.pi * i / count
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        points.append((
            fcx + frx * math.copysign(abs(cos_a) ** exponent, cos_a),
            fcy + fry * math.copysign(abs(sin_a) ** exponent, sin_a)))
    return points


def pointer_arrow_points(px, py, w: float = OVERLAY_POINTER_W,
                         h: float = OVERLAY_POINTER_PX) -> list:
    """Classic up-left cursor polygon with the tip AT (px, py) (pure).

    The tip is the hotspot: the body (notch + tail) extends down-right
    of it exactly like a real arrow cursor, so a flight landing on a
    target pixel puts the tip - not a centre - on the point. Junk and
    non-positive sizes give ``[]``; never raises.
    """
    try:
        fx, fy = float(px), float(py)
        width, height = float(w), float(h)
    except (TypeError, ValueError):
        return []
    if not all(math.isfinite(v) for v in (fx, fy, width, height)):
        return []
    if width <= 0 or height <= 0:
        return []
    shape = ((0.00, 0.00), (0.00, 0.60), (0.20, 0.46), (0.38, 0.97),
             (0.56, 0.89), (0.38, 0.40), (0.98, 0.34))
    return [(fx + sx * width, fy + sy * height) for sx, sy in shape]


def pointer_face_parts(px, py, w: float = OVERLAY_POINTER_W,
                       h: float = OVERLAY_POINTER_PX) -> dict:
    """The pointer's robot face in px, tip-anchored (pure, hermetic).

    The reference cursor carries two pink eyes (the right one winking
    through a short slash) and a zigzag mouth on its navy body.
    Returns ``{"eyes": [(cx, cy, r)] * 2, "wink": [(x, y)] * 2,
    "mouth": [(x, y)] * 5}``; everything scales linearly from the tip,
    so a pressed pointer keeps its face aligned. Junk and non-positive
    sizes give ``{}``; never raises.
    """
    try:
        fx, fy = float(px), float(py)
        width, height = float(w), float(h)
    except (TypeError, ValueError):
        return {}
    if not all(math.isfinite(v) for v in (fx, fy, width, height)):
        return {}
    if width <= 0 or height <= 0:
        return {}
    ratio = width / float(OVERLAY_POINTER_W)
    radius = float(OVERLAY_POINTER_EYE_R) * ratio
    eye_y = fy + 0.185 * height
    left_x = fx + 0.20 * width
    right_x = fx + 0.38 * width
    return {
        "eyes": [(left_x, eye_y, radius), (right_x, eye_y, radius)],
        "wink": [(right_x - 1.2 * radius, eye_y - 0.6 * radius),
                 (right_x + 1.2 * radius, eye_y + 0.6 * radius)],
        "mouth": [(fx + 0.24 * width, fy + 0.300 * height),
                  (fx + 0.33 * width, fy + 0.355 * height),
                  (fx + 0.42 * width, fy + 0.300 * height),
                  (fx + 0.51 * width, fy + 0.355 * height),
                  (fx + 0.60 * width, fy + 0.300 * height)],
    }


def face_bounce_offset(kind, now, face_at) -> float:
    """Happy-hop vertical offset in px for the face (pure, hermetic).

    ``happy`` bursts get two quick hops over the burst window (the
    same ``OVERLAY_FACE_HAPPY_SECONDS`` the eyes use), easing down a
    touch; the offset is never positive (screen-up is negative y in
    Tk). Every other kind, expired stamps and junk read as ``0.0``;
    never raises.
    """
    if kind != "happy":
        return 0.0
    now_t = _face_number(now)
    at_t = _face_number(face_at)
    if now_t is None or at_t is None:
        return 0.0
    t = now_t - at_t
    if t < 0 or t >= OVERLAY_FACE_HAPPY_SECONDS:
        return 0.0
    phase = t / OVERLAY_FACE_HAPPY_SECONDS
    return -OVERLAY_FACE_BOUNCE_PX * abs(math.sin(2.0 * math.pi * phase)) \
        * (1.0 - 0.25 * phase)


def face_gaze_target(pointer_travelling, pointer_xy, mouse_xy) -> tuple:
    """Where the face looks: the travelling pointer, else the mouse (pure).

    F21's both rule: while the pointer flies the face follows it; once
    it rests (parked beside the face or holding a point) the face goes
    back to watching the operator's real mouse. Malformed pointer
    coordinates fall through to the mouse; junk everywhere reads
    ``(0.0, 0.0)``; never raises.
    """
    try:
        if pointer_travelling is True:
            return (float(pointer_xy[0]), float(pointer_xy[1]))
    except (TypeError, ValueError, IndexError, KeyError):
        pass
    try:
        return (float(mouse_xy[0]), float(mouse_xy[1]))
    except (TypeError, ValueError, IndexError, KeyError):
        return (0.0, 0.0)


def trail_push(trail: list, x: float, y: float,
               cap: int = OVERLAY_TRAIL_POINTS) -> list:
    """Append one pointer position, oldest-first, capped (pure).

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

    Bluey's ``point()``: the spot joins the queue and the pointer
    works through it when the hold expires — tools never wait on it.
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
    """Flag a trip home once the pointer is quiet (pure, never raises)."""
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
    """Drop the queued tour + home request: a real action flies now
    (pure, Item-1c; home flag per Bluey's takeControl, Item-4b).

    ``announce()`` (a click/drag point) preempts the visual tour —
    the pointer goes where the action lands instead. ``home_requested``
    clears too: without it a real action after a home request would
    immediately re-home once it lands. Never raises.
    """
    with contextlib.suppress(AttributeError, TypeError):
        state["queue"].clear()
        state["home_requested"] = False


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


def shell_shades() -> tuple[str, str, str]:
    """Light→mid→deep fills for the stacked-oval gradient (pure)."""
    return (OVERLAY_SHELL_LIGHT, OVERLAY_SHELL_MID, OVERLAY_SHELL_DEEP)


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


def canvas_rect(vrect: tuple) -> tuple[int, int, int, int]:
    """Canvas-space rect for the overlay window (pure, hermetic).

    The Tk window covers the virtual desktop at ``+ox+oy``, so canvas
    coordinates are window-relative while every pixel helper returns
    absolute virtual-screen pixels. Drawing must hand those helpers
    THIS rect: same width/height, origin at ``(0, 0)``. On a single
    monitor the two rects are identical (a strict no-op); on a
    multi-monitor desktop it folds the origin back out. Malformed
    rects read ``(0, 0, 0, 0)``; never raises.
    """
    try:
        _ox, _oy, width, height = vrect
        return (0, 0, int(width), int(height))
    except (TypeError, ValueError):
        return (0, 0, 0, 0)


def screen_to_canvas(px, py, vrect: tuple) -> tuple[float, float]:
    """Absolute virtual-screen point in canvas coordinates (pure).

    Cursor polling is absolute; the child's canvas is window-relative,
    so every gaze comparison needs this shift. Junk reads
    ``(0.0, 0.0)``; never raises.
    """
    try:
        ox, oy = float(vrect[0]), float(vrect[1])
        return (float(px) - ox, float(py) - oy)
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0)


def shift_rect(rect: tuple, vrect: tuple) -> tuple[int, int, int, int]:
    """Absolute ``(x, y, w, h)`` rect into canvas space (pure).

    Folds the virtual-screen origin out of ``rect``'s corner, same as
    ``screen_to_canvas`` does for a point. Malformed input reads
    ``(0, 0, 0, 0)``; never raises.
    """
    try:
        x, y, w, h = rect
        return (int(x - vrect[0]), int(y - vrect[1]), int(w), int(h))
    except (TypeError, ValueError, IndexError, OverflowError):
        return (0, 0, 0, 0)


def dock_area(vrect: tuple, work=None) -> tuple[tuple, int]:
    """Rect the companion face docks into, plus its lift (pure).

    ``work`` is the primary monitor's work area ``(left, top, right,
    bottom)`` in absolute virtual-screen pixels (see ``work_area``).
    A usable work area (positive size, inside the desktop) wins: the
    face then sits on a real screen, above the real taskbar, with the
    small ``OVERLAY_WORKAREA_LIFT``. Missing, junk, degenerate or
    out-of-desktop values fall back to the old bounding-box dock
    (``vrect`` with ``OVERLAY_DOCK_LIFT``). Never raises.
    """
    try:
        ox, oy, vw, vh = (int(v) for v in vrect)
    except (TypeError, ValueError, OverflowError):
        return (vrect, OVERLAY_DOCK_LIFT)
    full = (ox, oy, vw, vh)
    try:
        left, top, right, bottom = (int(v) for v in work)
    except (TypeError, ValueError, OverflowError):
        return (full, OVERLAY_DOCK_LIFT)
    width, height = right - left, bottom - top
    inside = (left >= ox and top >= oy
              and right <= ox + vw and bottom <= oy + vh)
    if width <= 0 or height <= 0 or not inside:
        return (full, OVERLAY_DOCK_LIFT)
    return ((left, top, width, height), OVERLAY_WORKAREA_LIFT)


def work_area() -> tuple[int, int, int, int] | None:
    """Primary monitor work area ``(left, top, right, bottom)``.

    Screen minus the taskbar, in absolute virtual-screen pixels
    (SPI_GETWORKAREA). ``None`` off Windows or on any failure - the
    caller then falls back to the bounding-box dock. Never raises.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not ctypes.windll.user32.SystemParametersInfoW(
                _SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return None
        return (int(rect.left), int(rect.top),
                int(rect.right), int(rect.bottom))
    except Exception:
        return None


def _dpi_opt_in() -> bool:
    """Whether the opt-in physical-pixel switch is on (never raises)."""
    try:
        from invincible.core.settings import settings
        return bool(settings.overlay_dpi_aware())
    except Exception:
        return False


def _enable_dpi_awareness() -> None:
    """Best-effort: make this process report PHYSICAL pixels.

    Must run before the first window exists. Tries per-monitor v2,
    then per-monitor, then system-aware; each failure is swallowed so
    a missing API just leaves the overlay as it was. Never raises.
    """
    if os.name != "nt":
        return
    import ctypes

    with contextlib.suppress(Exception):
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    with contextlib.suppress(Exception):
        # PROCESS_PER_MONITOR_DPI_AWARE == 2; 0 is S_OK
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    with contextlib.suppress(Exception):
        ctypes.windll.user32.SetProcessDPIAware()


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
        """Glide the pointer to a resolved 0-1000 grid point. Never raises.

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
        """Queue a tour point; the pointer catches up visually. Never raises.

        Bluey's ``point()``: appends to the child queue and clears any
        home request — tools run immediately, the pointer glides through
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
        """Ask the pointer home once quiet (sets the flag, never raises)."""
        self._send({"type": "home"})

    def set_speaking(self, on: bool) -> None:
        """Track reply speech: homing pauses while true (never raises).

        ``on`` must be a real bool — anything else sends False, like
        ``confirm_action`` treats non-true approve as deny.
        """
        self._send({"type": "speaking", "on": on is True})

    def face(self, state: str) -> None:
        """Show what the agent is doing on the face (never raises).

        ``state`` is one of ``FACE_WIRE_STATES``: ``working`` and
        ``searching`` hold until the next face message, ``listening``
        and ``speaking`` hold until their stale-signal watchdog expires,
        ``happy`` and ``error`` run their own burst on the child's
        clock, ``idle`` clears. Unknown states are dropped, like every
        other malformed payload - the job's result stands regardless.
        """
        if not isinstance(state, str) or state not in FACE_WIRE_STATES:
            return
        self._send({"type": "face", "state": state})

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
        """Ask the pointer to dock now (idle timeout docks anyway)."""
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
                _INBOUND_TYPES):
            with contextlib.suppress(queue.Full):
                inbox.put_nowait(message)
    # Parent death: the quit MUST land even when the inbox is full -
    # a silently dropped quit would leave the overlay running forever
    # with no parent. Make room by dropping the oldest unread message.
    try:
        inbox.put_nowait({"type": "quit"})
    except queue.Full:
        with contextlib.suppress(queue.Empty):
            inbox.get_nowait()
        with contextlib.suppress(queue.Full):
            inbox.put_nowait({"type": "quit"})


def _destroy_window(root) -> None:
    """Close the overlay window on quit; never raises.

    ``destroy`` + ``quit`` are both suppressed: a teardown hiccup must
    not throw the tick loop dead, which would leave a frozen window
    and an orphaned process behind.
    """
    with contextlib.suppress(Exception):
        root.destroy()
    with contextlib.suppress(Exception):
        root.quit()


def overlay_main() -> None:
    """Child-process entry: own the Tk window, read stdin commands.

    Exits quietly (code 0, headless) on every failure path - off
    Windows, no tkinter, no display, or a lost click-through flag
    (fail closed: an overlay that could intercept clicks must never
    stay up).
    """
    if os.name != "nt":
        return
    if _dpi_opt_in():
        _enable_dpi_awareness()
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
    # F21 follow-up: the window spans the virtual desktop at +ox+oy,
    # so Tk canvas coordinates are window-relative. Every pixel helper
    # below returns ABSOLUTE virtual-screen pixels; drawing through
    # ``crect`` folds the origin out. Single monitor: crect == vrect
    # (a strict no-op).
    crect = canvas_rect(vrect)
    # Dock the face on the PRIMARY monitor's work area, not the corner
    # of the whole desktop (that corner can be empty space between
    # monitors of different sizes). ``drect`` is canvas-space like
    # ``crect``; with no usable work area it equals ``crect`` and
    # ``dock_lift`` is the old ``OVERLAY_DOCK_LIFT`` (strict no-op).
    dock_abs, dock_lift = dock_area(vrect, work_area())
    drect = shift_rect(dock_abs, vrect)
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

    # F21: the face sits in the bottom-right dock box (shifted left);
    # the pointer parks beside it, and its TIP - the hotspot - is what
    # every flight targets.
    face_x, face_y = face_box(drect, dock_lift)
    face_r = OVERLAY_FACE_PX / 2.0
    home = pointer_home(drect, dock_lift)
    pointer_xy = [float(home[0]), float(home[1])]
    _, mid, deep = shell_shades()
    # F20 glow, F21 shape: a faint dithered squircle band plus a crisp
    # LED rim hugging the face's shell (a circular glow crossed the
    # squircle's corners).
    glow = canvas.create_polygon(-10, -10, -10, -10, -10, -10,
                                 fill=OVERLAY_GLOW_FILL, outline="",
                                 width=0, smooth=True)
    with contextlib.suppress(Exception):
        canvas.itemconfigure(glow, stipple="gray25")
    glow_ring = canvas.create_polygon(-10, -10, -10, -10, -10, -10,
                                      fill="", outline=OVERLAY_GLOW_FILL,
                                      width=2, smooth=True)
    if not OVERLAY_GLOW:
        canvas.coords(glow, -10, -10, -10, -10, -10, -10)
        canvas.coords(glow_ring, -10, -10, -10, -10, -10, -10)
    # F20/F21: the trail draws UNDER both pieces. With no aging, a
    # settled pointer's trail collapses onto its own art; underneath
    # it only reads as motion. It must also stay under the companion
    # face, or a crossing flight smears dots across the face.
    trail_ids = [
        canvas.create_oval(-10, -10, -10, -10,
                           fill=OVERLAY_HEAD_OUTLINE,
                           outline=OVERLAY_HEAD_OUTLINE)
        for _ in range(OVERLAY_TRAIL_POINTS)
    ]
    face_shell = canvas.create_polygon(0, 0, 1, 1, 2, 2, fill=mid,
                                       outline=OVERLAY_HEAD_OUTLINE,
                                       width=3, smooth=True)
    face_plate = canvas.create_polygon(0, 0, 1, 1, 2, 2, fill=deep,
                                       outline=OVERLAY_HEAD_OUTLINE,
                                       width=2, smooth=True)
    shine = canvas.create_oval(0, 0, 1, 1, fill=OVERLAY_SHINE_FILL,
                               outline=OVERLAY_SHINE_FILL)
    # F20/F21: the face's eyes are LED blocks (rectangles); a stippled
    # bloom sits behind each block so they read as lit.
    eye_bloom_l = canvas.create_oval(0, 0, 1, 1, fill=OVERLAY_GLOW_FILL,
                                     outline="")
    eye_bloom_r = canvas.create_oval(0, 0, 1, 1, fill=OVERLAY_GLOW_FILL,
                                     outline="")
    for bloom in (eye_bloom_l, eye_bloom_r):
        with contextlib.suppress(Exception):
            canvas.itemconfigure(bloom, stipple="gray25")
    eye_l = canvas.create_rectangle(0, 0, 1, 1, fill=OVERLAY_EYE_FILL,
                                    outline=OVERLAY_EYE_FILL)
    eye_r = canvas.create_rectangle(0, 0, 1, 1, fill=OVERLAY_EYE_FILL,
                                    outline=OVERLAY_EYE_FILL)
    # F22: one LED mouth for the speaking pose; listening/idle park it.
    mouth_lamp = canvas.create_oval(0, 0, 1, 1, fill=OVERLAY_EYE_FILL,
                                    outline=OVERLAY_EYE_FILL)
    # Item-7 face: six extra items stacked right above the blocks - one
    # arc per eye (happy lids / sleeping) and two X strokes per eye
    # (error). Idle leaves them parked off-window.
    eye_stroke = int(round(OVERLAY_FACE_EYE_STROKE
                           * OVERLAY_FACE_EYE_SCALE))
    eye_arc_ids = [
        canvas.create_arc(-10, -10, -10, -10, start=0, extent=180,
                          style="arc", outline=OVERLAY_EYE_FILL,
                          width=eye_stroke)
        for _ in range(2)
    ]
    eye_cross_ids = [
        canvas.create_line(-10, -10, -10, -10, fill=OVERLAY_EYE_FILL,
                           width=eye_stroke, capstyle="round")
        for _ in range(4)
    ]
    # F21 pointer: a classic up-left cursor carrying the pink robot
    # face; every coordinate draws relative to the tip (the hotspot).
    pointer_poly = canvas.create_polygon(0, 0, 1, 1, 2, 2,
                                         fill=mid,
                                         outline=OVERLAY_HEAD_OUTLINE,
                                         width=OVERLAY_POINTER_STROKE)
    pointer_eye_l = canvas.create_oval(0, 0, 1, 1, fill=OVERLAY_EYE_FILL,
                                       outline=OVERLAY_EYE_FILL)
    pointer_eye_r = canvas.create_oval(0, 0, 1, 1, fill=OVERLAY_EYE_FILL,
                                       outline=OVERLAY_EYE_FILL)
    pointer_wink = canvas.create_line(-10, -10, -10, -10,
                                      fill=OVERLAY_EYE_FILL,
                                      width=OVERLAY_POINTER_FACE_STROKE,
                                      capstyle="round")
    pointer_mouth = canvas.create_line(-10, -10, -10, -10, -10, -10,
                                       -10, -10, -10, -10,
                                       fill=OVERLAY_EYE_FILL,
                                       width=OVERLAY_POINTER_FACE_STROKE,
                                       capstyle="round")
    bubble_box = canvas.create_rectangle(-10, -10, -10, -10,
                                         fill=OVERLAY_SHELL_MID,
                                         outline=OVERLAY_HEAD_OUTLINE)
    bubble_label = canvas.create_text(-10, -10, text="",
                                      fill=OVERLAY_EYE_FILL,
                                      font=("Segoe UI", 9))
    click_ring = canvas.create_oval(-10, -10, -10, -10,
                                    outline=OVERLAY_RING_LEFT_FILL,
                                    width=3)
    glyph_ids = [
        canvas.create_text(-10, -10, text="",
                           fill=OVERLAY_EYE_FILL,
                           font=("Segoe UI", 18, "bold"))
        for _ in range(OVERLAY_GLYPH_CAP)
    ]
    inbox: queue.Queue = queue.Queue(maxsize=16)
    reader = threading.Thread(target=_stdin_reader, args=(inbox,),
                              daemon=True)
    reader.start()

    def _poll_cursor() -> tuple[float, float]:
        """Absolute cursor px in canvas space (window sits at +ox+oy)."""
        raw_x, raw_y = cursor_pos()
        return screen_to_canvas(raw_x, raw_y, vrect)

    try:
        cursor = _poll_cursor()
    except OSError:
        cursor = (width / 2.0, height / 2.0)
    steps = max(2, int(round(
        OVERLAY_GLIDE_SECONDS / OVERLAY_POLL_SECONDS)))
    now0 = time.monotonic()
    state = {"path": [], "to_target": False,
             "trail": [],
             "bubble_until": 0.0, "bubble_text": "",
             "bubble_xy": None, "flight": None, "velocity": [0.0, 0.0],
             "landed_at": now0 - 10.0,
             "press_start": now0 - 10.0, "press_depth": 0.0,
             "dragging": False, "choreo": choreo_state(now0),
             "face": None, "face_at": now0,
             "ring": None, "glyphs": []}

    def _dock() -> None:
        target = pointer_home(drect, dock_lift)
        state["to_target"] = False
        state["flight"] = None
        state["velocity"] = [0.0, 0.0]
        state["dragging"] = False
        state["path"] = ease_waypoints(
            pointer_xy[0], pointer_xy[1],
            float(target[0]), float(target[1]), steps)

    def _launch(gx, gy, label, dragging,
                bubble_seconds: float) -> None:
        now_child = time.monotonic()
        px, py = grid_to_pixel(gx, gy, crect)
        state["flight"] = plan_flight(
            (pointer_xy[0], pointer_xy[1]),
            (float(px), float(py)),
            tuple(state["velocity"]),
            now_child, crect, dragging)
        state["path"] = []
        state["to_target"] = True
        state["dragging"] = dragging
        state["bubble_xy"] = None
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
        _spawn_ring(pointer_xy[0], pointer_xy[1], OVERLAY_LAND_RING_PX,
                    OVERLAY_LAND_RING_SECONDS, OVERLAY_RING_LEFT_FILL)

    def _effect(message: dict) -> None:
        now_fx = time.monotonic()
        kind = message.get("kind")
        if kind == "click":
            grid = _choreo_grid(message.get("x"), message.get("y"))
            if grid is None:
                return
            anchor = effect_anchor(grid, crect)
            if anchor is None:
                return
            state["press_start"] = now_fx
            state["press_depth"] = OVERLAY_PRESS_CLICK
            tint = (OVERLAY_RING_RIGHT_FILL
                    if message.get("button") == "right"
                    else OVERLAY_RING_LEFT_FILL)
            # Item-4b: ring + bubble anchor at the clicked point, never
            # the live pointer (it may be gliding home already).
            _spawn_ring(anchor[0], anchor[1], OVERLAY_CLICK_RING_PX,
                        OVERLAY_CLICK_RING_SECONDS, tint)
            label = bubble_text_for(*grid)
            if label:
                state["bubble_text"] = label
                state["bubble_until"] = (
                    now_fx + OVERLAY_CLICK_BUBBLE_SECONDS)
                state["bubble_xy"] = (float(anchor[0]),
                                      float(anchor[1]))
        elif kind == "type":
            chars = effect_glyph_chars(message.get("text", ""))
            if not chars:
                return
            state["press_start"] = now_fx
            state["press_depth"] = OVERLAY_PRESS_TYPE
            base_x = pointer_xy[0] + OVERLAY_POINTER_W * 0.6
            base_y = pointer_xy[1] - 6.0
            state["glyphs"] = [
                {"x": base_x + i * 8.0, "y": base_y,
                 "char": ch, "start": now_fx}
                for i, ch in enumerate(chars)]

    def _draw() -> None:
        fcx = face_x + face_r
        fcy = face_y + face_r
        px, py = pointer_xy
        now = time.monotonic()
        press = 1.0
        with contextlib.suppress(Exception):
            ps = press_scale(now, state["press_start"],
                             state["press_depth"])
            if math.isfinite(ps) and ps > 0:
                press = ps
        settled = state["flight"] is None and not state["path"]
        # F21 hover follow-up: "home" means the pointer is actually
        # parked beside the face, not merely "not flying". A settled
        # tour/click point still bobs; the parked pointer is still.
        is_home = settled and pointer_at_home(px, py, drect, lift=dock_lift)
        if settled and not is_home:
            with contextlib.suppress(Exception):
                bob = hover_offset(now, state["landed_at"],
                                   is_home=is_home, settled=settled)
                if math.isfinite(bob):
                    py = py + bob
        # F21 face: the state + blink + happy hop resolve on the child's
        # own clock, exactly like the pre-split head (Item-7); the hop
        # lifts the whole drawn face.
        face = "idle"
        with contextlib.suppress(Exception):
            face = face_kind(state["face"], now,
                             quiet_since=state["face_at"])
        # F22 watchdog: an expired speaking face must also release the
        # homing pause, otherwise a crashed tab leaves the pointer parked
        # forever even though the mouth has already gone idle.
        if (face != "speaking"
                and state["choreo"].get("speaking") is True):
            with contextlib.suppress(Exception):
                choreo_set_speaking(state["choreo"], False, now)
        blink = 0.0
        with contextlib.suppress(Exception):
            blink = face_blink(now)
        geom = face_geometry(face, now, blink)
        with contextlib.suppress(Exception):
            fcy = fcy + face_bounce_offset(face, now, state["face_at"])
        if OVERLAY_GLOW:
            gpts = squircle_points(
                fcx, fcy, face_r * OVERLAY_FACE_GLOW_SCALE,
                face_r * OVERLAY_FACE_GLOW_SCALE)
            if gpts:
                canvas.coords(glow, *[c for p in gpts for c in p])
            rpts = squircle_points(
                fcx, fcy, face_r * OVERLAY_FACE_RING_SCALE,
                face_r * OVERLAY_FACE_RING_SCALE)
            if rpts:
                canvas.coords(glow_ring, *[c for p in rpts for c in p])
        shell = squircle_points(fcx, fcy, face_r, face_r)
        if shell:
            canvas.coords(face_shell, *[c for p in shell for c in p])
        plate = squircle_points(fcx, fcy, face_r * OVERLAY_FACE_PLATE_SCALE,
                                face_r * OVERLAY_FACE_PLATE_SCALE)
        if plate:
            canvas.coords(face_plate, *[c for p in plate for c in p])
        sr = face_r * 0.11
        canvas.coords(shine, fcx - face_r * 0.55 - sr,
                      fcy - face_r * 0.66 - sr,
                      fcx - face_r * 0.55 + sr,
                      fcy - face_r * 0.66 + sr)
        # F21 gaze: the travelling pointer first, the real mouse while
        # it rests (the parked pointer is not "looking" at anything).
        travel = state["flight"] is not None or bool(state["path"])
        gx, gy = face_gaze_target(travel, (px, py), cursor)
        gaze = eye_offset(fcx, fcy, gx, gy,
                          OVERLAY_EYE_TRACK_R * OVERLAY_FACE_EYE_SCALE)
        track = gaze if geom["track"] else (0, 0)
        scale = OVERLAY_FACE_EYE_SCALE
        parked = (-10, -10, -10, -10)
        for oval, bloom, arc, cross_a, cross_b, side in (
                (eye_l, eye_bloom_l, eye_arc_ids[0], eye_cross_ids[0],
                 eye_cross_ids[1], -1),
                (eye_r, eye_bloom_r, eye_arc_ids[1], eye_cross_ids[2],
                 eye_cross_ids[3], 1)):
            ecx = fcx + side * geom["dx"] * scale \
                + (geom["sweep"] * scale + track[0])
            ecy = fcy + geom["dy"] * scale + track[1]
            erx = geom["rx"] * scale
            ery = geom["ry"] * scale
            box = (ecx - erx, ecy - ery,
                   ecx + erx, ecy + ery)
            bm = max(erx, ery) * 1.30
            bloom_box = (ecx - bm, ecy - bm, ecx + bm, ecy + bm)
            if geom["eyes"] == "oval":
                canvas.coords(oval, *box)
                canvas.coords(bloom, *bloom_box)
                canvas.coords(arc, *parked)
                canvas.coords(cross_a, *parked)
                canvas.coords(cross_b, *parked)
            elif geom["eyes"] == "x":
                canvas.coords(cross_a, box[0], box[1], box[2], box[3])
                canvas.coords(cross_b, box[0], box[3], box[2], box[1])
                canvas.coords(oval, *parked)
                canvas.coords(bloom, *parked)
                canvas.coords(arc, *parked)
            else:
                with contextlib.suppress(Exception):
                    canvas.itemconfigure(arc,
                                         start=(0 if geom["eyes"] == "arc_top"
                                                else 180),
                                         extent=180)
                canvas.coords(arc, *box)
                canvas.coords(oval, *parked)
                canvas.coords(bloom, *parked)
                canvas.coords(cross_a, *parked)
                canvas.coords(cross_b, *parked)
        # F22: the speaking mouth pulses open/closed under the eyes.
        mouth = geom.get("mouth") if isinstance(geom, dict) else None
        mouth_box = None
        if isinstance(mouth, dict):
            with contextlib.suppress(Exception):
                mw = float(mouth["w"]) * scale
                mh = float(mouth["h"]) * scale
                mdy = float(mouth["dy"]) * scale
                mouth_box = (fcx - mw, fcy + mdy - mh,
                             fcx + mw, fcy + mdy + mh)
        canvas.coords(mouth_lamp, *(mouth_box if mouth_box is not None
                                    else parked))
        trail = state["trail"]
        for i, dot in enumerate(trail_ids):
            if i < len(trail):
                tx, ty = trail[i]
                size = 1.2 + 2.4 * (i + 1) / max(len(trail), 1)
                canvas.coords(dot, tx - size, ty - size,
                              tx + size, ty + size)
            else:
                canvas.coords(dot, -10, -10, -10, -10)
        # F21 pointer: the arrow + ink scale about the tip by the press
        # factor, so the hotspot never drifts mid-tap.
        pw = OVERLAY_POINTER_W * press
        ph = OVERLAY_POINTER_PX * press
        apts = pointer_arrow_points(px, py, pw, ph)
        if apts:
            canvas.coords(pointer_poly, *[c for p in apts for c in p])
        parts = pointer_face_parts(px, py, pw, ph)
        eyes = parts.get("eyes") if parts else None
        if eyes:
            for item, (exx, eyy, err) in zip(
                    (pointer_eye_l, pointer_eye_r), eyes, strict=False):
                canvas.coords(item, exx - err, eyy - err,
                              exx + err, eyy + err)
        else:
            for item in (pointer_eye_l, pointer_eye_r):
                canvas.coords(item, -10, -10, -10, -10)
        wink = parts.get("wink") if parts else None
        if wink:
            canvas.coords(pointer_wink, wink[0][0], wink[0][1],
                          wink[1][0], wink[1][1])
        else:
            canvas.coords(pointer_wink, -10, -10, -10, -10)
        mouth = parts.get("mouth") if parts else None
        if mouth:
            canvas.coords(pointer_mouth, *[c for p in mouth for c in p])
        else:
            canvas.coords(pointer_mouth, *([-10, -10] * 5))
        if now < state["bubble_until"] and state["bubble_text"]:
            label = state["bubble_text"]
            # Item-4b: a click bubble keeps its anchor (the clicked
            # point) while the pointer travels; tour/land bubbles have
            # bubble_xy None and follow the pointer.
            bx, by = px, py
            anchor_xy = state["bubble_xy"]
            if isinstance(anchor_xy, tuple) and len(anchor_xy) == 2:
                with contextlib.suppress(Exception):
                    bx = float(anchor_xy[0])
                    by = float(anchor_xy[1])
            top = by - OVERLAY_POINTER_PX - 26
            half = min(120.0, 8.0 + len(label) * 3.6)
            canvas.coords(bubble_box, bx - half, top - 11,
                          bx + half, top + 11)
            canvas.coords(bubble_label, bx, top)
            canvas.itemconfigure(bubble_label, text=label)
        else:
            state["bubble_xy"] = None
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
                    # Item-4b: the ring stays at its spawn point (the
                    # clicked point; land rings spawn at the pointer).
                    canvas.coords(click_ring,
                                  ring["x"] - frame[0],
                                  ring["y"] - frame[0],
                                  ring["x"] + frame[0],
                                  ring["y"] + frame[0])
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
                    _destroy_window(root)
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
                elif kind == "face":
                    with contextlib.suppress(Exception):
                        now_face = time.monotonic()
                        applied = face_apply(message.get("state"),
                                             now_face)
                        if applied is not None:
                            state["face"] = applied
                            # only real face messages reset the quiet
                            # clock (sleeping is "no work for a while")
                            state["face_at"] = now_face
                elif kind == "effect":
                    with contextlib.suppress(Exception):
                        _effect(message)
        except queue.Empty:
            pass
        with contextlib.suppress(OSError):
            cursor = _poll_cursor()
        if state["flight"] is not None:
            with contextlib.suppress(Exception):
                flight = state["flight"]
                now = time.monotonic()
                duration = float(flight["duration"])
                if duration <= 0:
                    pointer_xy[0], pointer_xy[1] = flight["to"]
                    state["flight"] = None
                    state["velocity"] = [0.0, 0.0]
                    state["dragging"] = False
                    state["landed_at"] = now
                    if state["to_target"]:
                        state["to_target"] = False
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
                    pointer_xy[0], pointer_xy[1] = pos
                    if p >= 1:
                        pointer_xy[0], pointer_xy[1] = flight["to"]
                        state["flight"] = None
                        state["velocity"] = [0.0, 0.0]
                        state["dragging"] = False
                        state["landed_at"] = now
                        if state["to_target"]:
                            state["to_target"] = False
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
            pointer_xy[0], pointer_xy[1] = state["path"].pop(0)
            if not state["path"]:
                state["to_target"] = False
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
                home_now = pointer_home(drect, dock_lift)
                if (pointer_xy[0], pointer_xy[1]) != home_now:
                    with contextlib.suppress(Exception):
                        _dock()
                with contextlib.suppress(Exception):
                    choreo_go_home(state["choreo"])
        state["trail"] = trail_push(
            state["trail"], pointer_xy[0], pointer_xy[1])
        _draw()
        root.after(int(OVERLAY_POLL_SECONDS * 1000), _tick)

    _draw()
    _tick()
    root.mainloop()
