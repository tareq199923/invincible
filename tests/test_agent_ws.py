# tests/test_agent_ws.py
"""H1: WebSocket relay — hermetic (no Postgres, no real sockets).

Covers the WS-first dispatch contract with fake sockets:
attach/detach, cap, exactly-once push, cross-user isolation, dead-socket
sweep, plus the runner's URL/hello helpers.
"""
import asyncio

import pytest

from invincible.agent.runner import ws_url_for
from invincible.core.agent_registry import (
    MAX_WS_PER_USER,
    AgentRegistry,
    PollCapacityExceeded,
)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWS:
    """Minimal Starlette WebSocket stand-in (async send_json only)."""

    def __init__(self, fail: bool = False):
        self.sent: list = []
        self.fail = fail

    async def send_json(self, message: dict) -> None:
        if self.fail:
            raise RuntimeError("dead socket")
        self.sent.append(message)


@pytest.fixture
def registry():
    clock = FakeClock()
    return AgentRegistry(clock=clock), clock


async def test_attach_counts_as_online(registry):
    reg, _ = registry
    assert not reg.online(42)
    reg.attach_ws(42, FakeWS())
    assert reg.online(42)
    assert reg.ws_connected(42)
    assert reg.ws_count(42) == 1


async def test_ws_cap_mirrors_poll_cap(registry):
    reg, _ = registry
    for _ in range(MAX_WS_PER_USER):
        reg.attach_ws(7, FakeWS())
    with pytest.raises(PollCapacityExceeded):
        reg.attach_ws(7, FakeWS())
    # other users unaffected
    reg.attach_ws(8, FakeWS())
    assert reg.ws_count(8) == 1


async def test_detach_never_raises(registry):
    reg, _ = registry
    ws = FakeWS()
    reg.detach_ws(99, ws)  # nothing attached
    reg.attach_ws(99, ws)
    reg.detach_ws(99, ws)
    reg.detach_ws(99, ws)  # idempotent
    assert not reg.ws_connected(99)


async def test_dispatch_pushes_ws_first_and_skips_queue(registry):
    """WS-first: attached socket gets the job, poll queue stays empty, and
    a WS result resolves the dispatcher."""
    reg, _ = registry
    ws = FakeWS()
    reg.attach_ws(42, ws)

    async def dispatcher():
        return await reg.dispatch(
            42, "execute_bash", {"command": "echo hi", "timeout": 30.0},
            timeout=5,
        )

    async def agent():
        await asyncio.sleep(0.05)  # let dispatch push first
        assert len(ws.sent) == 1
        job = ws.sent[0]["job"]
        assert job["type"] == "execute_bash"
        # queue copy was dropped — a concurrent poll finds nothing
        assert await reg.poll(42, hold=0.01) is None
        assert reg.submit_result(
            42, job["job_id"], {"stdout": "hi", "returncode": 0})

    result, _ = await asyncio.gather(dispatcher(), agent())
    assert result == {"stdout": "hi", "returncode": 0}


async def test_dispatch_falls_back_to_poll_without_ws(registry):
    """No socket attached: queue path behaves exactly as before."""
    reg, _ = registry

    async def dispatcher():
        return await reg.dispatch(
            42, "execute_bash", {"command": "echo hi", "timeout": 30.0},
            timeout=5,
        )

    async def agent():
        job = await reg.poll(42, hold=1)
        assert job is not None
        assert reg.submit_result(
            42, job["job_id"], {"stdout": "hi", "returncode": 0})

    result, _ = await asyncio.gather(dispatcher(), agent())
    assert result == {"stdout": "hi", "returncode": 0}


async def test_cross_user_isolation(registry):
    """User 1's WS never receives user 2's job."""
    reg, _ = registry
    ws1 = FakeWS()
    reg.attach_ws(1, ws1)

    async def dispatcher():
        return await reg.dispatch(
            2, "execute_bash", {"command": "echo hi", "timeout": 30.0},
            timeout=5,
        )

    async def agent2():
        job = await reg.poll(2, hold=1)
        assert job is not None
        # wrong-owner WS result is refused (indistinguishable False)
        assert not reg.submit_result(
            1, job["job_id"], {"stdout": "evil", "returncode": 0})
        assert reg.submit_result(
            2, job["job_id"], {"stdout": "hi", "returncode": 0})

    result, _ = await asyncio.gather(dispatcher(), agent2())
    assert result == {"stdout": "hi", "returncode": 0}
    assert ws1.sent == []  # user 1's socket got nothing


async def test_dead_socket_detached_and_poll_takes_over(registry):
    """A broken socket is swept; the queued copy survives for polling."""
    reg, _ = registry
    reg.attach_ws(42, FakeWS(fail=True))

    async def dispatcher():
        return await reg.dispatch(
            42, "execute_bash", {"command": "echo hi", "timeout": 30.0},
            timeout=5,
        )

    async def agent():
        job = await reg.poll(42, hold=1)
        assert job is not None
        assert reg.submit_result(
            42, job["job_id"], {"stdout": "hi", "returncode": 0})

    result, _ = await asyncio.gather(dispatcher(), agent())
    assert result == {"stdout": "hi", "returncode": 0}
    assert not reg.ws_connected(42)  # dead socket detached


async def test_push_sends_to_exactly_one_socket(registry):
    """Two machines attached: one job goes to exactly one of them."""
    reg, _ = registry
    ws_a, ws_b = FakeWS(), FakeWS()
    reg.attach_ws(42, ws_a)
    reg.attach_ws(42, ws_b)
    assert await reg.push_ws(42, {"type": "job", "job": {"x": 1}})
    total = len(ws_a.sent) + len(ws_b.sent)
    assert total == 1


def test_ws_url_for_converts_schemes():
    assert ws_url_for("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/agent/ws"
    assert ws_url_for("https://invincible-ai.me") == (
        "wss://invincible-ai.me/agent/ws")
    assert ws_url_for("https://host/base/") == "wss://host/agent/ws"


def test_machine_id_stable_and_overridable(tmp_path, monkeypatch):
    import os

    from invincible.agent.runner import machine_id

    monkeypatch.setenv("INVINCIBLE_MACHINE_ID", "pinned-1")
    assert machine_id() == "pinned-1"
    monkeypatch.delenv("INVINCIBLE_MACHINE_ID")
    # Redirect home on BOTH platforms: nt expanduser honors USERPROFILE
    # (then HOMEDRIVE+HOMEPATH) while posix honors HOME. Without the HOME
    # override the file lands in the real home dir on Linux/macOS (and CI
    # fails because it is not under tmp_path).
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", "")
    monkeypatch.setenv("HOMEPATH", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    first = machine_id()
    assert first
    # Second call loads the persisted file, not a fresh uuid.
    assert machine_id() == first
    assert os.path.isfile(tmp_path / ".invincible" / "machine_id")


async def test_machine_tracking_reports_online(registry):
    reg, _ = registry
    assert reg.machines_for(42) == []
    reg.update_machine(42, "m1", {
        "machine_name": "laptop", "platform": "win",
        "capabilities": {"chrome": True},
    })
    rows = reg.machines_for(42)
    assert len(rows) == 1
    assert rows[0]["machine_id"] == "m1"
    assert rows[0]["online"] is True
    assert rows[0]["capabilities"] == {"chrome": True}
    assert reg.online(42)  # hello heartbeats


async def test_machine_tracking_goes_stale(registry):
    reg, clock = registry
    reg.update_machine(42, "m1", {})
    clock.advance(61)
    rows = reg.machines_for(42)
    assert rows[0]["online"] is False


async def test_machine_tracking_isolated_and_bounded(registry):
    from invincible.core.agent_registry import MAX_MACHINES_PER_USER

    reg, _ = registry
    reg.update_machine(1, "a", {})
    reg.update_machine(2, "b", {})
    assert [m["machine_id"] for m in reg.machines_for(1)] == ["a"]
    assert [m["machine_id"] for m in reg.machines_for(2)] == ["b"]
    reg.update_machine(1, "", {})  # empty id ignored
    assert len(reg.machines_for(1)) == 1
    for i in range(MAX_MACHINES_PER_USER + 5):
        reg.update_machine(3, f"m{i}", {})
    assert len(reg.machines_for(3)) == MAX_MACHINES_PER_USER


def test_harness_ws_settings_defaults(monkeypatch):
    from invincible.core.settings import settings

    monkeypatch.delenv("INVINCIBLE_HARNESS_WS", raising=False)
    assert settings.harness_ws_enabled() is True
    monkeypatch.setenv("INVINCIBLE_HARNESS_WS", "0")
    assert settings.harness_ws_enabled() is False
    monkeypatch.delenv("INVINCIBLE_HARNESS_WS_HEARTBEAT", raising=False)
    assert settings.harness_ws_heartbeat_seconds() == 20.0
    monkeypatch.setenv("INVINCIBLE_HARNESS_WS_HEARTBEAT", "5")
    assert settings.harness_ws_heartbeat_seconds() == 5.0
