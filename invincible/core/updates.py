# invincible/core/updates.py
"""CLI update awareness: version comparison + a small on-disk cache.

The CLI (``invincible update`` and the one-line "update available" notice)
is the only caller. This module deliberately owns **no env reads and no
network I/O**: the caller injects ``fetch`` (a function returning the PyPI
JSON payload) so ``cli.py`` keeps its documented settings exemption and
tests stay hermetic.

Cache shape (``~/.invincible/update-check.json``)::

    {"version": 1, "checked_at": 1790000000.0, "latest": "0.6.1"}

A fresh cache short-circuits the network call for ``DEFAULT_TTL_SECONDS``.
Every cache write is best effort - a read-only home directory or a
concurrent writer must never break a CLI command.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

CACHE_VERSION = 1
DEFAULT_TTL_SECONDS = 24 * 60 * 60

# Reject a cache timestamp from the future by more than this (clock skew or
# a hand-edited file must not pin a stale "latest" for weeks).
_FUTURE_SKEW_SECONDS = 300.0


@dataclass(frozen=True)
class UpdateCheck:
    """The result of a check that found a release newer than ``current``."""

    current: str
    latest: str
    from_cache: bool


def parse_version(raw: object) -> Version | None:
    """Return a comparable :class:`Version`, or ``None`` for junk input."""
    if raw is None:
        return None
    try:
        return Version(str(raw).strip())
    except (InvalidVersion, TypeError, ValueError):
        return None


def newest_release(payload: object, *, pre: bool = False) -> str | None:
    """Pick the newest release version from a PyPI JSON payload.

    Prereleases and dev releases are ignored unless ``pre`` is true.
    Invalid keys are skipped; an empty/malformed payload returns ``None``
    so callers can stay silent instead of erroring.
    """
    if not isinstance(payload, dict):
        return None
    releases = payload.get("releases")
    candidates: list[Version] = []
    if isinstance(releases, dict):
        for raw in releases:
            parsed = parse_version(raw)
            if parsed is None:
                continue
            if not pre and (parsed.is_prerelease or parsed.is_devrelease):
                continue
            candidates.append(parsed)
    if candidates:
        return str(max(candidates))
    info = payload.get("info")
    if isinstance(info, dict):
        parsed = parse_version(info.get("version"))
        if parsed is not None and (pre or not (
                parsed.is_prerelease or parsed.is_devrelease)):
            return str(parsed)
    return None


def read_cache(path: str) -> dict | None:
    """Read the cache file; ``None`` for missing, unreadable, or corrupt."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_cache(path: str, *, latest: str,
                checked_at: float | None = None) -> bool:
    """Persist one check result. Returns ``False`` instead of raising.

    Uses a same-directory temp file + ``os.replace`` so a crashed writer
    can never leave half-written JSON behind.
    """
    payload = {
        "version": CACHE_VERSION,
        "checked_at": float(time.time() if checked_at is None else checked_at),
        "latest": str(latest),
    }
    directory = os.path.dirname(path)
    temp_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, delete=False) as handle:
            temp_path = handle.name
            json.dump(payload, handle)
        os.replace(temp_path, path)
        temp_path = None
        return True
    except OSError:
        return False
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                os.remove(temp_path)


def cached_latest(path: str, *, now: float | None = None,
                  ttl: float = DEFAULT_TTL_SECONDS) -> str | None:
    """The cached latest version, only when the entry is still fresh."""
    cache = read_cache(path)
    if cache is None:
        return None
    if cache.get("version") != CACHE_VERSION:
        return None
    checked_at = cache.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return None
    current_time = time.time() if now is None else float(now)
    age = current_time - float(checked_at)
    if age < -_FUTURE_SKEW_SECONDS or age > float(ttl):
        return None
    latest = cache.get("latest")
    return str(latest) if parse_version(latest) is not None else None


def check_for_update(*, current: str, fetch, cache_path: str,
                     now: float | None = None,
                     ttl: float = DEFAULT_TTL_SECONDS,
                     pre: bool = False) -> UpdateCheck | None:
    """Return an :class:`UpdateCheck` when a newer release exists.

    A fresh cache avoids ``fetch``. Otherwise ``fetch()`` is called once and
    its result cached. ``fetch`` exceptions propagate - the CLI decides
    whether silence or a message is appropriate.
    """
    current_version = parse_version(current)
    if current_version is None:
        return None

    cached = cached_latest(cache_path, now=now, ttl=ttl)
    if cached is not None:
        latest = cached
        from_cache = True
    else:
        latest = newest_release(fetch(), pre=pre)
        if latest is None:
            return None
        write_cache(cache_path, latest=latest, checked_at=now)
        from_cache = False

    latest_version = parse_version(latest)
    if latest_version is None or latest_version <= current_version:
        return None
    return UpdateCheck(current=str(current), latest=latest,
                       from_cache=from_cache)
