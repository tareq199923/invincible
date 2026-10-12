# tests/test_tunnel_registry.py
"""F03 slices 1-2: tunnel registry, caps, framing, port policy.

Hermetic (no Postgres, no sockets): FakeClock drives TTL, tiny settings
knobs drive caps. The twenty acceptance tests in the F03 design note live
here (pure-unit ones) and in tests/test_tunnel_route.py (full-app ones).
"""
import asyncio
import base64
import time

import pytest

from invincible.core.harness_tunnel import (
    MAX_FRAME_BYTES,
    MAX_STREAMS_PER_TUNNEL,
    STREAM_CLOSED,
    TUNNEL_DENIED_PORTS,
    FrameError,
    TunnelCapExceeded,
    TunnelRegistry,
    check_tunnel_host,
    check_tunnel_port,
    decode_data_frame,
    encode_data_frame,
    is_tunnel_frame,
    make_tunnel_token,
    validate_request_head,
    validate_response_head,
)
from invincible.core.tool_executor import ToolBlocked


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def registry():
    clock = FakeClock()
    return TunnelRegistry(clock=clock), clock


# --- port policy (Wall 1 + Wall 2 share this) -------------------------------

ALLOWED_PORTS = [1, 80, 443, 3000, 5173, 8000, 8080, 9000, 65535]


async def test_check_tunnel_port_allows_dev_ports():
    for port in ALLOWED_PORTS:
        assert check_tunnel_port(port) == port
    assert check_tunnel_port("3000") == 3000  # numeric strings coerce
    assert check_tunnel_port(3000.0) == 3000  # integral floats coerce


async def test_check_tunnel_port_denies_infrastructure():
    assert frozenset(
        {22, 3389, 5432, 5433, 3306, 1433, 6379, 27017, 25, 587}
    ) == TUNNEL_DENIED_PORTS
    for port in sorted(TUNNEL_DENIED_PORTS):
        with pytest.raises(ToolBlocked):
            check_tunnel_port(port)


async def test_check_tunnel_port_rejects_shapes():
    for bad in (0, 65536, -1, "abc", "", None, 3.5, True, False,
                "  ", "+3000", "0x50"):
        with pytest.raises(ToolBlocked):
            check_tunnel_port(bad)


async def test_expose_port_non_loopback_host_refused():
    """The tool schema carries no host at all (localhost-only is
    structural); this pins the agent-side backstop before dial."""
    assert check_tunnel_host("127.0.0.1") == "127.0.0.1"
    assert check_tunnel_host("::1") == "::1"
    for bad in ("192.168.1.5", "10.0.0.1", "8.8.8.8", "localhost",
                "example.com", "", "  ", "0.0.0.0"):
        with pytest.raises(ToolBlocked):
            check_tunnel_host(bad)


# --- framing -----------------------------------------------------------------

async def test_frame_round_trip_counts_decoded_bytes():
    raw = b"a" * 100
    frame = encode_data_frame("T", "S", 7, raw)
    assert frame["type"] == "tunnel_data"
    assert frame["seq"] == 7
    # base64 inflates on the wire; the cap counts decoded bytes
    assert len(frame["payload_b64"]) > len(raw)
    assert base64.b64decode(frame["payload_b64"]) == raw
    assert decode_data_frame(frame) == raw


async def test_frame_cap_is_decoded_bytes():
    with pytest.raises(FrameError):
        encode_data_frame("T", "S", 0, b"x" * (MAX_FRAME_BYTES + 1))
    big = {"type": "tunnel_data", "tunnel_id": "T", "stream_id": "S",
           "seq": 0,
           "payload_b64": base64.b64encode(b"y" * (MAX_FRAME_BYTES + 1)
                                           ).decode("ascii")}
    with pytest.raises(FrameError):
        decode_data_frame(big)


async def test_frame_rejects_job_keys_and_bad_shapes():
    good = encode_data_frame("T", "S", 0, b"hi")
    assert is_tunnel_frame(good)
    forged = dict(good, job_id="j1", result={})
    assert not is_tunnel_frame(forged)
    with pytest.raises(FrameError):
        decode_data_frame(forged)
    with pytest.raises(FrameError):
        decode_data_frame({"type": "tunnel_data"})
    with pytest.raises(FrameError):
        decode_data_frame({"type": "tunnel_data", "tunnel_id": "T",
                           "stream_id": "S", "seq": 0,
                           "payload_b64": "!!!not-base64!!!"})
    with pytest.raises(FrameError):
        decode_data_frame({"type": "job", "job": {}})
    assert not is_tunnel_frame({"type": "job", "job": {}})
    assert not is_tunnel_frame({"job_id": "x", "result": {}})
    assert not is_tunnel_frame("nope")


async def test_request_head_validation():
    method, path, headers = validate_request_head({
        "type": "tunnel_request_head", "tunnel_id": "T", "stream_id": "S",
        "method": "get", "path": "/demo?a=1", "headers": {"A": "b"},
    })
    assert (method, path, headers) == ("GET", "/demo?a=1", {"A": "b"})
    for bad in (
        {"type": "tunnel_request_head", "method": "CONNECT",
         "path": "/", "headers": {}},
        {"type": "tunnel_request_head", "method": "GET",
         "path": "no-leading-slash", "headers": {}},
        {"type": "tunnel_request_head", "method": "GET",
         "path": "/", "headers": {"A": 1}},
        {"type": "tunnel_request_head", "method": "GET",
         "path": "/", "headers": {f"h{i}": "v" for i in range(101)}},
        {"type": "tunnel_data"},
    ):
        frame = {"tunnel_id": "T", "stream_id": "S", **bad}
        with pytest.raises(FrameError):
            validate_request_head(frame)


async def test_response_head_validation():
    status, headers = validate_response_head({
        "type": "tunnel_response_head", "tunnel_id": "T", "stream_id": "S",
        "status": 201, "headers": {"Content-Type": "text/plain"},
    })
    assert status == 201
    assert headers == {"Content-Type": "text/plain"}
    for bad_status in (99, 600, "ok", None):
        with pytest.raises(FrameError):
            validate_response_head({
                "type": "tunnel_response_head", "tunnel_id": "T",
                "stream_id": "S", "status": bad_status, "headers": {}})


async def test_tunnel_tokens_unique():
    seen = {make_tunnel_token() for _ in range(100)}
    assert len(seen) == 100
    assert all(len(t) >= 32 for t in seen)


# --- registry: open / lookup / close -----------------------------------------

async def test_open_lookup_close_cycle(registry):
    reg, _ = registry
    token = reg.open(7, 3000)
    record = reg.lookup(token)
    assert record is not None
    assert record["owner"] == 7 and record["port"] == 3000
    assert reg.close(token, 7) is True
    assert reg.lookup(token) is None
    # idempotent: second close answers identically (False)
    assert reg.close(token, 7) is False


async def test_close_is_owner_bound(registry):
    reg, _ = registry
    token = reg.open(7, 3000)
    assert reg.close(token, 8) is False  # no leak, no consume
    assert reg.lookup(token) is not None  # owner unaffected
    assert reg.close(token, 7) is True


async def test_open_revalidates_port_defensively(registry):
    reg, _ = registry
    with pytest.raises(ToolBlocked):
        reg.open(7, 22)


async def test_max_per_user_refused(registry, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_TUNNEL_MAX_PER_USER", "1")
    reg, _ = registry
    first = reg.open(7, 3000)
    with pytest.raises(TunnelCapExceeded):
        reg.open(7, 3001)
    # other users unaffected; closing frees the slot
    other = reg.open(8, 3000)
    assert reg.close(first, 7) is True
    assert reg.open(7, 3001)
    assert reg.close(other, 8) is True


async def test_ttl_expiry_behaves_as_unknown(registry, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_TUNNEL_TTL_SECONDS", "100")
    reg, clock = registry
    token = reg.open(7, 3000)
    assert reg.lookup(token) is not None
    clock.advance(101)
    assert reg.lookup(token) is None  # expired == unknown
    assert reg.close(token, 7) is False  # replay stays dead


# --- caps --------------------------------------------------------------------

async def test_tunnel_bytes_per_second_cap_throttles(monkeypatch):
    """Token-bucket: over-budget admits after one bounded sleep."""
    monkeypatch.setenv("INVINCIBLE_TUNNEL_BYTES_PER_SECOND", "100")
    reg = TunnelRegistry()
    token = reg.open(7, 3000)
    assert await reg.spend(token, 60) is True
    start = time.monotonic()
    assert await reg.spend(token, 60) is True  # 120 > 100: throttles
    elapsed = time.monotonic() - start
    assert 0.9 <= elapsed < 5.0
    assert reg.close(token, 7) is True


async def test_spend_total_cap_ends_stream(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_TUNNEL_TOTAL_BYTES", "10")
    reg = TunnelRegistry()
    token = reg.open(7, 3000)
    assert await reg.spend(token, 6) is True
    assert await reg.spend(token, 6) is False  # 12 > 10: capped
    assert await reg.spend("nope", 1) is False  # unknown: capped


# --- streams -----------------------------------------------------------------

async def test_stream_queue_bounded_and_capped(registry):
    reg, _ = registry
    token = reg.open(7, 3000)
    first = reg.stream_queue(token, "s0")
    assert reg.stream_queue(token, "s0") is first  # same stream: same queue
    for i in range(1, MAX_STREAMS_PER_TUNNEL):
        assert reg.stream_queue(token, f"s{i}") is not None
    assert reg.stream_queue(token, "one-too-many") is None  # stream cap
    assert reg.stream_queue("nope", "s") is None  # unknown: miss


async def test_push_drop_seals_stream(registry):
    reg, _ = registry
    token = reg.open(7, 3000)
    queue = reg.stream_queue(token, "s")
    assert reg.push_to_stream(token, "s", b"hi") is True
    assert queue.get_nowait() == b"hi"
    assert reg.push_to_stream(token, "missing", b"x") is False
    assert reg.push_to_stream("nope", "s", b"x") is False
    reg.drop_stream(token, "s")
    assert queue.get_nowait() is STREAM_CLOSED
    assert reg.push_to_stream(token, "s", b"late") is False


async def test_full_queue_drops_not_blocks(registry):
    reg, _ = registry
    token = reg.open(7, 3000)
    assert reg.stream_queue(token, "s") is not None
    for _ in range(64):
        assert reg.push_to_stream(token, "s", b"x") is True
    assert reg.push_to_stream(token, "s", b"overflow") is False


# --- ack rendezvous + affinity + orphans -------------------------------------

async def test_ack_resolve_and_timeout(registry):
    reg, _ = registry
    token = reg.open(7, 3000)
    ws = object()

    async def _resolve():
        await asyncio.sleep(0.01)
        assert reg.resolve_ack(token, ws, True) is True

    waiter, resolver = await asyncio.gather(
        reg.await_ack(token, timeout=5), _resolve())
    assert waiter == {"ok": True, "error": ""}
    assert reg.socket_for(token) is ws  # pinned to the acking socket
    # no double-resolve
    assert reg.resolve_ack(token, ws, True) is False


async def test_ack_timeout_drops_tunnel(registry):
    reg, _ = registry
    token = reg.open(7, 3000)
    assert await reg.await_ack(token, timeout=0.05) is None
    assert reg.lookup(token) is None  # wedged agent holds no slot
    assert reg.resolve_ack(token, object(), True) is False


async def test_ack_refusal_closes_cleanly(registry):
    reg, _ = registry
    token = reg.open(7, 3000)

    async def _refuse():
        await asyncio.sleep(0.01)
        assert reg.resolve_ack(token, object(), False, "nope") is True

    ack, _ = await asyncio.gather(
        reg.await_ack(token, timeout=5), _refuse())
    assert ack == {"ok": False, "error": "nope"}


async def test_detach_ws_tears_down_only_its_tunnels(registry):
    reg, _ = registry
    ws_a, ws_b = object(), object()
    t1 = reg.open(7, 3000)
    t2 = reg.open(7, 3001)
    assert reg.bind_ws(t1, ws_a) is True
    assert reg.bind_ws(t2, ws_b) is True
    q1 = reg.stream_queue(t1, "s")
    assert reg.detach_ws(ws_a) == 1
    assert reg.lookup(t1) is None
    assert q1.get_nowait() is STREAM_CLOSED  # streams seal
    assert reg.lookup(t2) is not None  # other socket's tunnel lives
    assert reg.detach_ws(object()) == 0


async def test_tunnels_for_is_metadata_only(registry):
    reg, _ = registry
    token = reg.open(7, 3000)
    rows = reg.tunnels_for(7)
    assert rows == [{"port": 3000, "created_at": rows[0]["created_at"],
                     "bytes_total": 0, "open_acked": True}]
    blob = str(rows)
    assert token not in blob  # tokens never appear in listings
    assert reg.tunnels_for(8) == []
