# invincible/endpoints/agents.py
"""Agent-facing endpoints: long-poll + WebSocket relay + result submission
+ phone→PC handoff (Phase 10, extended H1, B2 handoff).

Transport for confirmed MCP tool jobs between the server and the
user's paired harness (``invincible harness connect`` on their own machine). Two
realms meet here, and they never mix:

- ``POST /agent/poll`` + ``POST /agent/result`` + ``WS /agent/ws``
  + ``POST /agent/handoff`` (B2: fire-and-forget phone→PC task,
  WS-first with poll fallback, control-plane — not a tool job)
  authenticate with an inv_ API key (the same credential
  ``invincible login`` persists to ~/.invincible/config.json) via
  ``require_agent_auth`` - a deliberately narrow dependency, NOT
  endpoints/auth.py's require_auth. A key resolves to exactly one user,
  and registry queues/sockets are keyed by that user - routing is the
   isolation. WS is outbound-only from the agent (relay);
  WS-first with long-poll fallback.
- Read-only companions in that same inv_-key realm: ``GET
  /agent/machines`` (machine inventory) and ``GET /agent/whoami`` (which
  account a paired key belongs to - never the key itself).
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

from invincible.core.accounts import UserService, resolve_session
from invincible.core.agent_registry import (
    HANDOFF_CONTEXT_MAX_CHARS,
    HANDOFF_FROM_DEVICE_MAX_CHARS,
    HANDOFF_TASK_MAX_CHARS,
    PollCapacityExceeded,
)
from invincible.core.harness_tunnel import (
    FrameError,
    decode_data_frame,
    validate_response_head,
)
from invincible.core.identity import ensure_default_project
from invincible.core.settings import AGENT_POLL_HOLD_SECONDS, settings

router = APIRouter()

# WS close codes (application-defined 4xxx): auth vs disabled vs cap.
_WS_AUTH_CLOSE = 4401
_WS_DISABLED_CLOSE = 4400
_WS_CAP_CLOSE = 4402


def _route_tunnel_frame(tunnels, message: dict) -> None:
    """Deliver one agent→server tunnel frame to its stream queue.

    ``tunnel_data`` is base64-validated before enqueue (caps count
    decoded bytes); ``tunnel_response_head`` is shape-validated here and
    re-validated by the ``/t/`` route before touching the public
    response (the agent is user-controlled — validate at the trust
    boundary). A full queue or a bad frame drops the stream, fail
    closed. Never raises (the caller wraps in suppress).
    """
    token = str(message.get("tunnel_id", ""))
    stream_id = str(message.get("stream_id", ""))
    if message.get("type") == "tunnel_data":
        try:
            chunk = decode_data_frame(message)
        except FrameError:
            tunnels.drop_stream(token, stream_id)
            return
        if not tunnels.push_to_stream(token, stream_id, chunk):
            tunnels.drop_stream(token, stream_id)
        return
    try:
        status, headers = validate_response_head(message)
    except FrameError:
        tunnels.drop_stream(token, stream_id)
        return
    if not tunnels.push_to_stream(token, stream_id, {
        "type": "tunnel_response_head",
        "status": status,
        "headers": headers,
    }):
        tunnels.drop_stream(token, stream_id)


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


async def _resolve_agent_principal(request: Request) -> dict:
    """Resolve the inv_ Bearer key to its stored record, or 401.

    ApiKeyStore.resolve (core/identity.py) is the single source of
    truth for key validity and revocation; an unrevoked key implies a
    completed device pairing. The store touches last_used_at
    best-effort on its own. The record carries ``user_id`` (the
    isolation key every queue/socket/machine table is keyed by) and
    ``prefix`` (the visible non-secret slice). Never the raw key.
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
    return resolved


async def require_agent_auth(request: Request) -> int:
    """The inv_ key's owning user_id - the isolation key agent routing
    and machine inventory are built on. Thin wrapper over
    ``_resolve_agent_principal`` so there is exactly one resolution
    path (and one set of 401s) on this surface.
    """
    return int((await _resolve_agent_principal(request))["user_id"])


@router.post("/agent/poll")
async def agent_poll(request: Request,
                     user_id: int = Depends(require_agent_auth)) -> dict:
    """Long-poll: answer with the next confirmed job for this agent's
    user, or ``{"job": null}`` after the hold window. Every call is a
    heartbeat, so liveness tracks connection health, not execution.
    Over-cap concurrent polls (LOW-5) get a 429 - the agent runner
    treats any non-200 like a network hiccup (2s backoff), so the
    excess connection sheds without hot-looping.

    B2: a queued phone→PC handoff rides along additively (``"handoff"``
    key present ONLY when one is waiting) so WS-less PCs still hop.
    F22: the browser's latest voice state rides along the same way
    (``"voice_state"`` present ONLY when waiting) for WS-less agents.
    Job-only consumers keep their exact ``{"job": ...}`` shape when
    neither extra waits.
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
    extra: dict = {}
    handoff = registry.take_handoff(user_id)
    if handoff is not None:
        extra["handoff"] = handoff
    voice = registry.take_voice_state(user_id)
    if voice is not None:
        extra["voice_state"] = voice["state"]
    return {"job": job, **extra}


@router.post("/agent/handoff")
async def agent_handoff(request: Request,
                        user_id: int = Depends(require_agent_auth)) -> dict:
    """Phone→PC handoff (B2): queue a fire-and-forget task for this key's
    own paired PC, WS-first with poll fallback.

    Owner-scoped by construction: the inv_ key resolves to exactly one
    user and the handoff lands only in that user's queue — cross-user
    handoffs are refused the same structural way cross-user jobs are
    (there is no target-user field to forge). The phone's "hopping to
    PC…" state resolves from this response (``relayed`` vs queued).

    Fail-closed when ``INVINCIBLE_HANDOFF`` is off (503, nothing
    queued). Restart orphans queued handoffs (in-memory by design, same
    trade-off as jobs) — the phone retries end-to-end.
    """
    if not settings.handoff_enabled():
        raise HTTPException(
            status_code=503, detail="Handoff is disabled on this server")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400,
                            detail="Body must be {task, context?}")
    task = body.get("task")
    context = body.get("context", "")
    from_device = body.get("from_device", "")
    if not isinstance(task, str) or not task.strip():
        raise HTTPException(status_code=400,
                            detail="task must be a non-empty string")
    if len(task) > HANDOFF_TASK_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"task must be at most {HANDOFF_TASK_MAX_CHARS} "
                   "characters",
        )
    if context is None:
        context = ""
    if not isinstance(context, str):
        raise HTTPException(status_code=400,
                            detail="context must be a string")
    if len(context) > HANDOFF_CONTEXT_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"context must be at most {HANDOFF_CONTEXT_MAX_CHARS} "
                   "characters",
        )
    if from_device is None:
        from_device = ""
    if not isinstance(from_device, str):
        raise HTTPException(status_code=400,
                            detail="from_device must be a string")
    from_device = from_device.strip()[:HANDOFF_FROM_DEVICE_MAX_CHARS]
    registry = request.app.state.agent_registry
    envelope = registry.post_handoff(
        user_id, task.strip(), context, from_device)
    relayed = False
    if settings.handoff_enabled():
        # WS-first: exactly one socket, like jobs. On success drop the
        # queued copy so a concurrent poll can't deliver twice; on
        # failure the queue copy stays for poll fallback.
        with contextlib.suppress(Exception):
            relayed = await registry.push_ws(
                user_id,
                {"type": "handoff", "handoff": envelope["handoff"],
                 "handoff_id": envelope["handoff_id"]},
            )
        if relayed:
            registry.drop_handoff(user_id, envelope["handoff_id"])
    return {"queued": True, "relayed": bool(relayed),
            "handoff_id": envelope["handoff_id"]}


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


@router.get("/agent/whoami")
async def agent_whoami(
    request: Request,
    resolved: dict = Depends(_resolve_agent_principal),
) -> dict:
    """Which account this paired inv_ key belongs to (Phase 6 follow-up).

    Read-only and secret-safe: the visible key ``prefix`` is reported,
    never the raw key or its hash. Same narrow inv_-key realm as
    poll/result/machines - deliberately NOT require_auth, and no cookie
    path, because the realms never merge (docs/HARNESS_PLAN.md §5).
    """
    engine = request.app.state.engine
    user_id = int(resolved["user_id"])
    user = await UserService(engine).get(user_id)
    return {
        "user_id": user_id,
        "email": user["email"] if user else None,
        "project_id": await ensure_default_project(engine, user_id),
        "key_prefix": resolved["prefix"],
    }


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
    """Outbound-only relay socket for the paired agent (H1).

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
            if message.get("type") == "tunnel_open_ack":
                # F03: the agent dialed localhost:PORT for a confirmed
                # tunnel and reports back on the SAME socket — the
                # registry pins tunnel sends to this connection
                # (affinity), resolving the confirm-time ack future.
                # Unknown/shape-bad frames are dropped, never answered:
                # the ack carries no secret worth confirming.
                with contextlib.suppress(Exception):
                    tunnels = getattr(
                        websocket.app.state, "tunnel_registry", None)
                    if tunnels is not None:
                        tunnels.resolve_ack(
                            str(message.get("tunnel_id", "")),
                            websocket,
                            message.get("ok") is True,
                            str(message.get("error", "")),
                        )
                continue
            if message.get("type") in ("tunnel_data",
                                       "tunnel_response_head"):
                # F03 data plane: agent→server stream bytes / response
                # heads land in the stream's queue for the holding /t/
                # request. Anything malformed drops the stream (fail
                # closed) — never the tunnel, never the relay.
                with contextlib.suppress(Exception):
                    tunnels = getattr(
                        websocket.app.state, "tunnel_registry", None)
                    if tunnels is not None:
                        _route_tunnel_frame(tunnels, message)
                continue
            if message.get("type") in ("tunnel_half_close",
                                       "tunnel_close"):
                # Agent finished (half: body done; full: teardown) —
                # seal the stream; the holding /t/ request drains it.
                with contextlib.suppress(Exception):
                    tunnels = getattr(
                        websocket.app.state, "tunnel_registry", None)
                    if tunnels is not None:
                        tunnels.drop_stream(
                            str(message.get("tunnel_id", "")),
                            str(message.get("stream_id", "")),
                        )
                continue
            job_id = message.get("job_id")
            result = message.get("result")
            if job_id and isinstance(result, dict):
                accepted = registry.submit_result(user_id, job_id, result)
                with contextlib.suppress(Exception):
                    await websocket.send_json({"accepted": bool(accepted)})
    finally:
        registry.detach_ws(user_id, websocket)
        # F03/C2: a dead socket hard-closes its tunnels — streams seal,
        # tokens drop, public clients retry end-to-end. Never raises.
        with contextlib.suppress(Exception):
            tunnels = getattr(websocket.app.state, "tunnel_registry", None)
            if tunnels is not None:
                tunnels.detach_ws(websocket)


@router.websocket("/harness/events")
async def harness_events_ws(websocket: WebSocket) -> None:
    """Read-only inspector stream (H1).

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
