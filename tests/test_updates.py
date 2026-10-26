"""Hermetic tests for invincible.core.updates (no network, tmp cache)."""
import json
import time

from invincible.core import updates


def test_parse_version_rejects_junk():
    assert updates.parse_version("0.6.1") is not None
    assert updates.parse_version("not-a-version") is None
    assert updates.parse_version(None) is None


def test_newest_release_skips_prereleases_by_default():
    payload = {"releases": {"0.5.0": [], "0.6.0": [], "0.7.0b1": []}}
    assert updates.newest_release(payload) == "0.6.0"
    assert updates.newest_release(payload, pre=True) == "0.7.0b1"


def test_newest_release_malformed_payload_is_silent():
    assert updates.newest_release({}) is None
    assert updates.newest_release(None) is None
    assert updates.newest_release({"releases": {"junk": []}}) is None


def test_newest_release_falls_back_to_info():
    payload = {"releases": {}, "info": {"version": "1.2.3"}}
    assert updates.newest_release(payload) == "1.2.3"


def test_newest_release_filters_prerelease_info():
    payload = {"info": {"version": "1.2.3b1"}}
    assert updates.newest_release(payload) is None
    assert updates.newest_release(payload, pre=True) == "1.2.3b1"


def test_cache_round_trip(tmp_path):
    path = tmp_path / "update-check.json"
    now = time.time()
    assert updates.write_cache(str(path), latest="0.6.1",
                               checked_at=now) is True
    assert updates.cached_latest(str(path), now=now + 10) == "0.6.1"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == updates.CACHE_VERSION


def test_cache_expires_and_corrupt_is_ignored(tmp_path):
    path = tmp_path / "update-check.json"
    now = time.time()
    updates.write_cache(str(path), latest="0.6.1", checked_at=now)
    expired = now + updates.DEFAULT_TTL_SECONDS + 1
    assert updates.cached_latest(str(path), now=expired) is None

    path.write_text("{not json", encoding="utf-8")
    assert updates.cached_latest(str(path), now=now) is None


def test_cache_rejects_future_timestamp(tmp_path):
    path = tmp_path / "update-check.json"
    now = time.time()
    updates.write_cache(str(path), latest="0.6.1",
                        checked_at=now + 10_000)
    assert updates.cached_latest(str(path), now=now) is None


def test_write_cache_is_best_effort(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    assert updates.write_cache(
        str(blocker / "cache.json"), latest="0.6.1") is False


def test_check_for_update_uses_fresh_cache_without_fetch(tmp_path):
    path = tmp_path / "update-check.json"
    now = time.time()
    updates.write_cache(str(path), latest="0.6.1", checked_at=now)

    def _boom():
        raise AssertionError("fresh cache must not hit the network")

    found = updates.check_for_update(
        current="0.6.0", fetch=_boom, cache_path=str(path), now=now)
    assert found is not None
    assert found.latest == "0.6.1"
    assert found.from_cache is True


def test_check_for_update_refreshes_stale_cache(tmp_path):
    path = tmp_path / "update-check.json"
    now = time.time()
    calls = []

    def _fetch():
        calls.append(1)
        return {"releases": {"0.7.0": []}}

    found = updates.check_for_update(
        current="0.6.0", fetch=_fetch, cache_path=str(path), now=now)
    assert found is not None
    assert found.latest == "0.7.0"
    assert found.from_cache is False
    assert calls == [1]
    assert updates.cached_latest(str(path), now=now) == "0.7.0"


def test_check_for_update_is_silent_when_current(tmp_path):
    path = tmp_path / "update-check.json"
    now = time.time()
    found = updates.check_for_update(
        current="0.6.0",
        fetch=lambda: {"releases": {"0.6.0": []}},
        cache_path=str(path), now=now)
    assert found is None
    # The check still refreshed the cache for the next 24h.
    assert updates.cached_latest(str(path), now=now) == "0.6.0"


def test_check_for_update_rejects_unparseable_current(tmp_path):
    found = updates.check_for_update(
        current="not-a-version",
        fetch=lambda: {"releases": {"99.0.0": []}},
        cache_path=str(tmp_path / "cache.json"))
    assert found is None


def test_check_for_update_ignores_empty_payload(tmp_path):
    found = updates.check_for_update(
        current="0.6.0", fetch=lambda: {},
        cache_path=str(tmp_path / "cache.json"))
    assert found is None
