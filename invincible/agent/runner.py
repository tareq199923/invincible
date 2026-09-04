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

import httpx

from invincible.agent import sandbox
from invincible.core import tool_executor
from invincible.core.settings import AGENT_POLL_HOLD_SECONDS

POLL_BACKOFF_SECONDS = 2.0


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
