# invincible/core/agent_registry.py
"""In-memory agent liveness tracking + job dispatch to paired agents
(Phase 10, extended H1 with WebSocket relay).

The golden rule of the agent design: the server decides what's allowed
(denylist, staging, confirm tokens, per-user binding - all unchanged in
tool_executor), and the agent only does the work. This module is the
plumbing that moves *confirmed* work down an agent's own open connection
and carries the result back to the holding /mcp request.

Two transports, one dispatch table (H1):
- long-poll ``POST /agent/poll`` (https, httpx, no new packages)
- WebSocket ``WS /agent/ws`` (outbound-only from the agent, flexx-style
  relay; WS-first with long-poll fallback)

Each poll AND each WS attach/message is a heartbeat, so "online" means
"polled or messaged within AGENT_ONLINE_TTL_SECONDS".

Isolation is structural, not a policy check: every queued job carries
its owner's user_id, queues and futures are keyed by user_id, and
submit_result refuses results for jobs staged by a different user. An
agent authenticated as user 42 is only ever handed user 42's work;
user 1's commands cannot reach user 2's PC because no code path tries.

Single-instance, in-memory by design - the same trade-off as the
default PendingActionStore: a restart orphans in-flight jobs (the
holding /mcp request then fails with a connection reset, and the
client retries end-to-end) and every agent re-registers itself on its
next poll/connect. No persistence layer, no migration.
"""
import asyncio
import secrets
import time
from collections import deque

from invincible.core.settings import (
    AGENT_ONLINE_TTL_SECONDS,
    AGENT_POLL_HOLD_SECONDS,
)


class PollCapacityExceeded(Exception):
    """More concurrent held polls for one user than MAX_POLLS_PER_USER.

    Raised by ``poll`` when a user already holds the cap's worth of open
    long-poll connections. The endpoint maps it to a 429 so the excess
    connection sheds immediately instead of parking another hold.
    """


# One paired machine runs one poll loop, so even a user with agents on
# several machines stays well under this. The cap exists purely to bound
# per-user held connections on the single-replica deployment (multi-tenant
# audit LOW-5): the counter is plain dict state, race-free under the
# event loop's single-threaded execution.
MAX_POLLS_PER_USER = 5

# H1: same bound for concurrent WebSocket connections per user. WS and
# polls are counted separately (an agent uses one or the other).
MAX_WS_PER_USER = 5

# H6c: bound on tracked machines per user. Eviction is oldest-first, so
# a reinstall storm can't grow the table unbounded (same discipline as
# the STALE_USER_SECONDS sweep below).
MAX_MACHINES_PER_USER = 20


class AgentRegistry:
    """Liveness + dispatch table for paired agents.

    Layout (all keyed by user_id unless noted):

    - ``_last_seen`` - wall-clock timestamp of the user's last poll.
      ``time.time()`` (not monotonic) purely for consistency with
      PendingActionStore; nothing here crosses a process boundary.
    - ``_pollers`` - count of this user's currently held poll calls.
    - ``_queues`` - jobs waiting to be picked up by a poll.
    - ``_events`` - per-user event a held poll sleeps on; ``dispatch``
      sets it so an already-connected poll answers immediately.
    - ``_futures`` - job_id -> Future the dispatching /mcp request
      awaits; ``submit_result`` resolves it.
    - ``_jobs`` - job_id -> owner user_id, for the single-use/owner
      checks in submit_result. Entries live until resolved or swept.

    Events are created lazily and *kept* (not popped after a wake) so
    concurrent dispatches and the next held poll can share one event;
    ``Event.set()`` followed by ``Event.clear()`` happens under the
    loop's single-threaded execution, so no wake is ever lost.
    """

    def __init__(self, *, clock=time.time):
        self._last_seen: dict[int, float] = {}
        self._pollers: dict[int, int] = {}
        self._queues: dict[int, deque] = {}
        self._events: dict[int, asyncio.Event] = {}
        self._futures: dict[int, dict[str, asyncio.Future]] = {}
        # job_id -> owner user_id, for the single-use/owner checks in
        # submit_result. Entries live until resolved or swept.
        self._jobs: dict[str, dict] = {}
        # H1: user_id -> live WebSocket connections (Starlette WebSocket
        # objects; tests use fakes with an async send_json). Sets, so one
        # user with N machines holds N entries.
        self._ws: dict[int, set] = {}
        # H6c: user_id -> machine_id -> advertised info. Survives
        # disconnect (entries go stale → reported offline) so the
        # dashboard lists machines that were seen, not just connected ones.
        self._machines: dict[int, dict[str, dict]] = {}
        self._last_prune = 0.0
        self._clock = clock

    # How long a user must have been offline, holding nothing in flight,
    # before their bookkeeping is forgotten. Without a sweep these three
    # dicts gain an entry per user who ever polls and keep it forever -
    # small each, unbounded over time (deep code review 2026-09-24,
    # finding 9).
    STALE_USER_SECONDS = 3600.0
    # The sweep is O(tracked users), so it runs at most this often rather
    # than on every heartbeat: a poll is the hot path, a stale entry is
    # not urgent.
    PRUNE_INTERVAL_SECONDS = 60.0

    # --- liveness -------------------------------------------------------

    def heartbeat(self, user_id: int) -> None:
        """Record that this user's agent just polled or messaged (H1: both
        transports count as liveness)."""
        self._last_seen[user_id] = self._clock()
        self._prune()

    def _prune(self) -> None:
        """Forget users who are long gone and hold nothing in flight.

        Deliberately conservative. A user is only forgotten once they have
        been offline past STALE_USER_SECONDS AND hold no held poll, no
        queued job and no waiting dispatcher - anything else may still be
        live. The event object in particular is kept for that reason: a
        dispatch and the next poll are meant to share one.
        """
        now = self._clock()
        if now - self._last_prune < self.PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        cutoff = now - self.STALE_USER_SECONDS
        for user_id, seen in list(self._last_seen.items()):
            if seen > cutoff:
                continue
            if (self._pollers.get(user_id)
                    or self._queues.get(user_id)
                    or self._futures.get(user_id)
                    or self._ws.get(user_id)
                    or self._machines.get(user_id)):
                continue
            self._last_seen.pop(user_id, None)
            self._events.pop(user_id, None)
            self._queues.pop(user_id, None)
            self._ws.pop(user_id, None)
            self._machines.pop(user_id, None)

    def online(self, user_id: int) -> bool:
        last = self._last_seen.get(user_id)
        return (
            last is not None
            and (self._clock() - last) <= AGENT_ONLINE_TTL_SECONDS
        )

    def last_seen(self, user_id: int) -> float | None:
        return self._last_seen.get(user_id)

    # --- websocket relay (H1) -----------------------------------------

    def attach_ws(self, user_id: int, ws) -> None:
        """Register one live WS connection. Heartbeats (WS counts as
        liveness). Raises PollCapacityExceeded past MAX_WS_PER_USER."""
        sockets = self._ws.setdefault(user_id, set())
        if len(sockets) >= MAX_WS_PER_USER:
            raise PollCapacityExceeded(
                f"user {user_id} already holds {len(sockets)} websockets "
                f"(cap {MAX_WS_PER_USER})")
        sockets.add(ws)
        self.heartbeat(user_id)

    def detach_ws(self, user_id: int, ws) -> None:
        """Drop one WS connection (close, error, or replaced). Never raises."""
        sockets = self._ws.get(user_id)
        if not sockets:
            return
        sockets.discard(ws)
        if not sockets:
            self._ws.pop(user_id, None)

    def ws_connected(self, user_id: int) -> bool:
        """True when at least one WS is attached (liveness still goes
        through `online()` heartbeats)."""
        return bool(self._ws.get(user_id))

    def ws_count(self, user_id: int) -> int:
        return len(self._ws.get(user_id) or ())

    # --- machine inventory (H6c) --------------------------------------

    def update_machine(self, user_id: int, machine_id: str,
                       info: dict | None = None) -> None:
        """Record a hello from one machine. Heartbeats (a hello proves
        liveness). Unknown/empty ids are ignored — tracking is display
        only and must never break dispatch. Oldest-first eviction past
        MAX_MACHINES_PER_USER."""
        mid = str(machine_id or "").strip()[:64]
        if not mid:
            return
        self.heartbeat(user_id)
        table = self._machines.setdefault(user_id, {})
        table[mid] = {
            "machine_id": mid,
            "machine_name": str((info or {}).get("machine_name", ""))[:128],
            "platform": str((info or {}).get("platform", ""))[:128],
            "capabilities": dict((info or {}).get("capabilities") or {}),
            "last_seen": self._clock(),
        }
        while len(table) > MAX_MACHINES_PER_USER:
            oldest = min(table, key=lambda k: table[k]["last_seen"])
            del table[oldest]

    def machines_for(self, user_id: int) -> list[dict]:
        """This user's machines, newest-first, each with an `online`
        flag (seen within AGENT_ONLINE_TTL_SECONDS). Structural
        isolation falls out of the keying: a user_id only ever reads
        their own table."""
        now = self._clock()
        rows = sorted(
            self._machines.get(user_id, {}).values(),
            key=lambda m: m["last_seen"], reverse=True,
        )
        return [
            {**m, "online": (now - m["last_seen"]) <= AGENT_ONLINE_TTL_SECONDS}
            for m in rows
        ]

    async def push_ws(self, user_id: int, message: dict) -> bool:
        """Send one message to exactly one of this user's WS connections
        (first attached). True when sent; False when nobody attached or
        every send failed (dead sockets are detached).

        Exactly-once delivery to one machine: broadcast would run a
        confirmed command on N machines. First result wins via
        submit_result anyway, but never executing twice is stronger.
        """
        sockets = self._ws.get(user_id)
        if not sockets:
            return False
        for ws in list(sockets):
            try:
                await ws.send_json(message)
            except Exception:
                self.detach_ws(user_id, ws)
                continue
            self.heartbeat(user_id)
            return True
        return False

    # --- long-poll ------------------------------------------------------

    async def poll(self, user_id: int,
                   hold: float = AGENT_POLL_HOLD_SECONDS) -> dict | None:
        """Fetch the next job for this user's agent, or None after
        ``hold`` seconds of quiet. Marks the heartbeat either way.

        Raises ``PollCapacityExceeded`` when this user already holds
        MAX_POLLS_PER_USER open polls (LOW-5 cap; released in ``finally``
        so an aborted connection never leaks a slot).
        """
        inflight = self._pollers.get(user_id, 0)
        if inflight >= MAX_POLLS_PER_USER:
            raise PollCapacityExceeded(
                f"user {user_id} already holds {inflight} polls "
                f"(cap {MAX_POLLS_PER_USER})")
        self._pollers[user_id] = inflight + 1
        try:
            return await self._poll_hold(user_id, hold)
        finally:
            remaining = self._pollers.get(user_id, 1) - 1
            if remaining > 0:
                self._pollers[user_id] = remaining
            else:
                self._pollers.pop(user_id, None)

    async def _poll_hold(self, user_id: int, hold: float) -> dict | None:
        self.heartbeat(user_id)
        queue = self._queues.setdefault(user_id, deque())
        while True:
            if queue:
                return queue.popleft()
            event = self._events.get(user_id)
            if event is None:
                event = asyncio.Event()
                self._events[user_id] = event
            try:
                await asyncio.wait_for(event.wait(), timeout=hold)
            except asyncio.TimeoutError:
                # A wake that arrived between the queue check and the
                # timeout deserves one more non-blocking look - the
                # race window is tiny but the retry costs nothing.
                if queue:
                    continue
                return None
            event.clear()
            hold = 0.05  # woken once: answer fast, don't re-block long

    # --- dispatch + result correlation ----------------------------------

    async def dispatch(self, user_id: int, job_type: str, args: dict,
                       timeout: float) -> dict:
        """Stage a job for this user's agent and await its result.

        Returns the agent's result dict, or ``{"status":
        "agent_timeout", ...}`` if nothing came back within ``timeout``
        (the action's own timeout plus grace, supplied by the caller).

        WS-first with long-poll fallback (H1): when a WebSocket is
        attached, the job is pushed to exactly one socket and removed from
        the poll queue so it never executes twice. Otherwise it waits in
        the queue for the next poll, exactly as before.

        The queued job is removed on EVERY exit path - timeout,
        cancellation, or success - so a late poll never executes stale
        work nobody is waiting on. Cancellation matters as much as
        timeout here: a vanished client or a shutting-down server
        cancels the awaiting task, and leaving the job queued would run
        a confirmed command on the user's machine with no one to receive
        the result.
        """
        job_id = secrets.token_urlsafe(16)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._jobs[job_id] = {
            "owner": user_id,
            "type": job_type,
            "args": args,
            "deadline": self._clock() + timeout,
        }
        self._futures.setdefault(user_id, {})[job_id] = future
        job = {
            "job_id": job_id,
            "type": job_type,
            "args": args,
        }
        self._queues.setdefault(user_id, deque()).append(job)
        event = self._events.get(user_id)
        if event is not None:
            event.set()
        # H1: WS-first. Push to one socket; on success drop the queued
        # copy so a concurrent poll can't pick up the same confirmed work.
        # On failure (nobody attached / send error) the queue copy stays
        # and the poll path behaves exactly as before.
        if await self.push_ws(user_id, {"type": "job", "job": job}):
            self._drop_job(user_id, job_id)

        try:
            return await asyncio.wait_for(asyncio.shield(future),
                                          timeout=timeout)
        except asyncio.TimeoutError:
            return {
                "status": "agent_timeout",
                "message": (
                    "The agent did not return a result in time. The "
                    "command may still have been executed; treat the "
                    "outcome as unknown and re-check state before "
                    "retrying."
                ),
            }
        finally:
            # Unconditional, and that is the point: a CANCELLED dispatch
            # (client disconnect, shutdown) raises CancelledError, not
            # TimeoutError, so cleanup that lived only on the timeout
            # branch left the job in the queue - a later poll then
            # executed confirmed work nobody was waiting on, exactly what
            # this class documents as never happening. Draining the queue
            # here covers timeout, cancellation and success alike.
            self._drop_job(user_id, job_id)
            self._futures.get(user_id, {}).pop(job_id, None)
            self._jobs.pop(job_id, None)

    def submit_result(self, user_id: int, job_id: str,
                      result: dict) -> bool:
        """Resolve a dispatched job. True when accepted.

        False - with no detail about *why* - for an unknown, timed-out,
        already-resolved, or differently-owned job_id: the answer is
        indistinguishable on purpose, same convention as
        PendingActionStore.take for mismatched subjects. A replayed or
        forged result can never resolve a future twice, and a
        cross-user submission is dropped as a plain non-match.
        """
        job = self._jobs.get(job_id)
        if job is None or job["owner"] != user_id:
            return False
        if self._clock() > job["deadline"]:
            # The dispatcher already gave up (or is about to); a result
            # arriving past the deadline resolves nothing.
            return False
        future = self._futures.get(user_id, {}).get(job_id)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True

    def _drop_job(self, user_id: int, job_id: str) -> None:
        """Remove a timed-out job from the user's queue so a late poll
        never picks up work nobody is waiting on."""
        queue = self._queues.get(user_id)
        if queue is None:
            return
        self._queues[user_id] = deque(
            job for job in queue if job["job_id"] != job_id
        )
