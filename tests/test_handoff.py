# tests/test_handoff.py
"""B2 phone→PC handoff: fire-and-forget control messages (not tool jobs).

Hermetic where possible (FakeClock/FakeWS, no network/mic); the
endpoint + phone-page tests ride the full-app ``client`` fixture like
the other agent-surface tests. Golden/drift suites must stay green
untouched — handoff adds zero tool-registry surface by design.
"""
import asyncio

import pytest

from invincible.agent import runner as agent_runner
from invincible.core.agent_registry import (
    HANDOFF_CONTEXT_MAX_CHARS,
    HANDOFF_TASK_MAX_CHARS,
    MAX_HANDOFFS_PER_USER,
    AgentRegistry,
)
from invincible.core.identity import ApiKeyStore
from invincible.main import app
from tests.conftest import register_account


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWS:
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


# --- registry: queue semantics -------------------------------------------


async def test_post_take_round_trip_fifo(registry):
    reg, _ = registry
    first = reg.post_handoff(42, "task-a", "ctx-a", "phone")
    reg.post_handoff(42, "task-b")
    assert reg.pending_handoffs(42) == 2
    got_first = reg.take_handoff(42)
    got_second = reg.take_handoff(42)
    assert got_first["handoff_id"] == first["handoff_id"]
    assert got_first["handoff"]["task"] == "task-a"
    assert got_first["handoff"]["context"] == "ctx-a"
    assert got_first["handoff"]["from_device"] == "phone"
    assert got_second["handoff"]["task"] == "task-b"
    assert reg.take_handoff(42) is None
    assert reg.pending_handoffs(42) == 0


async def test_handoff_isolated_per_user(registry):
    reg, _ = registry
    reg.post_handoff(1, "for-user-1")
    assert reg.take_handoff(2) is None
    assert reg.pending_handoffs(2) == 0
    got = reg.take_handoff(1)
    assert got is not None and got["handoff"]["task"] == "for-user-1"


async def test_handoff_queue_bounded_oldest_first(registry):
    reg, _ = registry
    for i in range(MAX_HANDOFFS_PER_USER + 5):
        reg.post_handoff(7, f"task-{i}")
    assert reg.pending_handoffs(7) == MAX_HANDOFFS_PER_USER
    oldest = reg.take_handoff(7)
    assert oldest["handoff"]["task"] == "task-5"


async def test_drop_handoff_prevents_double_delivery(registry):
    reg, _ = registry
    envelope = reg.post_handoff(42, "hop")
    assert await reg.push_ws(42, {"type": "x"}) is False  # nobody attached
    reg.drop_handoff(42, envelope["handoff_id"])
    assert reg.take_handoff(42) is None
    reg.drop_handoff(42, "missing-id")  # never raises
    reg.drop_handoff(99, "missing-id")  # unknown user: never raises


async def test_post_handoff_wakes_a_held_poll(registry):
    """post_handoff sets the shared event so a WS-less PC's held poll
    answers fast (job None) and the endpoint then drains the handoff."""
    reg, _ = registry
    held = asyncio.ensure_future(reg.poll(42, hold=5))
    await asyncio.sleep(0.01)  # let the poll park
    reg.post_handoff(42, "wake me")
    job = await asyncio.wait_for(held, timeout=1)
    assert job is None
    got = reg.take_handoff(42)
    assert got is not None and got["handoff"]["task"] == "wake me"


async def test_user_holding_handoff_is_never_pruned(registry):
    reg, clock = registry
    reg.post_handoff(7, "queued-hop")
    clock.advance(reg.PRUNE_INTERVAL_SECONDS + 1)
    clock.advance(reg.STALE_USER_SECONDS + 1)
    reg.heartbeat(99)  # another user triggers the sweep
    got = reg.take_handoff(7)
    assert got is not None and got["handoff"]["task"] == "queued-hop"


async def test_ws_first_push_delivers_handoff_to_one_socket(registry):
    reg, _ = registry
    ws_a, ws_b = FakeWS(), FakeWS()
    reg.attach_ws(42, ws_a)
    reg.attach_ws(42, ws_b)
    envelope = reg.post_handoff(42, "hop-task", "ctx", "phone")
    sent = await reg.push_ws(
        42,
        {"type": "handoff", "handoff": envelope["handoff"],
         "handoff_id": envelope["handoff_id"]},
    )
    assert sent is True
    total = len(ws_a.sent) + len(ws_b.sent)
    assert total == 1
    delivered = (ws_a.sent or ws_b.sent)[0]
    assert delivered["type"] == "handoff"
    assert delivered["handoff"]["task"] == "hop-task"
    # endpoint drops the queued copy on relay success: same call here
    reg.drop_handoff(42, envelope["handoff_id"])
    assert reg.take_handoff(42) is None


# --- runner: arrival never breaks jobs ------------------------------------


def test_handle_handoff_ignores_malformed_never_raises(monkeypatch):
    monkeypatch.setattr(agent_runner, "ensure_overlay",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("must not be called")))
    for bad in (None, "task", 42, [], {"no_task": 1}):
        agent_runner.handle_handoff(bad)  # must not raise


def test_handle_handoff_wakes_overlay_best_effort(monkeypatch):
    settled = []

    class FakeOverlay:
        alive = True

        def settle(self):
            settled.append(True)

    monkeypatch.setattr(agent_runner, "ensure_overlay",
                        lambda: FakeOverlay())
    agent_runner.handle_handoff({"task": "open the report", "context": ""})
    assert settled == [True]


def test_handle_handoff_dead_overlay_is_silent(monkeypatch):
    class DeadOverlay:
        alive = False

        def settle(self):
            raise AssertionError("headless must not settle")

    monkeypatch.setattr(agent_runner, "ensure_overlay",
                        lambda: DeadOverlay())
    agent_runner.handle_handoff({"task": "hi"})  # must not raise


def test_handle_handoff_overlay_exception_swallowed(monkeypatch):
    def _boom():
        raise RuntimeError("no desktop")

    monkeypatch.setattr(agent_runner, "ensure_overlay", _boom)
    agent_runner.handle_handoff({"task": "hi"})  # must not raise


# --- settings kill-switch ---------------------------------------------------


def test_handoff_settings_defaults(monkeypatch):
    from invincible.core.settings import settings

    monkeypatch.delenv("INVINCIBLE_HANDOFF", raising=False)
    assert settings.handoff_enabled() is True
    monkeypatch.setenv("INVINCIBLE_HANDOFF", "0")
    assert settings.handoff_enabled() is False
    monkeypatch.setenv("INVINCIBLE_HANDOFF", "off")
    assert settings.handoff_enabled() is False


# --- endpoints ---------------------------------------------------------------


def agent_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _mint_key(client, email="handoff-user@example.com"):
    made, _ = await register_account(client, email=email)
    uid = int(made.json()["id"])
    record = await ApiKeyStore(app.state.engine).create(uid, label="phone")
    return uid, record["raw"]


async def test_handoff_requires_inv_key(client):
    assert (await client.post("/agent/handoff",
                              json={"task": "hi"})).status_code == 401
    assert (await client.post(
        "/agent/handoff", headers=agent_headers("inv_garbage"),
        json={"task": "hi"})).status_code == 401


async def test_handoff_post_then_poll_fallback(client, monkeypatch):
    import invincible.endpoints.agents as agents_mod

    monkeypatch.setattr(agents_mod, "AGENT_POLL_HOLD_SECONDS", 0.05)
    uid, key = await _mint_key(client)
    posted = await client.post(
        "/agent/handoff", headers=agent_headers(key),
        json={"task": "open the report", "context": "q3 numbers",
              "from_device": "phone"},
    )
    assert posted.status_code == 200
    body = posted.json()
    assert body["queued"] is True
    assert body["relayed"] is False  # nobody attached: poll fallback
    assert body["handoff_id"]
    polled = await client.post("/agent/poll", headers=agent_headers(key))
    assert polled.status_code == 200
    payload = polled.json()
    assert payload["job"] is None
    assert payload["handoff"]["handoff"]["task"] == "open the report"
    assert payload["handoff"]["handoff"]["context"] == "q3 numbers"
    assert payload["handoff"]["handoff"]["from_device"] == "phone"
    # fire-and-forget: second poll finds nothing
    again = await client.post("/agent/poll", headers=agent_headers(key))
    assert again.json() == {"job": None}


async def test_poll_shape_unchanged_without_handoff(client, monkeypatch):
    """Job-only consumers keep their exact {"job": ...} shape."""
    import invincible.endpoints.agents as agents_mod

    monkeypatch.setattr(agents_mod, "AGENT_POLL_HOLD_SECONDS", 0.05)
    _, key = await _mint_key(client, "shape@example.com")
    response = await client.post("/agent/poll", headers=agent_headers(key))
    assert response.json() == {"job": None}


async def test_handoff_cross_user_isolated(client, monkeypatch):
    import invincible.endpoints.agents as agents_mod

    monkeypatch.setattr(agents_mod, "AGENT_POLL_HOLD_SECONDS", 0.05)
    _, key_a = await _mint_key(client, "hop-a@example.com")
    _, key_b = await _mint_key(client, "hop-b@example.com")
    posted = await client.post(
        "/agent/handoff", headers=agent_headers(key_a),
        json={"task": "mine"},
    )
    assert posted.status_code == 200
    foreign = await client.post("/agent/poll", headers=agent_headers(key_b))
    assert foreign.json() == {"job": None}
    mine = await client.post("/agent/poll", headers=agent_headers(key_a))
    assert mine.json()["handoff"]["handoff"]["task"] == "mine"


async def test_handoff_ws_first_relayed(client, monkeypatch):
    """Attached socket gets it; the poll copy is dropped (never twice)."""
    import invincible.endpoints.agents as agents_mod

    monkeypatch.setattr(agents_mod, "AGENT_POLL_HOLD_SECONDS", 0.05)
    uid, key = await _mint_key(client, "ws-first@example.com")
    ws = FakeWS()
    app.state.agent_registry.attach_ws(uid, ws)
    try:
        posted = await client.post(
            "/agent/handoff", headers=agent_headers(key),
            json={"task": "hop now"},
        )
        assert posted.json()["relayed"] is True
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "handoff"
        assert ws.sent[0]["handoff"]["task"] == "hop now"
        polled = await client.post(
            "/agent/poll", headers=agent_headers(key))
        assert polled.json() == {"job": None}
    finally:
        app.state.agent_registry.detach_ws(uid, ws)


async def test_handoff_validation(client):
    _, key = await _mint_key(client, "valid@example.com")
    blank = await client.post(
        "/agent/handoff", headers=agent_headers(key), json={"task": "  "})
    assert blank.status_code == 400
    missing = await client.post(
        "/agent/handoff", headers=agent_headers(key), json={})
    assert missing.status_code == 400
    long_task = await client.post(
        "/agent/handoff", headers=agent_headers(key),
        json={"task": "x" * (HANDOFF_TASK_MAX_CHARS + 1)})
    assert long_task.status_code == 400
    long_ctx = await client.post(
        "/agent/handoff", headers=agent_headers(key),
        json={"task": "ok",
              "context": "y" * (HANDOFF_CONTEXT_MAX_CHARS + 1)})
    assert long_ctx.status_code == 400
    bad_ctx = await client.post(
        "/agent/handoff", headers=agent_headers(key),
        json={"task": "ok", "context": {"not": "a string"}})
    assert bad_ctx.status_code == 400


async def test_handoff_kill_switch(client, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_HANDOFF", "0")
    _, key = await _mint_key(client, "kill@example.com")
    refused = await client.post(
        "/agent/handoff", headers=agent_headers(key),
        json={"task": "hi"},
    )
    assert refused.status_code == 503


# --- phone page --------------------------------------------------------------


async def test_phone_page_requires_session(client):
    response = await client.get("/dashboard/phone")
    assert response.status_code == 401


async def test_phone_page_renders_shell(client):
    await register_account(client, email="phone-page@example.com")
    response = await client.get("/dashboard/phone")
    assert response.status_code == 200
    text = response.text
    assert "Hey Baymax" in text
    assert "phone-hop" in text
    assert "baymax-dot" in text
    assert "phone.webmanifest" in text
    # secret-safety: the page takes the key as user input, never renders one
    assert 'id="phone-key"' in text
    assert 'value="inv_' not in text
