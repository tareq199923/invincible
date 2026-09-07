# invincible/endpoints/template_filters.py
"""Shared Jinja filters for the server-rendered dashboard.

All three Jinja2Templates instances (accounts, dashboard, main/landing)
register these through register_template_filters so every page sees the
same presentation helpers. Filters are presentation-only: they never
touch stored values, they just render them.
"""
import time

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR
_MONTH = 30 * _DAY


def timeago(value) -> str:
    """Render an epoch-seconds timestamp as a compact relative age.

    Falls back to "-" for missing values and to the raw value for
    anything non-numeric (projection payloads carry both shapes).
    """
    if value is None or value == "":
        return "-"
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return str(value)
    delta = time.time() - ts
    if delta < 0:
        return "just now"
    if delta < _MINUTE:
        return "just now" if delta < 5 else f"{int(delta)}s ago"
    if delta < _HOUR:
        return f"{int(delta // _MINUTE)}m ago"
    if delta < _DAY:
        return f"{int(delta // _HOUR)}h ago"
    if delta < _MONTH:
        return f"{int(delta // _DAY)}d ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def absdate(value) -> str:
    """Machine-checkable absolute timestamp for title attributes next to
    timeago's relative ages (hovering "3d ago" shows the real moment)."""
    if value is None or value == "":
        return ""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return str(value)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def compactnum(value) -> str:
    """Render a count with k/M suffixes for stat cards (45213 -> 45.2k)."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n < 1000:
        return f"{sign}{int(n)}"
    if n < 1_000_000:
        return f"{sign}{n / 1_000:.1f}k"
    return f"{sign}{n / 1_000_000:.1f}M"


def register_template_filters(templates) -> None:
    """Attach the shared filter set to a Jinja2Templates instance."""
    templates.env.filters["timeago"] = timeago
    templates.env.filters["absdate"] = absdate
    templates.env.filters["compactnum"] = compactnum
