# invincible/agent/overlay.py
"""B1 head-cursor overlay (F11): a Bmax-head cursor on the paired PC.

A transparent, click-through, always-on-top window draws an original
soft-white rounded head with two ink eyes (no Disney likeness - the
canvas echo of the ``.baymax-dot`` CSS concept in
``invincible/templates/static/app.css``). It glides to each Pack 3
target point BEFORE the action executes (so the user watches Bmax
"do" the click/type), idles docked at a screen corner, and its eyes
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

# Motion: ~30Hz cursor poll, ease-out glide, idle dock, pre-action dwell
# (the dwell lives in the runner - it only waits when an overlay is
# alive, so headless jobs pay zero added latency).
OVERLAY_POLL_SECONDS = 1.0 / 30.0
OVERLAY_GLIDE_SECONDS = 0.30
OVERLAY_IDLE_SETTLE_SECONDS = 2.0
OVERLAY_PRE_ACTION_DWELL_SECONDS = 0.35
OVERLAY_DOCK_MARGIN = 24
OVERLAY_DOCK_CORNER = "br"
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
    """Top-left pixel of the idle-docked head box (pure, hermetic)."""
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
        "br": (ox + width - pad - size, oy + height - pad - size),
        "bl": (ox + pad, oy + height - pad - size),
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

    Best-effort by contract: ``start``/``announce``/``settle`` never
    raise, and ``alive`` is False whenever there is no window (off
    Windows, spawn failure, dead child). Jobs check ``alive`` and skip
    the pre-action dwell when it is False.
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

    def announce(self, point: dict) -> None:
        """Glide the head to a resolved 0-1000 grid point. Never raises.

        Malformed points are ignored (the job already validated its
        own args - the overlay never second-guesses execution).
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
        self._send({"type": "target", "x": int(x), "y": int(y)})

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
                "target", "settle", "quit"):
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
    state = {"path": [], "idle_since": now0, "to_target": False,
             "trail": [], "squash_until": 0.0,
             "bubble_until": 0.0, "bubble_text": ""}

    def _draw() -> None:
        hx, hy = head_xy
        now = time.monotonic()
        squished = now < state["squash_until"]
        hovering = bool(state["path"]) and bool(state["to_target"])
        rx, ry = head_radii(radius, hovering=hovering,
                            squished=squished)
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
        if pts:
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
                    target = (dock[0] + radius, dock[1] + radius)
                    state["to_target"] = False
                    state["path"] = ease_waypoints(
                        head_xy[0], head_xy[1],
                        float(target[0]), float(target[1]), steps)
                elif kind == "target":
                    with contextlib.suppress(Exception):
                        px, py = grid_to_pixel(
                            message.get("x"), message.get("y"), vrect)
                        state["path"] = bowed_waypoints(
                            head_xy[0], head_xy[1],
                            float(px), float(py), steps)
                        state["to_target"] = True
                        state["idle_since"] = time.monotonic()
                        label = bubble_text_for(
                            message.get("x"), message.get("y"))
                        if label:
                            state["bubble_text"] = label
                            state["bubble_until"] = (
                                time.monotonic()
                                + OVERLAY_GLIDE_SECONDS
                                + OVERLAY_BUBBLE_SECONDS)
        except queue.Empty:
            pass
        with contextlib.suppress(OSError):
            cursor = cursor_pos()
        if state["path"]:
            head_xy[0], head_xy[1] = state["path"].pop(0)
            if not state["path"]:
                now = time.monotonic()
                state["idle_since"] = now
                if state["to_target"]:
                    state["to_target"] = False
                    state["squash_until"] = now + OVERLAY_SQUISH_SECONDS
        elif time.monotonic() - state["idle_since"] \
                >= OVERLAY_IDLE_SETTLE_SECONDS:
            home = (float(dock[0] + radius), float(dock[1] + radius))
            if (head_xy[0], head_xy[1]) != home:
                state["to_target"] = False
                state["path"] = ease_waypoints(
                    head_xy[0], head_xy[1], home[0], home[1], steps)
        state["trail"] = trail_push(
            state["trail"], head_xy[0], head_xy[1])
        _draw()
        root.after(int(OVERLAY_POLL_SECONDS * 1000), _tick)

    _draw()
    _tick()
    root.mainloop()
