# invincible/endpoints/tunnel.py
"""Public tunnel egress: /t/{token}/{path} (F03).

One confirmed ``expose_port`` mints an unguessable capability token; this
route serves that tunnel's bytes to the public internet. The token in the
path IS the auth (a bearer capability, not a fourth realm — see
docs/SECURITY.md §11): unknown and expired tokens answer with one
identical 404, so probers learn nothing. A *valid* token whose agent is
gone answers 502 — operational honesty for the tunnel owner debugging
their demo, leaking nothing to anyone who doesn't already hold the token.

Per stream: forward the filtered request head + body chunks as frames
down the tunnel's pinned socket, then stream the agent's response head +
body chunks back. Caps count decoded bytes (base64 inflates ~33% on the
wire); the lifetime total covers both directions, the token-bucket rate
covers the shared window. Anything malformed ends the stream (502 /
truncation), never the tunnel — fail closed, one stream at a time.

Registered AFTER every other router in main.py on purpose: the
``/{path:path}`` tail must never shadow a real route.
"""
import asyncio
import contextlib
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from invincible.core.harness_tunnel import (
    MAX_FRAME_BYTES,
    STREAM_CLOSED,
    STREAM_IDLE_SECONDS,
    FrameError,
    encode_data_frame,
    validate_response_head,
)

router = APIRouter()

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Hop-by-hop headers are never forwarded either direction (the gateway
# terminates both hops). Request side additionally strips credentials and
# Host: the tunnel must not become a credential-forwarding oracle against
# local services, and the agent rebuilds Host for 127.0.0.1 itself.
_STRIP_REQUEST = frozenset({
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding",
    "upgrade", "authorization", "cookie", "host",
})

# Response side strips framing (Starlette re-chunks) + hop-by-hop. The
# local app's own headers (content-type, location, set-cookie, …) pass
# through — faithful proxying is the point.
_STRIP_RESPONSE = frozenset({
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding",
    "upgrade", "content-length",
})


def _not_found() -> JSONResponse:
    """The one indistinguishable answer (unknown/expired/raced)."""
    return JSONResponse({"detail": "Not found"}, status_code=404)


def _filter(headers, stripped: frozenset) -> dict:
    kept = {}
    for key, value in headers.items():
        if key.lower() in stripped:
            continue
        kept[key] = value
    return kept


async def _pump_request_body(request: Request, tunnels, token: str,
                             stream_id: str) -> None:
    """Forward the inbound body as tunnel_data frames, then half-close.

    Stops (leaving the stream to the response side) when the tunnel is
    gone, a send fails, or the lifetime total cap is hit. Total-cap and
    rate accounting share spend(): upstream bytes are usually small;
    counting them keeps one bucket and one audit story.
    """
    seq = 0
    try:
        async for chunk in request.stream():
            for start in range(0, len(chunk), MAX_FRAME_BYTES):
                piece = chunk[start:start + MAX_FRAME_BYTES]
                if not await tunnels.spend(token, len(piece)):
                    return
                try:
                    frame = encode_data_frame(
                        token, stream_id, seq, piece)
                except FrameError:
                    return
                seq += 1
                if not await tunnels.send_frame(token, frame):
                    return
    except (asyncio.CancelledError, Exception):
        return
    finally:
        with contextlib.suppress(Exception):
            await tunnels.send_frame(token, {
                "type": "tunnel_half_close",
                "tunnel_id": token,
                "stream_id": stream_id,
            })


@router.api_route("/t/{token}/{path:path}", methods=_METHODS)
async def tunnel_proxy(request: Request, token: str, path: str):
    tunnels = getattr(request.app.state, "tunnel_registry", None)
    record = tunnels.lookup(token) if tunnels is not None else None
    if record is None:
        return _not_found()
    owner = record["owner"]
    if tunnels.socket_for(token) is None:
        return JSONResponse(
            {"detail": "Tunnel unavailable"}, status_code=502)
    stream_id = secrets.token_urlsafe(8)
    queue = tunnels.stream_queue(token, stream_id)
    if queue is None:
        # Raced expiry → 404 (indistinguishable); stream cap → 429 (the
        # token is already validated, so nothing leaks).
        if tunnels.lookup(token) is None:
            return _not_found()
        return JSONResponse(
            {"detail": "Too many concurrent streams"},
            status_code=429, headers={"Retry-After": "5"},
        )
    query = request.url.query
    head = {
        "type": "tunnel_request_head",
        "tunnel_id": token,
        "stream_id": stream_id,
        "method": request.method,
        "path": "/" + path + ("?" + query if query else ""),
        "headers": _filter(request.headers, _STRIP_REQUEST),
    }
    if not await tunnels.send_frame(token, head):
        tunnels.drop_stream(token, stream_id)
        return JSONResponse(
            {"detail": "Tunnel unavailable"}, status_code=502)
    pump = asyncio.create_task(
        _pump_request_body(request, tunnels, token, stream_id))

    async def _fail():
        pump.cancel()
        tunnels.drop_stream(token, stream_id)
        return JSONResponse(
            {"detail": "Tunnel unavailable"}, status_code=502)

    try:
        first = await asyncio.wait_for(queue.get(),
                                       timeout=STREAM_IDLE_SECONDS)
    except asyncio.TimeoutError:
        return await _fail()
    if (first is STREAM_CLOSED or not isinstance(first, dict)
            or first.get("type") != "tunnel_response_head"):
        # Agent closed before answering, or data beat the head
        # (protocol violation) — end the stream, fail closed.
        return await _fail()
    try:
        status, resp_headers = validate_response_head(first)
    except FrameError:
        return await _fail()

    async def _body():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=STREAM_IDLE_SECONDS)
                except asyncio.TimeoutError:
                    break  # idle: end the stream
                if item is STREAM_CLOSED or not isinstance(item, bytes):
                    break  # closed, or a control frame past the head:
                    # fail closed, never emit non-bytes downstream
                if not await tunnels.spend(token, len(item)):
                    log = getattr(request.app.state, "audit_log", None)
                    if log is not None:
                        with contextlib.suppress(Exception):
                            await log.record(
                                "tunnel.total_cap",
                                actor_user_id=owner,
                                actor_kind="mcp",
                                resource_type="tunnel",
                            )
                    break  # lifetime total hit: truncate the stream
                yield item
        finally:
            pump.cancel()
            tunnels.drop_stream(token, stream_id)
            # Best-effort close so the agent frees its TCP dial; an
            # unknown stream is ignored agent-side (idempotent).
            with contextlib.suppress(Exception):
                await tunnels.send_frame(token, {
                    "type": "tunnel_close",
                    "tunnel_id": token,
                    "stream_id": stream_id,
                })

    return StreamingResponse(
        _body(), status_code=status,
        headers=_filter(resp_headers, _STRIP_RESPONSE),
    )
