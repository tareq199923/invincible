# invincible/endpoints/agents.py
"""Agent-facing endpoints: long-poll + WebSocket relay + result submission
(Phase 10, extended H1).

Transport for confirmed MCP tool jobs between the server and the
user's paired harness (``invincible harness connect`` on their own machine). Two
realms meet here, and they never mix:

- ``POST /agent/poll`` + ``POST /agent/result`` + ``WS /agent/ws``
  authenticate with an inv_ API key (the same credential
  ``invincible login`` persists to ~/.invincible/config.json) via
  ``require_agent_auth`` - a deliberately narrow dependency, NOT
  endpoints/auth.py's require_auth. A key resolves to exactly one user,
  and registry queues/sockets are keyed by that user - routing is the
  isolation. WS is outbound-only from the agent (flexx-style relay);
  WS-first with long-poll fallback.
- ``GET /agent/status`` + ``WS /harness/events`` authenticate with a
  dashboard session cookie (``resolve_session``, the same resolver every
  account page uses). The events socket is read-only: replay + live
  harnessbus stream for the inspector/dashboard.

Replay indistinguishability: submit_result returns plain accepted
True/False with no reason attached, mirroring PendingActionStore.take's
treatment of mismatched subjects - unknown, timed-out,
already-resolved, and wrong-owner job_ids all look identical to the
caller.
"""
import asyncio
import contextlib

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)

from invincible.core.accounts import resolve_session
from invincible.core.agent_registry import PollCapacityExceeded
from invincible.core.settings import AGENT_POLL_HOLD_SECONDS, settings

router = APIRouter()

# WS close codes (application-defined 4xxx): auth vs disabled vs cap.
_WS_AUTH_CLOSE = 4401
_WS_DISABLED_CLOSE = 4400
_WS_CAP_CLOSE = 4402


async def _resolve_ws_key(websocket: WebSocket) -> int | None:
    """Resolve the inv_ key from an Authorization header or ?api_key=
    (WS clients can't always set headers). None when missing/unknown."""
    auth = websocket.headers.get("authorization", "")
    raw = ""
    if auth.startswith("Bearer "):
        raw = auth[len("Bearer "):].strip()
    elif "api_key" in websocket.query_params:
        raw = websocket.query_params["api_key"].strip()
    if not raw:
        return None
    api_keys = getattr(websocket.app.state, "api_keys", None)
    if api_keys is None:
        return None
    try:
        resolved = await api_keys.resolve(raw)
    except Exception:
        return None
    if resolved is None:
        return None
    return int(resolved["user_id"])


async def require_agent_auth(request: Request) -> int:
    """Resolve the inv_ Bearer key to its owning user_id, or 401.

    ApiKeyStore.resolve (core/identity.py) is the single source of
    truth for key validity and revocation; an unrevoked key implies a
    completed device pairing. The store touches last_used_at
    best-effort on its own.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer key")
    raw = auth[len("Bearer "):].strip()
    api_keys = getattr(request.app.state, "api_keys", None)
    resolved = (
        await api_keys.resolve(raw) if api_keys is not None else None
    )
    if resolved is None:
        raise HTTPException(status_code=401, detail="Unknown or revoked key")
    return int(resolved["user_id"])


@router.post("/agent/poll")
async def agent_poll(request: Request,
                     user_id: int = Depends(require_agent_auth)) -> dict:
    """Long-poll: answer with the next confirmed job for this agent's
    user, or ``{"job": null}`` after the hold window. Every call is a
    heartbeat, so liveness tracks connection health, not execution.
    Over-cap concurrent polls (LOW-5) get a 429 - the agent runner
    treats any non-200 like a network hiccup (2s backoff), so the
    excess connection sheds without hot-looping.
    """
    registry = request.app.state.agent_registry
    try:
        job = await registry.poll(user_id, AGENT_POLL_HOLD_SECONDS)
    except PollCapacityExceeded:
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent polls for this account",
            headers={"Retry-After": "5"},
        ) from None
    if job is None:
        return {"job": None}
    return {"job": job}


@router.post("/agent/result")
async def agent_result(request: Request,
                       user_id: int = Depends(require_agent_auth)) -> dict:
    """Submit a job result. Accepted False covers unknown, timed-out,
    already-resolved, and wrong-owner job_ids indistinguishably; a
    mismatched result can never resolve a future twice."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail="Invalid JSON body") from exc
    job_id = body.get("job_id") if isinstance(body, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    if not job_id or not isinstance(result, dict):
        raise HTTPException(
            status_code=400,
            detail="Body must be {job_id, result(dict)}",
        )
    registry = request.app.state.agent_registry
    accepted = registry.submit_result(user_id, job_id, result)
    return {"accepted": bool(accepted)}


@router.get("/agent/machines")
async def agent_machines(
    request: Request,
    user_id: int = Depends(require_agent_auth),
) -> dict:
    """This agent's account machine inventory (H6c, inv_-key realm like
    poll/result — the CLI `harness status` reads this; the dashboard
    Machines page reads the cookie-realm `/agent/status`). Structural
    isolation holds by construction: queues, sockets, and machine tables
    are all keyed by the resolved user."""
    registry = request.app.state.agent_registry
    return {"machines": registry.machines_for(user_id)}


@router.get("/agent/status")
async def agent_status(request: Request) -> dict:
    """Agent liveness for the signed-in dashboard user (session-cookie
    realm, same resolver as every account page). H6c adds the per-machine
    inventory (additive — existing `agent_online` consumers untouched)."""
    user = await resolve_session(
        request.app.state.engine, request.cookies.get("invincible_session")
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    registry = request.app.state.agent_registry
    uid = int(user["id"])
    return {
        "agent_online": registry.online(uid),
        "machines": registry.machines_for(uid),
    }


@router.websocket("/agent/ws")
async def agent_ws(websocket: WebSocket) -> None:
    """Outbound-only relay socket for the paired agent (H1, flexx-style).

    The agent connects here (authenticating with its inv_ key) and the
    server pushes ``{"type": "job", "job": {...}}`` messages; the agent
    replies ``{"job_id": ..., "result": {...}}``. ``{"type": "hello",
    ...}`` frames are heartbeats (machine_id/capabilities are acked, not
    persisted in H1). Poll fallback stays available when WS is disabled
    or unreachable.
    """
    if not settings.harness_ws_enabled():
        await websocket.close(
            code=_WS_DISABLED_CLOSE, reason="ws disabled, use poll")
        return
    user_id = await _resolve_ws_key(websocket)
    if user_id is None:
        await websocket.close(
            code=_WS_AUTH_CLOSE, reason="unknown or revoked key")
        return
    registry = websocket.app.state.agent_registry
    try:
        registry.attach_ws(user_id, websocket)
    except PollCapacityExceeded:
        await websocket.close(
            code=_WS_CAP_CLOSE, reason="too many connections")
        return
    await websocket.accept()
    try:
        while True:
            try:
                message = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                # Malformed frame: ignore, keep the relay open (same
                # posture as the poll loop's backoff-and-retry).
                continue
            registry.heartbeat(user_id)
            if not isinstance(message, dict):
                continue
            if message.get("type") == "hello":
                # H1 hello + H6c inventory: capabilities acked AND tracked
                # per machine_id for the dashboard Machines page.
                # Anything malformed here is display data only — never
                # let it break the relay.
                with contextlib.suppress(Exception):
                    registry.update_machine(
                        user_id, message.get("machine_id", ""),
                        {
                            "machine_name": message.get("machine_name", ""),
                            "platform": message.get("platform", ""),
                            "capabilities": message.get("capabilities"),
                        },
                    )
                with contextlib.suppress(Exception):
                    await websocket.send_json({"type": "hello_ack"})
                continue
            job_id = message.get("job_id")
            result = message.get("result")
            if job_id and isinstance(result, dict):
                accepted = registry.submit_result(user_id, job_id, result)
                with contextlib.suppress(Exception):
                    await websocket.send_json({"accepted": bool(accepted)})
    finally:
        registry.detach_ws(user_id, websocket)


@router.websocket("/harness/events")
async def harness_events_ws(websocket: WebSocket) -> None:
    """Read-only inspector stream (H1, Hendrixer inspector parity).

    Session-cookie realm. Replays the bounded bus history, then streams
    new events by polling `history(since_ts)` — no cross-thread listener
    bridge, no model/tool access from this socket.
    """
    engine = getattr(websocket.app.state, "engine", None)
    user = await resolve_session(
        engine, websocket.cookies.get("invincible_session"))
    if user is None:
        await websocket.close(
            code=_WS_AUTH_CLOSE, reason="not signed in")
        return
    bus = getattr(websocket.app.state, "harness_bus", None)
    if bus is None:
        await websocket.close(code=_WS_DISABLED_CLOSE, reason="no bus")
        return
    await websocket.accept()
    since_ts: float | None = None
    try:
        for event in bus.history():
            await websocket.send_json(dict(event))
            since_ts = float(event.get("ts", 0) or 0)
        while True:
            await asyncio.sleep(1.0)
            fresh = bus.history(since_ts=since_ts)
            for event in fresh:
                await websocket.send_json(dict(event))
                since_ts = float(event.get("ts", 0) or 0)
    except WebSocketDisconnect:
        return
    except Exception:
        with contextlib.suppress(Exception):
            await websocket.close()
        return
