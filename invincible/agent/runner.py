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
  logic, tool_executor._write_file), so behavior is byte-identical
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
import json
import os
import platform
import shutil
import uuid
from urllib.parse import urlparse, urlunparse

import httpx

from invincible.agent import sandbox
from invincible.core import tool_executor
from invincible.core.settings import AGENT_POLL_HOLD_SECONDS, settings

POLL_BACKOFF_SECONDS = 2.0


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
    advertisement (flexx-style auto-discovery, cheap `shutil.which`
    probes only)."""
    return {
        "type": "hello",
        "machine_id": machine_id(),
        "machine_name": platform.node(),
        "platform": platform.platform(),
        "capabilities": {
            "ripgrep": shutil.which("rg") is not None,
            "docker": shutil.which("docker") is not None,
            "chrome": any(
                shutil.which(name) is not None
                for name in (
                    "google-chrome", "chrome", "chromium",
                    "chrome.exe", "msedge",
                )
            ),
        },
    }


async def run_agent_ws(
    base_url: str,
    api_key: str,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """WS relay loop (H1, flexx-style): outbound-only connection, server
    pushes jobs, agent replies with results. Raises on disconnect so the
    caller falls back to polling.

    `websockets` is imported lazily so a missing optional dep degrades to
    the poll loop instead of crashing the agent.
    """
    import websockets

    stop = stop or asyncio.Event()
    url = ws_url_for(base_url) + f"?api_key={api_key}"
    heartbeat = settings.harness_ws_heartbeat_seconds()
    async with websockets.connect(
        url, ping_interval=heartbeat, ping_timeout=heartbeat,
    ) as ws:
        await ws.send(json.dumps(hello_frame()))
        while not stop.is_set():
            raw = await asyncio.wait_for(ws.recv(), timeout=heartbeat * 3)
            try:
                message = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            job = (message.get("job")
                   if message.get("type") == "job" else None)
            if job is None:
                continue
            print(f"[agent] job {job.get('job_id')}: {job.get('type')} (ws)")
            result = await execute_job(job)
            await ws.send(json.dumps(
                {"job_id": job["job_id"], "result": result}))


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
        if job_type == "read_file":
            sandbox.check_agent_read(args.get("path", ""))
            return await _read_local(args.get("path", ""))
        if job_type == "code_search":
            # H6a: home sandbox re-check (Wall 2), then the EXACT shared
            # search the server uses — byte-identical result shapes.
            sandbox.check_agent_read(args.get("path", "") or ".")
            return await tool_executor._search_code(
                args.get("pattern", ""), args.get("path", ""),
                int(args.get("max_results")
                    or tool_executor.SEARCH_DEFAULT_MAX_RESULTS),
            )
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
        return {
            "status": "error",
            "error": f"Unknown job type: {job_type}",
        }
    except tool_executor.ToolBlocked as e:
        return {"status": "blocked", "reason": e.reason}


async def _read_local(path: str) -> dict:
    """Same read result shapes tool_executor.read_file produces, with
    the agent sandbox as the gate instead of the server's read
    roots."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"status": "read", "path": path, "content": content}
    except FileNotFoundError:
        return {"status": "error", "error": f"File not found: {path}"}
    except IsADirectoryError:
        return {"status": "error",
                "error": f"Path is a directory, not a file: {path}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


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
