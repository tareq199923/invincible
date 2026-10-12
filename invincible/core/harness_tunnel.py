# invincible/core/harness_tunnel.py
"""Per-user localhost port tunneling (F03/H6b): registry, caps, framing.

Lets a user expose one ``localhost:PORT`` on their *paired* machine through
a public ``/t/{tunnel_token}/...`` URL served by the gateway. The tunnel
itself is opened like any dangerous tool (staged + confirmed through
``PendingActionStore`` with same-subject binding); the bytes then flow
multiplexed over the agent's existing ``WS /agent/ws`` relay.

What lives here (business logic; handlers stay thin per AGENTS.md #4):

- :func:`check_tunnel_port` / :func:`check_tunnel_host` — Wall 1 port
  policy (also called agent-side as Wall 2 before dial; raises the shared
  ``ToolBlocked`` so error handling matches every other gate).
- :class:`TunnelRegistry` — in-memory tunnel + stream table, TTL sweep,
  per-tunnel token-bucket rate cap + lifetime total cap, per-user
  concurrency cap. In-memory on purpose (same trade-off as
  ``AgentRegistry`` and the default ``PendingActionStore``): a restart
  orphans live tunnels and every token becomes unresolvable. Persisting
  public byte-routes across restarts would be a vulnerability, not a
  feature — so there is deliberately NO migration (AGENTS.md #1).
- Framing helpers — tunnel frames are JSON text frames keyed on ``type``
  (binary WS frames are dropped by the ``agent_ws`` receive loop, which
  only speaks ``receive_json``). Frames NEVER carry ``job_id``/``result``
  keys, so they fall through the job branches of old parsers harmlessly.

Timestamps are epoch floats; no JSONB writes on the hot path (audit rows
are metadata only, emitted by the endpoints). This module moves bytes and
never calls providers — no router import anywhere (AGENTS.md #3).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import secrets
import time

from invincible.core.settings import settings
from invincible.core.tool_executor import ToolBlocked

# --- Port policy (Wall 1 server-side, Wall 2 agent-side) ---------------------

# Infrastructure ports that are never legit "show my dev server" targets:
# credential-forwarding / protocol-confusion risks (a browser fetching an
# SSH banner, a DB wire protocol fed HTTP). Everything else 1-65535 is
# allowed — dev servers live on 3000/5173/8000/8080/9000/random.
TUNNEL_DENIED_PORTS = frozenset({
    22,     # SSH
    3389,   # RDP
    5432, 5433,  # Postgres (server + local dev-db convention)
    3306,   # MySQL
    1433,   # MSSQL
    6379,   # Redis
    27017,  # MongoDB
    25, 587,  # SMTP / submission
})

HINT_BLOCKED_PORT = (
    "Expose a dev-server port (e.g. 3000, 5173, 8000, 8080) instead — "
    "infrastructure ports cannot be tunneled, so do not retry this port."
)

# The agent dials loopback literals only. Hostnames are refused outright
# (no DNS resolution at all: DNS would admit rebinding/SSRF against
# intranet hosts) and non-loopback literals are refused (no LAN IPs).
LOOPBACK_HOSTS = ("127.0.0.1", "::1")


def check_tunnel_port(port: int | str) -> int:
    """Raise ``ToolBlocked`` unless ``port`` is a tunnelable TCP port.

    Returns the validated int on pass. Pure function — safe to call on
    both walls (server pre-stage gate, agent pre-dial re-check).
    Strict about shape (fail closed): bools, fractional floats, and
    non-numeric strings are refused rather than coerced — ``True`` is
    not port 1 and ``3.5`` is not port 3.
    """
    number: int | None = None
    if isinstance(port, bool):
        number = None
    elif isinstance(port, int):
        number = port
    elif isinstance(port, float):
        number = int(port) if port.is_integer() else None
    elif isinstance(port, str):
        text = port.strip()
        number = int(text) if text.isdigit() else None
    if number is None:
        raise ToolBlocked(
            f"tunnel port must be an integer 1-65535, got {port!r}. "
            f"{HINT_BLOCKED_PORT}"
        )
    if not 1 <= number <= 65535:
        raise ToolBlocked(
            f"tunnel port out of range: {number}. {HINT_BLOCKED_PORT}"
        )
    if number in TUNNEL_DENIED_PORTS:
        raise ToolBlocked(
            f"tunnel port {number} is infrastructure and cannot be "
            f"exposed. {HINT_BLOCKED_PORT}"
        )
    return number


def check_tunnel_host(host: str) -> str:
    """Raise ``ToolBlocked`` unless ``host`` is a loopback literal.

    Hostnames are never resolved (DNS-rebinding guard); only the two
    loopback literals pass. The ``expose_port`` tool schema carries no
    host at all — this is the agent-side backstop before dial.
    """
    if str(host or "").strip() not in LOOPBACK_HOSTS:
        raise ToolBlocked(
            f"tunnel target must be localhost ({'/'.join(LOOPBACK_HOSTS)}); "
            f"refusing {host!r}. {HINT_BLOCKED_PORT}"
        )
    return str(host).strip()


# --- Framing -----------------------------------------------------------------

# Every tunnel frame carries "tunnel_id"; stream frames also carry
# "stream_id" (+ "seq" for data). None carry "job_id"/"result" — that is
# what lets them interleave with job JSON on one socket without breaking
# either parser (agents.py receive loop falls through both branches).
TUNNEL_FRAME_TYPES = frozenset({
    "tunnel_open",
    "tunnel_open_ack",
    "tunnel_request_head",
    "tunnel_response_head",
    "tunnel_data",
    "tunnel_half_close",
    "tunnel_close",
    "tunnel_window",
})

# Max decoded payload bytes per tunnel_data frame. Keeps WS messages
# small and bounds per-frame memory (base64 inflates ~33% on the wire).
MAX_FRAME_BYTES = 16 * 1024

# Bound on one request/response head (method + path + headers mapping).
# Heads past this are refused — a dev-server request line never needs
# more, and an unbounded head would be a memory primitive.
MAX_HEAD_BYTES = 32 * 1024

# Bound on concurrent streams per tunnel: a browser opens ~6-8 parallel
# connections per host, so 16 leaves headroom while bounding agent-side
# TCP fan-out from one approval.
MAX_STREAMS_PER_TUNNEL = 16

# Per-stream server queue bound: 64 frames x 16 KB ~= 1 MB in flight.
# A full queue drops the stream (502), never grows unbounded.
STREAM_QUEUE_MAXSIZE = 64

# How long confirm waits for the agent's tunnel_open_ack before giving
# up (the dial is local and instant; this only covers a wedged agent).
TUNNEL_OPEN_TIMEOUT_SECONDS = 30.0

# Entropy for the public URL capability (longer than PendingActionStore's
# token_urlsafe(16): this token lives in a URL path, not a POST body).
TUNNEL_TOKEN_BYTES = 24

# Sentinel completing a stream queue on close/half-close-after-drain.
STREAM_CLOSED = object()


class FrameError(ValueError):
    """A tunnel frame failed validation; the stream is dropped, fail closed."""


def make_tunnel_token() -> str:
    """Mint one unguessable public-URL capability."""
    return secrets.token_urlsafe(TUNNEL_TOKEN_BYTES)


def is_tunnel_frame(message: dict) -> bool:
    """True when ``message`` is a tunnel frame (never a job frame)."""
    return (
        isinstance(message, dict)
        and message.get("type") in TUNNEL_FRAME_TYPES
        and "job_id" not in message
        and "result" not in message
    )


def encode_data_frame(tunnel_id: str, stream_id: str, seq: int,
                      raw: bytes) -> dict:
    """Build one ``tunnel_data`` frame. Caps count decoded bytes."""
    if len(raw) > MAX_FRAME_BYTES:
        raise FrameError(
            f"tunnel frame payload {len(raw)} bytes exceeds "
            f"cap {MAX_FRAME_BYTES}"
        )
    return {
        "type": "tunnel_data",
        "tunnel_id": tunnel_id,
        "stream_id": stream_id,
        "seq": seq,
        "payload_b64": base64.b64encode(bytes(raw)).decode("ascii"),
    }


def decode_data_frame(frame: dict) -> bytes:
    """Validate + base64-decode one ``tunnel_data`` frame (fail closed)."""
    if not isinstance(frame, dict) or frame.get("type") != "tunnel_data":
        raise FrameError("not a tunnel_data frame")
    if "job_id" in frame or "result" in frame:
        # A frame shaped like both a tunnel frame and a job result is
        # forged or buggy — never route it anywhere.
        raise FrameError("tunnel frame must not carry job keys")
    for key in ("tunnel_id", "stream_id", "seq", "payload_b64"):
        if key not in frame:
            raise FrameError(f"tunnel_data frame missing {key!r}")
    try:
        raw = base64.b64decode(frame["payload_b64"], validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise FrameError(f"tunnel frame payload is not valid base64: {exc}") from None
    if len(raw) > MAX_FRAME_BYTES:
        raise FrameError(
            f"tunnel frame payload {len(raw)} bytes exceeds "
            f"cap {MAX_FRAME_BYTES}"
        )
    return raw


def _head_size_ok(mapping: dict) -> bool:
    """Bound one head's headers mapping (count + rough byte size)."""
    if not isinstance(mapping, dict) or len(mapping) > 100:
        return False
    total = 0
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return False
        total += len(key) + len(value)
        if total > MAX_HEAD_BYTES:
            return False
    return True


def validate_request_head(frame: dict) -> tuple[str, str, dict]:
    """Validate an agent-bound ``tunnel_request_head`` (fail closed).

    Returns ``(method, path, headers)``. Used by the agent before
    dialing (slice 5); the server builds (never parses) this frame.
    """
    if not isinstance(frame, dict) or frame.get("type") != "tunnel_request_head":
        raise FrameError("not a tunnel_request_head frame")
    if "job_id" in frame or "result" in frame:
        raise FrameError("tunnel frame must not carry job keys")
    method = str(frame.get("method", "")).upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE",
                      "HEAD", "OPTIONS"):
        raise FrameError(f"refusing tunnel method {method!r}")
    path = str(frame.get("path", ""))
    if not path.startswith("/") or len(path) > MAX_HEAD_BYTES:
        raise FrameError("refusing tunnel path")
    headers = frame.get("headers")
    if not _head_size_ok(headers):
        raise FrameError("refusing tunnel request headers")
    return method, path, dict(headers)


def validate_response_head(frame: dict) -> tuple[int, dict]:
    """Validate a server-bound ``tunnel_response_head`` (fail closed).

    Returns ``(status, headers)``. Used by the ``/t/`` route before the
    status/headers touch the public response.
    """
    if not isinstance(frame, dict) or frame.get("type") != "tunnel_response_head":
        raise FrameError("not a tunnel_response_head frame")
    if "job_id" in frame or "result" in frame:
        raise FrameError("tunnel frame must not carry job keys")
    try:
        status = int(frame.get("status", 0))
    except (TypeError, ValueError):
        raise FrameError("refusing tunnel response status") from None
    if not 100 <= status <= 599:
        raise FrameError("refusing tunnel response status")
    headers = frame.get("headers")
    if not _head_size_ok(headers):
        raise FrameError("refusing tunnel response headers")
    return status, dict(headers)


# --- Registry ----------------------------------------------------------------

STREAM_IDLE_SECONDS = 60.0


class TunnelCapExceeded(Exception):
    """User already holds the max concurrent tunnels (clear error, retryable)."""


class TunnelRegistry:
    """In-memory tunnel + stream table (F03).

    Layout (all keyed by capability token unless noted):

    - ``_tunnels`` — token -> record ``{tunnel_id, owner, port,
      created_at, bytes_total, window_start, window_used, streams,
      ws, open_acked}``. Tokens are raw in memory (same posture as
      ``PendingActionStore``); they are never logged, only compared.
    - ``_by_owner`` — user_id -> set of tokens, for the per-user
      concurrency cap and for disconnect teardown.
    - ``streams`` — stream_id -> ``asyncio.Queue`` of decoded ``bytes``
      chunks (or ``STREAM_CLOSED``). Bounded (``STREAM_QUEUE_MAXSIZE``);
      a full queue drops the stream rather than applying backpressure
      upstream without bound.

    Socket affinity: a tunnel pins to the ``ws`` object that acked its
    ``tunnel_open`` — NOT ``push_ws``'s first-attached socket — because
    one user may have N machines online and bytes for machine B must
    never ride machine A's socket.

    Isolation is structural: every record carries its owner's user_id,
    lookups miss indistinguishably (unknown/expired/wrong… there is no
    wrong-owner lookup — tokens are unguessable capabilities, and close
    is owner-bound), and ``detach_ws`` only tears down that socket's
    tunnels. No cross-user code path exists.
    """

    def __init__(self, *, clock=time.time):
        self._tunnels: dict[str, dict] = {}
        self._by_owner: dict[int, set[str]] = {}
        # token -> Future awaiting the agent's tunnel_open_ack (confirm
        # path only; resolved by resolve_ack, cancelled on timeout/close).
        self._acks: dict[str, asyncio.Future] = {}
        self._clock = clock

    # --- open / lookup / close -----------------------------------------

    def open(self, owner: int, port: int | str, ws=None) -> str:
        """Register a tunnel (post-confirm). Returns its capability token.

        Raises ``ToolBlocked`` for a denied port (defense in depth — the
        policy gate normally stops these pre-stage) and
        ``TunnelCapExceeded`` past the per-user concurrency cap.
        """
        number = check_tunnel_port(port)
        self._sweep()
        live = sum(
            1 for token in self._by_owner.get(owner, ())
            if token in self._tunnels
        )
        if live >= settings.tunnel_max_per_user():
            raise TunnelCapExceeded(
                f"user holds {live} tunnels "
                f"(cap {settings.tunnel_max_per_user()}); close one first"
            )
        token = make_tunnel_token()
        now = self._clock()
        self._tunnels[token] = {
            "tunnel_id": token,
            "owner": owner,
            "port": number,
            "created_at": now,
            "bytes_total": 0,
            "window_start": now,
            "window_used": 0,
            "streams": {},
            "ws": ws,
            "open_acked": ws is None,
            "last_activity": now,
            # Serializes server→agent sends for this tunnel: one socket
            # may carry interleaved streams, and concurrent send_json
            # calls must not interleave mid-message.
            "send_lock": asyncio.Lock(),
        }
        self._by_owner.setdefault(owner, set()).add(token)
        return token

    def lookup(self, token: str) -> dict | None:
        """Resolve a capability token, or None if unknown/expired.

        Expired behaves exactly like unknown (indistinguishable 404
        downstream) and is purged on the lazy sweep.
        """
        self._sweep()
        record = self._tunnels.get(token or "")
        if record is None:
            return None
        if self._clock() - record["created_at"] > settings.tunnel_ttl_seconds():
            self._drop(token)
            return None
        return record

    def close(self, token: str, requester: int | None) -> bool:
        """Owner-bound, idempotent close (safe direction, no staging).

        True when a live tunnel was torn down. False — identically — for
        unknown, expired, or differently-owned tokens.
        """
        self._sweep()
        record = self._tunnels.get(token or "")
        if record is None:
            return False
        if requester is not None and record["owner"] != requester:
            return False
        self._drop(token)
        return True

    def _drop(self, token: str) -> None:
        record = self._tunnels.pop(token, None)
        if record is None:
            return
        self._by_owner.get(record["owner"], set()).discard(token)
        ack = self._acks.pop(token, None)
        if ack is not None and not ack.done():
            ack.cancel()
        for queue in record["streams"].values():
            # A wedged consumer must not keep the tunnel alive: best-effort
            # seal, then drop the reference so streams end on next poll.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(STREAM_CLOSED)
        record["streams"].clear()

    def _sweep(self, now: float | None = None) -> None:
        """Purge TTL-expired tunnels (lazy: on lookup/open/close — no
        background task, the PendingActionStore._sweep pattern)."""
        at = now if now is not None else self._clock()
        ttl = settings.tunnel_ttl_seconds()
        for token, record in list(self._tunnels.items()):
            if at - record["created_at"] > ttl:
                self._drop(token)

    # --- open ack rendezvous (confirm path) -------------------------------

    async def await_ack(self, token: str,
                        timeout: float = TUNNEL_OPEN_TIMEOUT_SECONDS,
                        ) -> dict | None:
        """Wait for the agent's ``tunnel_open_ack`` for one open tunnel.

        Returns ``{"ok": bool, "error": str}``, or None on timeout (the
        tunnel is dropped — a wedged agent must not hold the slot).
        """
        if token not in self._tunnels:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        future = loop.create_future()
        self._acks[token] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._drop(token)
            return None
        finally:
            self._acks.pop(token, None)

    def resolve_ack(self, token: str, ws, ok: bool,
                    error: str = "") -> bool:
        """Record one agent's ``tunnel_open_ack`` (called from the
        ``agent_ws`` receive loop, which supplies the socket).

        On ``ok`` the tunnel pins to that socket (affinity: later bytes
        ride exactly this connection, never first-attached). False when
        no open is waiting (unknown/expired/timed-out token — dropped,
        fail closed).
        """
        future = self._acks.get(token)
        if future is None or future.done():
            return False
        if ok and not self.bind_ws(token, ws):
            return False
        future.set_result({"ok": bool(ok), "error": str(error or "")})
        return True

    # --- caps ------------------------------------------------------------

    async def spend(self, token: str, n: int) -> bool:
        """Admit ``n`` decoded bytes against rate + total caps.

        Throttles (one bounded sleep, at most ~1s) when the 1-second
        token-bucket window is over budget, then admits. Returns False
        when the lifetime total cap is hit — the caller must close the
        stream. A single frame bigger than the whole rate window still
        admits after one sleep (never spins forever on tiny test knobs).
        """
        record = self._tunnels.get(token or "")
        if record is None:
            return False
        if record["bytes_total"] + n > settings.tunnel_total_bytes():
            return False
        now = self._clock()
        limit = settings.tunnel_bytes_per_second()
        if now - record["window_start"] >= 1.0:
            record["window_start"] = now
            record["window_used"] = 0
        if record["window_used"] + n > limit:
            await asyncio.sleep(max(0.0, record["window_start"] + 1.0 - now))
            at = self._clock()
            record["window_start"] = at
            record["window_used"] = 0
        record["window_used"] += n
        record["bytes_total"] += n
        record["last_activity"] = self._clock()
        return True

    # --- streams -----------------------------------------------------------

    def stream_queue(self, token: str, stream_id: str) -> asyncio.Queue | None:
        """Get (creating) the bounded queue for one stream, or None when
        the tunnel is unknown/expired (indistinguishable downstream) or
        the per-tunnel stream cap is hit (caller answers 429 — the token
        is already validated at that point, so nothing leaks)."""
        record = self.lookup(token)
        if record is None:
            return None
        queue = record["streams"].get(stream_id)
        if queue is None:
            if len(record["streams"]) >= MAX_STREAMS_PER_TUNNEL:
                return None
            queue = asyncio.Queue(maxsize=STREAM_QUEUE_MAXSIZE)
            record["streams"][stream_id] = queue
        record["last_activity"] = self._clock()
        return queue

    def push_to_stream(self, token: str, stream_id: str,
                       chunk: bytes | object) -> bool:
        """Non-blocking enqueue of one decoded chunk (or STREAM_CLOSED).

        False when the tunnel is gone or the queue is full — the caller
        drops the stream (502), never blocks the relay.
        """
        record = self._tunnels.get(token or "")
        if record is None:
            return False
        queue = record["streams"].get(stream_id)
        if queue is None:
            return False
        try:
            queue.put_nowait(chunk)
        except asyncio.QueueFull:
            return False
        record["last_activity"] = self._clock()
        return True

    def drop_stream(self, token: str, stream_id: str) -> None:
        """Seal + forget one stream (cap hit, seq break, idle, RST)."""
        record = self._tunnels.get(token or "")
        if record is None:
            return
        queue = record["streams"].pop(stream_id, None)
        if queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(STREAM_CLOSED)

    # --- socket affinity + orphan teardown (C2: hard close) -----------------

    def bind_ws(self, token: str, ws) -> bool:
        """Pin a tunnel to the socket that acked its open. False when the
        tunnel is gone (the ack is then dropped, fail closed)."""
        record = self._tunnels.get(token or "")
        if record is None:
            return False
        record["ws"] = ws
        record["open_acked"] = True
        return True

    def socket_for(self, token: str):
        """The owning socket for tunnel sends (None when unacked/gone)."""
        record = self._tunnels.get(token or "")
        if record is None or not record["open_acked"]:
            return None
        return record["ws"]

    async def send_frame(self, token: str, frame: dict) -> bool:
        """Send one frame down the tunnel's pinned socket, serialized
        against sibling streams. False when the tunnel is gone, unacked,
        or the send failed (caller drops the stream; the ``agent_ws``
        loop owns socket teardown)."""
        record = self._tunnels.get(token or "")
        if record is None or not record["open_acked"]:
            return False
        ws = record["ws"]
        if ws is None:
            return False
        try:
            async with record["send_lock"]:
                await ws.send_json(frame)
        except Exception:
            return False
        record["last_activity"] = self._clock()
        return True

    def detach_ws(self, ws) -> int:
        """Hard-close every tunnel pinned to a dead socket (C2): agent
        disconnect orphans nothing — streams seal, tokens drop, clients
        retry end-to-end. Returns the count torn down."""
        doomed = [
            token for token, record in self._tunnels.items()
            if record.get("ws") is ws
        ]
        for token in doomed:
            self._drop(token)
        return len(doomed)

    def tunnels_for(self, owner: int) -> list[dict]:
        """This owner's live tunnels (metadata only — never tokens beyond
        the id the owner already holds; used by close-by-port UX)."""
        self._sweep()
        return [
            {
                "port": record["port"],
                "created_at": record["created_at"],
                "bytes_total": record["bytes_total"],
                "open_acked": record["open_acked"],
            }
            for token in self._by_owner.get(owner, ())
            if (record := self._tunnels.get(token)) is not None
        ]
