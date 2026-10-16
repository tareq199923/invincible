# invincible/agent/runner.py
"""The local agent loop (Phase 10): poll the server, run confirmed
jobs on this machine, post results back.

One loop, no threads: httpx AsyncClient against the paired server,
authenticating every request with the inv_ key ``invincible login``
saved to ~/.invincible/config.json. ``POST /agent/poll`` holds up to
~25s server-side; any network failure backs off 2s and retries -
flaky WiFi, tunnel restarts, and server redeploys (the in-memory
registry drops with the process, agents re-register on next poll) all
look like "poll again soon".

Execution policy on this side, in order:

- Wall 2: re-run the SAME denylist the server ran
  (tool_executor.check_denylist for execute_bash; this package's
  sandbox for read/write). Defense in depth - a command crafted to
  hit something bad here that the server's patterns missed is caught
  locally. A local block is returned as the job result (status
  "blocked"), never silently dropped, so the AI sees it and the
  server can audit it.
- Then execute with the EXACT functions the server uses today
  (tool_executor._run_command with its timeout + kill-on-timeout
  logic, tool_executor._write_file, tool_executor._read_file with its
  content cap), so behavior is byte-identical
  wherever the work happens - same JSON result shapes, same failure
  dicts, no protocol changes anywhere.
- The process runs as the logged-in user, with exactly their
  privileges, never elevated.

Ctrl+C exits cleanly. Every job result is posted via
/agent/result; the server resolves the waiting /mcp request, and
rejected/duplicate results (job already timed out, not ours) are the
server's business - the agent just logs and moves on.
"""
import asyncio
import contextlib
import json
import os
import platform
import shutil
import time
import uuid
from urllib.parse import urlparse, urlunparse

import httpx

from invincible.agent import overlay, sandbox
from invincible.core import harness_tools, tool_executor
from invincible.core.harness_tunnel import (
    MAX_FRAME_BYTES,
    STREAM_IDLE_SECONDS,
    FrameError,
    decode_data_frame,
    encode_data_frame,
    validate_request_head,
)
from invincible.core.settings import AGENT_POLL_HOLD_SECONDS, settings

POLL_BACKOFF_SECONDS = 2.0

# Agent-side tunnel state (F03). _TUNNELS maps tunnel_id -> port for
# tunnels THIS agent acked (bytes for other machines' tunnels never
# arrive — the server pins sends to the acking socket — and stale ids
# are ignored). _STREAMS maps (tunnel_id, stream_id) -> pump task;
# _INBOUND maps the same key -> bounded queue of request-body chunks.
_TUNNELS: dict[str, int] = {}
_STREAMS: dict[tuple[str, str], asyncio.Task] = {}
_INBOUND: dict[tuple[str, str], asyncio.Queue] = {}
_STREAM_SEQ: dict[tuple[str, str], int] = {}

_TUNNEL_INBOUND = frozenset({
    "tunnel_open",
    "tunnel_request_head",
    "tunnel_data",
    "tunnel_half_close",
    "tunnel_close",
})

_TUNNEL_ACK_ERROR_MAX = 200

# F11 overlay handle (child process, best-effort). ``None`` until the
# first ensure; a dead overlay retries at most once a minute so a
# machine without a desktop never pays a spawn per job.
_OVERLAY: overlay.Overlay | None = None
_OVERLAY_RETRY_AT = 0.0
OVERLAY_RETRY_COOLDOWN_SECONDS = 60.0


def ensure_overlay() -> overlay.Overlay | None:
    """Live overlay handle, or None when headless. Never raises.

    Headless covers: off-Windows, INVINCIBLE_OVERLAY=0, spawn failure,
    or a dead child inside its retry cooldown. Callers skip the
    pre-action dwell when this returns None.
    """
    global _OVERLAY, _OVERLAY_RETRY_AT
    if os.name != "nt" or not settings.overlay_enabled():
        return None
    if _OVERLAY is None:
        _OVERLAY = overlay.Overlay()
    if _OVERLAY.alive:
        return _OVERLAY
    if time.monotonic() < _OVERLAY_RETRY_AT:
        return None
    if _OVERLAY.start():
        return _OVERLAY
    _OVERLAY_RETRY_AT = (
        time.monotonic() + OVERLAY_RETRY_COOLDOWN_SECONDS)
    return None


def stop_overlay() -> None:
    """Terminate the overlay child. Never raises; safe to call twice."""
    global _OVERLAY
    current, _OVERLAY = _OVERLAY, None
    if current is not None:
        with contextlib.suppress(Exception):
            current.stop()


def handle_handoff(handoff: dict | None) -> None:
    """B2 arrival: wake the B1 overlay so Bmax visibly hops to the PC.

    Best-effort and never raises (same isolation as every overlay
    call): headless/off-Windows/kill-switched machines just log.
    ``handoff`` is display data only — malformed shapes are ignored.
    """
    if not isinstance(handoff, dict):
        return
    task = handoff.get("task", "")
    shown = str(task)[:200] if isinstance(task, str) else ""
    print(f"[agent] handoff arrived{(': ' + shown) if shown else ''}")
    with contextlib.suppress(Exception):
        seen = ensure_overlay()
        if seen is not None and seen.alive:
            seen.settle()


def _silence(task: asyncio.Task) -> None:
    """Swallow a background task's exception on completion (stream pumps
    report failure by closing the stream, never by raising into the WS
    loop — an unretrieved exception would log noise per stream)."""
    def _cb(done: asyncio.Task) -> None:
        if not done.cancelled():
            with contextlib.suppress(Exception):
                done.exception()

    task.add_done_callback(_cb)


def machine_id() -> str:
    """Stable per-machine id (H6c): ``INVINCIBLE_MACHINE_ID`` wins, else a
    load-or-create ``~/.invincible/machine_id`` (uuid4 hex). Best-effort —
    a fresh id on failure modes that only costs dashboard display churn,
    never execution (routing keys on user_id, not machine_id)."""
    override = os.getenv("INVINCIBLE_MACHINE_ID", "").strip()
    if override:
        return override[:64]
    path = os.path.join(os.path.expanduser("~"), ".invincible",
                        "machine_id")
    try:
        with open(path, encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            return saved[:64]
    except OSError:
        pass
    fresh = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(fresh)
    except OSError:
        pass
    return fresh


def ws_url_for(base_url: str) -> str:
    """Convert an http(s) server URL to the ws(s) relay URL (/agent/ws)."""
    parts = urlparse(base_url.rstrip("/"))
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, "ws")
    return urlunparse((scheme, parts.netloc, "/agent/ws", "", "", ""))


def hello_frame() -> dict:
    """H1 hello (+H6c machine_id): machine identity + capability
    advertisement (auto-discovery, cheap probes only)."""
    return {
        "type": "hello",
        "machine_id": machine_id(),
        "machine_name": platform.node(),
        "platform": platform.platform(),
        "capabilities": {
            "ripgrep": shutil.which("rg") is not None,
            "docker": shutil.which("docker") is not None,
            "chrome": tool_executor._find_chrome() is not None,
        },
    }


async def run_agent_ws(
    base_url: str,
    api_key: str,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """WS relay loop (H1): outbound-only connection, server
    pushes jobs, agent replies with results. Raises on disconnect so the
    caller falls back to polling.

    `websockets` is imported lazily so a missing optional dep degrades to
    the poll loop instead of crashing the agent.
    """
    import websockets

    stop = stop or asyncio.Event()
    url = ws_url_for(base_url) + f"?api_key={api_key}"
    heartbeat = settings.harness_ws_heartbeat_seconds()

    async def _one_ws_message(ws) -> None:
        raw = await asyncio.wait_for(ws.recv(), timeout=heartbeat * 3)
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(message, dict):
            return
        if message.get("type") in _TUNNEL_INBOUND:
            # F03: tunnel frames ride the same relay; jobs and byte
            # streams interleave by type key (neither carries the
            # other's keys, so neither parser can misroute).
            await _handle_tunnel_frame(ws, message)
            return
        if message.get("type") == "handoff":
            # B2: fire-and-forget phone→PC task (control-plane, not a
            # tool job — no result is posted back). Wakes the overlay
            # so the hop is visible, then the loop keeps waiting.
            with contextlib.suppress(Exception):
                handle_handoff(message.get("handoff"))
            return
        job = (message.get("job")
               if message.get("type") == "job" else None)
        if job is None:
            return
        print(f"[agent] job {job.get('job_id')}: {job.get('type')} (ws)")
        result = await execute_job(job)
        await ws.send(json.dumps(
            {"job_id": job["job_id"], "result": result}))

    async with websockets.connect(
        url, ping_interval=heartbeat, ping_timeout=heartbeat,
    ) as ws:
        await ws.send(json.dumps(hello_frame()))
        ensure_overlay()  # F11: Bmax arrives with the agent (no-op
        # off-Windows / headless / INVINCIBLE_OVERLAY=0)
        try:
            while not stop.is_set():
                await _one_ws_message(ws)
        finally:
            # C2, agent side: every TCP dial dies with the relay.
            await _teardown_tunnels(ws)
            stop_overlay()


async def execute_job(job: dict) -> dict:
    """Run one dispatched job locally. Wall 2 re-check first; a block
    is the job's result, not an exception."""
    job_type = job.get("type")
    args = job.get("args") or {}

    try:
        if job_type == "execute_bash":
            tool_executor.check_denylist(args.get("command", ""))
            return await tool_executor._run_command(
                args.get("command", ""), float(args.get("timeout", 30.0))
            )
        if job_type == "write_file":
            sandbox.check_agent_write(args.get("path", ""))
            return await tool_executor._write_file(
                args.get("path", ""), args.get("content", "")
            )
        if job_type == "edit_file":
            sandbox.check_agent_write(args.get("path", ""))
            return await tool_executor._edit_file(
                args.get("path", ""), args.get("old_string", ""),
                args.get("new_string", ""),
                bool(args.get("replace_all", False)),
            )
        if job_type == "read_file":
            sandbox.check_agent_read(args.get("path", ""))
            return await _read_local(
                args.get("path", ""),
                args.get("offset"), args.get("limit"))
        if job_type == "code_search":
            # H6a: home sandbox re-check (Wall 2), then the EXACT shared
            # search the server uses — byte-identical result shapes.
            sandbox.check_agent_read(args.get("path", "") or ".")
            return await tool_executor._search_code(
                args.get("pattern", ""), args.get("path", ""),
                int(args.get("max_results")
                    or tool_executor.SEARCH_DEFAULT_MAX_RESULTS),
            )
        if job_type == "list_dir":
            # Read-only like code_search: home sandbox re-check, then
            # the EXACT shared listing the server uses.
            sandbox.check_agent_read(args.get("path", "") or ".")
            try:
                dir_limit = int(
                    args.get("limit")
                    or tool_executor.LIST_DIR_DEFAULT_LIMIT
                )
            except (TypeError, ValueError):
                dir_limit = tool_executor.LIST_DIR_DEFAULT_LIMIT
            return await tool_executor._list_dir(
                args.get("path", "") or ".", dir_limit,
                bool(args.get("show_hidden", False)),
            )
        if job_type == "find_files":
            # Read-only like list_dir: home sandbox re-check, then
            # the EXACT shared finder the server uses.
            sandbox.check_agent_read(args.get("path", "") or ".")
            try:
                find_limit = int(
                    args.get("max_results")
                    or tool_executor.FIND_DEFAULT_MAX_RESULTS
                )
            except (TypeError, ValueError):
                find_limit = tool_executor.FIND_DEFAULT_MAX_RESULTS
            return await tool_executor._find_files(
                args.get("pattern", ""), args.get("path", "") or ".",
                find_limit, bool(args.get("show_hidden", False)),
            )
        if job_type == "git_status":
            # Read-only git inspection: same sandbox as reads.
            sandbox.check_agent_read(args.get("path", "") or ".")
            return await tool_executor._git_status(
                args.get("path", "") or ".")
        if job_type == "git_diff":
            sandbox.check_agent_read(args.get("path", "") or ".")
            return await tool_executor._git_diff(
                args.get("path", "") or ".")
        if job_type == "git_log":
            sandbox.check_agent_read(args.get("path", "") or ".")
            try:
                log_limit = int(
                    args.get("limit")
                    or tool_executor.GIT_LOG_DEFAULT_LIMIT
                )
            except (TypeError, ValueError):
                log_limit = tool_executor.GIT_LOG_DEFAULT_LIMIT
            return await tool_executor._git_log(
                args.get("path", "") or ".", log_limit)
        if job_type == "process_list":
            # H6a: no path, no network — runs as-is with the caller's own
            # privileges, same as execute_bash post-approval.
            return await tool_executor._list_processes(
                int(args.get("limit")
                    or tool_executor.PROCESSES_DEFAULT_LIMIT),
            )
        if job_type == "screenshot":
            # H6a: URL-shape refusal happens inside _take_screenshot
            # (non-http(s) is an error result, never an exception); no
            # home path is touched so no sandbox check applies.
            return await tool_executor._take_screenshot(
                args.get("url", ""),
                float(args.get("timeout", 30.0)),
            )
        if job_type == "web_fetch":
            # Same agent-only posture as screenshot: URL-shape refusal
            # happens inside _fetch_url; no home path is touched so no
            # sandbox check applies.
            return await tool_executor._fetch_url(
                args.get("url", ""),
                float(args.get("timeout", 30.0)),
            )
        if job_type == "look_at_screen":
            # F09: no args, no paths, no URLs - reads the frontmost
            # window's controls on this machine; no sandbox check
            # applies. Non-Windows answers unavailable, never raises.
            return await tool_executor._look_at_screen()
        if job_type in tool_executor.COMPUTER_TOOLS:
            # F10: paired-PC input + launch. Shape + denylist are
            # re-checked inside _computer_act (Wall 2); ToolBlocked
            # becomes the job's blocked result via the handler below.
            # Non-Windows answers unavailable, never raises.
            # F11: resolved points glide the overlay BEFORE the action
            # (pre-action dwell only when an overlay is alive); the
            # head settles back to its dock corner after. Every overlay
            # call is best-effort - a dead overlay never breaks a job.
            seen = ensure_overlay()
            if seen is not None and job_type == "drag":
                _seen = seen

                def sink(point, _seen=_seen):  # type: ignore[misc]
                    with contextlib.suppress(Exception):
                        _seen.announce(point, dragging=True)
            else:
                sink = seen.announce if seen is not None else None
            dwell = (overlay.OVERLAY_PRE_ACTION_DWELL_SECONDS
                     if seen is not None and seen.alive else 0.0)
            try:
                return await tool_executor._computer_act(
                    job_type, args if isinstance(args, dict) else {},
                    on_point=sink, pre_action_delay=dwell)
            finally:
                if seen is not None:
                    with contextlib.suppress(Exception):
                        seen.settle()
        return {
            "status": "error",
            "error": f"Unknown job type: {job_type}. Valid job types: "
                     f"{', '.join(harness_tools.agent_job_names())}.",
        }
    except tool_executor.ToolBlocked as e:
        return {"status": "blocked", "reason": e.reason}


async def _tunnel_send(ws, frame: dict) -> bool:
    """Best-effort tunnel frame send (False when the relay is gone —
    the caller kills the stream; the WS loop owns reconnect)."""
    try:
        await ws.send(json.dumps(frame))
    except Exception:
        return False
    return True


async def _tunnel_close_stream(ws, tunnel_id: str, stream_id: str) -> None:
    """Idempotent stream teardown: cancel the pump, drop bookkeeping,
    tell the server (its unknown-stream handling ignores dupes)."""
    key = (tunnel_id, stream_id)
    task = _STREAMS.pop(key, None)
    if task is not None:
        task.cancel()
    _INBOUND.pop(key, None)
    _STREAM_SEQ.pop(key, None)
    await _tunnel_send(ws, {
        "type": "tunnel_close",
        "tunnel_id": tunnel_id,
        "stream_id": stream_id,
    })


async def _teardown_tunnels(ws) -> None:
    """C2, agent side: on WS exit close every TCP dial and forget every
    tunnel. The server hard-closes its side independently; both ends
    converge on closed with no resume attempt."""
    for task in list(_STREAMS.values()):
        task.cancel()
    _STREAMS.clear()
    _INBOUND.clear()
    _STREAM_SEQ.clear()
    _TUNNELS.clear()
    if ws is not None:
        await _tunnel_send(ws, {"type": "tunnel_close", "tunnel_id": ""})


async def _handle_tunnel_open(ws, message: dict) -> None:
    """Wall 2 + liveness probe: re-check the port, verify something
    actually listens on 127.0.0.1:port, then claim the tunnel and ack.
    A refusal is the ack's result, never an exception."""
    tunnel_id = str(message.get("tunnel_id", ""))
    if not tunnel_id:
        return
    try:
        port = sandbox.check_tunnel_port(message.get("port", ""))
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=10)
    except Exception as e:
        await _tunnel_send(ws, {
            "type": "tunnel_open_ack",
            "tunnel_id": tunnel_id,
            "ok": False,
            "error": f"{e.__class__.__name__}: {e}"[:_TUNNEL_ACK_ERROR_MAX],
        })
        return
    try:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    except Exception:
        pass
    _TUNNELS[tunnel_id] = port
    await _tunnel_send(ws, {
        "type": "tunnel_open_ack",
        "tunnel_id": tunnel_id,
        "ok": True,
    })


def _parse_response_head(raw: bytes) -> tuple[int, dict, bytes]:
    """Split one origin response into (status, headers, body_prefix).

    Raises on any shape violation — the caller kills the stream, fail
    closed. Header names/values decode latin-1 (HTTP field octets);
    count capped (100) like the server-side validator.
    """
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines or not lines[0]:
        raise ValueError("empty response head")
    parts = lines[0].decode("latin-1").split(" ", 2)
    status = int(parts[1])
    if not 100 <= status <= 599:
        raise ValueError(f"bad status {parts[1]!r}")
    headers: dict[str, str] = {}
    for line in lines[1:101]:
        name, _, value = line.decode("latin-1").partition(":")
        if name.strip():
            headers[name.strip()] = value.strip()
    return status, headers, rest


async def _serve_stream(ws, tunnel_id: str, stream_id: str,
                        method: str, path: str, headers: dict) -> None:
    """One tunneled HTTP exchange: dial localhost, forward the request,
    de-chunk + relay the response. Any violation kills the stream (the
    server 502s that stream); the tunnel and relay stay up."""
    key = (tunnel_id, stream_id)
    port = _TUNNELS.get(tunnel_id)
    if port is None:
        return
    send_headers = {
        k: v for k, v in headers.items()
        if k.lower() not in (
            # Rebuilt by the agent (Host) or forced safe (Connection,
            # Accept-Encoding); Content-Length re-added from the frame
            # when present. Authorization/Cookie are stripped at BOTH
            # ends (the server filters first; this is the backstop so
            # the tunnel can never become a credential-forwarding
            # oracle against local services).
            "host", "connection", "accept-encoding", "content-length",
            "authorization", "cookie")
    }
    if method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        inbound = _INBOUND.get(key)
        if inbound is not None:
            with contextlib.suppress(Exception):
                length = int(headers.get("Content-Length")
                             or headers.get("content-length") or 0)
                if length > 0:
                    send_headers["Content-Length"] = str(length)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=10)
    except Exception:
        await _tunnel_close_stream(ws, tunnel_id, stream_id)
        return
    try:
        head_lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            "Connection: close",
            "Accept-Encoding: identity",
        ]
        head_lines += [f"{k}: {v}" for k, v in send_headers.items()]
        writer.write(("\r\n".join(head_lines) + "\r\n\r\n").encode(
            "latin-1"))
        await writer.drain()

        async def _forward_body() -> None:
            inbound = _INBOUND.get(key)
            if inbound is None:
                return
            while True:
                chunk = await inbound.get()
                if chunk is None:  # half-close sentinel: end write side
                    with contextlib.suppress(Exception):
                        writer.write_eof()
                    return
                writer.write(chunk)
                await writer.drain()

        body_task = asyncio.ensure_future(_forward_body())
        _silence(body_task)
        try:
            raw_head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=STREAM_IDLE_SECONDS)
        except (asyncio.LimitOverrunError, asyncio.TimeoutError,
                asyncio.IncompleteReadError, Exception):
            body_task.cancel()
            await _tunnel_close_stream(ws, tunnel_id, stream_id)
            return
        try:
            status, resp_headers, prefix = _parse_response_head(raw_head)
        except (ValueError, IndexError):
            body_task.cancel()
            await _tunnel_close_stream(ws, tunnel_id, stream_id)
            return
        chunked = "chunked" in resp_headers.get(
            "Transfer-Encoding", resp_headers.get(
                "transfer-encoding", "")).lower()
        resp_headers.pop("Transfer-Encoding", None)
        resp_headers.pop("transfer-encoding", None)
        if chunked:
            # De-chunked below, so any origin framing headers go.
            resp_headers.pop("Content-Length", None)
            resp_headers.pop("content-length", None)
        if not await _tunnel_send(ws, {
            "type": "tunnel_response_head",
            "tunnel_id": tunnel_id,
            "stream_id": stream_id,
            "status": status,
            "headers": resp_headers,
        }):
            body_task.cancel()
            await _tunnel_close_stream(ws, tunnel_id, stream_id)
            return
        seq = 0

        async def _emit(raw: bytes) -> bool:
            nonlocal seq
            if not raw:
                return True
            try:
                frame = encode_data_frame(
                    tunnel_id, stream_id, seq, raw)
            except FrameError:
                return False
            seq += 1
            return await _tunnel_send(ws, frame)

        if prefix and not await _emit(prefix):
            body_task.cancel()
            await _tunnel_close_stream(ws, tunnel_id, stream_id)
            return
        while True:
            try:
                if chunked:
                    size_line = await asyncio.wait_for(
                        reader.readuntil(b"\r\n"),
                        timeout=STREAM_IDLE_SECONDS)
                    size = int(size_line.split(b";")[0].strip(), 16)
                    if size == 0:
                        break
                    if size > MAX_FRAME_BYTES * 64:
                        break  # absurd chunk: end, fail closed
                    data = await asyncio.wait_for(
                        reader.readexactly(size),
                        timeout=STREAM_IDLE_SECONDS)
                    await asyncio.wait_for(
                        reader.readexactly(2),
                        timeout=STREAM_IDLE_SECONDS)
                    ok = await _emit(data)
                else:
                    data = await asyncio.wait_for(
                        reader.read(MAX_FRAME_BYTES),
                        timeout=STREAM_IDLE_SECONDS)
                    if not data:
                        break  # origin EOF (Connection: close)
                    ok = await _emit(data)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                    ValueError, Exception):
                break  # idle/ragged close: end the stream, not the relay
            if not ok:
                break
        body_task.cancel()
    except Exception:
        # Unexpected pump failure (never the relay's problem): the
        # stream dies here; _silence keeps the task quiet.
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
    await _tunnel_close_stream(ws, tunnel_id, stream_id)


async def _handle_tunnel_frame(ws, message: dict) -> None:
    """Route one server→agent tunnel frame (called from the WS loop)."""
    kind = message.get("type")
    if kind == "tunnel_open":
        await _handle_tunnel_open(ws, message)
        return
    tunnel_id = str(message.get("tunnel_id", ""))
    stream_id = str(message.get("stream_id", ""))
    key = (tunnel_id, stream_id)
    if kind == "tunnel_close" and not stream_id:
        # Whole-tunnel teardown: drop ownership + every stream.
        _TUNNELS.pop(tunnel_id, None)
        for (tid, sid) in [k for k in _STREAMS if k[0] == tunnel_id]:
            await _tunnel_close_stream(ws, tid, sid)
        return
    if kind == "tunnel_close":
        await _tunnel_close_stream(ws, tunnel_id, stream_id)
        return
    if tunnel_id not in _TUNNELS:
        return  # not ours (stale or another machine's): ignore
    if kind == "tunnel_request_head":
        try:
            method, path, headers = validate_request_head(message)
        except FrameError:
            return
        if key in _STREAMS:
            return  # duplicate head: ignore, fail closed
        _INBOUND[key] = asyncio.Queue(maxsize=64)
        _STREAM_SEQ[key] = 0
        task = asyncio.ensure_future(
            _serve_stream(ws, tunnel_id, stream_id,
                          method, path, headers))
        _silence(task)
        _STREAMS[key] = task
        return
    if kind == "tunnel_data":
        # Request-body bytes for a live stream (seq-checked: the relay
        # is FIFO, so a break means corruption — kill the stream).
        try:
            chunk = decode_data_frame(message)
        except FrameError:
            await _tunnel_close_stream(ws, tunnel_id, stream_id)
            return
        try:
            seq = int(message.get("seq", -1))
        except (TypeError, ValueError):
            seq = -1
        inbound = _INBOUND.get(key)
        if inbound is None or seq != _STREAM_SEQ.get(key, 0):
            await _tunnel_close_stream(ws, tunnel_id, stream_id)
            return
        _STREAM_SEQ[key] = seq + 1
        try:
            inbound.put_nowait(chunk)
        except asyncio.QueueFull:
            await _tunnel_close_stream(ws, tunnel_id, stream_id)
        return
    if kind == "tunnel_half_close":
        inbound = _INBOUND.get(key)
        if inbound is not None:
            with contextlib.suppress(asyncio.QueueFull):
                inbound.put_nowait(None)  # write-side EOF sentinel
        return


async def _read_local(path: str, offset=None, limit=None) -> dict:
    """Same read result shapes tool_executor.read_file produces, with
    the agent sandbox as the gate instead of the server's read
    roots."""
    return await tool_executor._read_file(path, offset, limit)


async def run_agent(base_url: str, api_key: str,
                    *, client: httpx.AsyncClient | None = None,
                    stop: asyncio.Event | None = None) -> None:
    """The polling loop. ``client`` and ``stop`` are injectable so
    tests drive this hermetically against the ASGI app (the same
    pattern ``_pair_device`` uses).

    Shutdown promptness: a parked poll (up to the server's 25s hold)
    is abandoned the moment ``stop`` fires - the poll request itself
    is raced against the stop event rather than awaited blind, so
    Ctrl+C never waits out the hold."""
    owns_client = client is None
    http = client or httpx.AsyncClient(
        base_url=base_url, timeout=AGENT_POLL_HOLD_SECONDS + 5
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    stop = stop or asyncio.Event()
    announced = False  # one-time "connected" after the first good poll
    ensure_overlay()  # F11: Bmax arrives with the agent (no-op
    # off-Windows / headless / INVINCIBLE_OVERLAY=0)

    async def _one_cycle() -> None:
        nonlocal announced
        polled = await http.post("/agent/poll", headers=headers)
        if polled.status_code == 401:
            # Revoked or unknown key: retrying with the same
            # credential can never succeed - stop instead of
            # hot-looping denials into the audit log.
            print("[agent] key rejected (401) - run "
                  "`invincible login` to re-pair. Stopping.")
            stop.set()
            return
        polled.raise_for_status()
        if not announced:
            announced = True
            print("[agent] connected - waiting for jobs from your AI")
        job = polled.json().get("job")
        handoff = polled.json().get("handoff")
        if handoff is not None:
            # B2 poll fallback: WS-less PCs hop here. Fire-and-forget
            # (no result posts back); a dead overlay never breaks jobs.
            with contextlib.suppress(Exception):
                handle_handoff(handoff.get("handoff")
                               if isinstance(handoff, dict) else None)
        if job is None:
            return
        print(f"[agent] job {job.get('job_id')}: {job.get('type')}")
        result = await execute_job(job)
        posted = await http.post(
            "/agent/result",
            headers=headers,
            json={"job_id": job["job_id"], "result": result},
        )
        if posted.status_code == 200 and \
                not posted.json().get("accepted", False):
            print(f"[agent] result for {job['job_id']} not "
                  "accepted (timed out or duplicate) - dropped")

    try:
        while not stop.is_set():
            try:
                cycle = asyncio.ensure_future(_one_cycle())
                stop_wait = asyncio.ensure_future(stop.wait())
                done, pending = await asyncio.wait(
                    {cycle, stop_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cycle in pending:
                    # stop fired mid-cycle: abandon the parked poll
                    cycle.cancel()
                    break
                stop_wait.cancel()
                await cycle
            except (httpx.HTTPError, httpx.StreamError) as e:
                # Network hiccup, tunnel restart, or a server redeploy.
                # Back off and poll again - the loop IS the retry.
                print(f"[agent] connection issue ({e.__class__.__name__})"
                      f" - retrying in {POLL_BACKOFF_SECONDS:.0f}s")
                await asyncio.sleep(POLL_BACKOFF_SECONDS)
            except asyncio.CancelledError:
                raise
    finally:
        stop_overlay()
        if owns_client:
            await http.aclose()


async def run_harness(
    base_url: str,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Harness entry (H1): WS-first with long-poll fallback.

    Tries the WS relay once when enabled and the `websockets` package is
    importable; any failure (missing dep, disabled flag, disconnect) falls
    back to the proven `run_agent()` poll loop, which IS the retry. This is
    what `harness connect` runs; `run_agent()` stays as the poll-only core
    the fallback (and the hermetic tests) drive directly.
    """
    stop = stop or asyncio.Event()
    if settings.harness_ws_enabled() and client is None:
        try:
            await run_agent_ws(base_url, api_key, stop=stop)
            return
        except Exception as e:
            print(f"[agent] ws unavailable ({e.__class__.__name__}) - "
                  "falling back to polling")
    await run_agent(base_url, api_key, client=client, stop=stop)
