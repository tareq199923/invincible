# invincible/core/agent_registry.py
"""In-memory agent liveness tracking + job dispatch to paired agents
(Phase 10).

The golden rule of the agent design: the server decides what's allowed
(denylist, staging, confirm tokens, per-user binding - all unchanged in
tool_executor), and the agent only does the work. This module is the
plumbing that moves *confirmed* work down an agent's own open long-poll
connection and carries the result back to the holding /mcp request.

No new transport: agents long-poll ``POST /agent/poll`` (https, httpx,
no WebSocket, no new packages). Each poll is a heartbeat, so "online"
means "polled within AGENT_ONLINE_TTL_SECONDS".

Isolation is structural, not a policy check: every queued job carries
its owner's user_id, queues and futures are keyed by user_id, and
submit_result refuses results for jobs staged by a different user. An
agent authenticated as user 42 is only ever handed user 42's work;
user 1's commands cannot reach user 2's PC because no code path tries.

Single-instance, in-memory by design - the same trade-off as the
default PendingActionStore: a restart orphans in-flight jobs (the
holding /mcp request then fails with a connection reset, and the
client retries end-to-end) and every agent re-registers itself on its
next poll. No persistence layer, no migration.
"""
import asyncio
import secrets
import time
from collections import deque

from invincible.core.settings import (
    AGENT_ONLINE_TTL_SECONDS,
    AGENT_POLL_HOLD_SECONDS,
)


class AgentRegistry:
    """Liveness + dispatch table for paired agents.

    Layout (all keyed by user_id unless noted):

    - ``_last_seen`` - wall-clock timestamp of the user's last poll.
      ``time.time()`` (not monotonic) purely for consistency with
      PendingActionStore; nothing here crosses a process boundary.
    - ``_queues`` - jobs waiting to be picked up by a poll.
    - ``_events`` - per-user event a held poll sleeps on; ``dispatch``
      sets it so an already-connected poll answers immediately.
    - ``_futures`` - job_id -> Future the dispatching /mcp request
      awaits; ``submit_result`` resolves it.

    Events are created lazily and *kept* (not popped after a wake) so
    concurrent dispatches and the next held poll can share one event;
    ``Event.set()`` followed by ``Event.clear()`` happens under the
    loop's single-threaded execution, so no wake is ever lost.
    """

    def __init__(self, *, clock=time.time):
        self._last_seen: dict[int, float] = {}
        self._queues: dict[int, deque] = {}
        self._events: dict[int, asyncio.Event] = {}
        self._futures: dict[int, dict[str, asyncio.Future]] = {}
        # job_id -> owner user_id, for the single-use/owner checks in
        # submit_result. Entries live until resolved or swept.
        self._jobs: dict[str, dict] = {}
        self._clock = clock

    # --- liveness -------------------------------------------------------

    def heartbeat(self, user_id: int) -> None:
        """Record that this user's agent just polled."""
        self._last_seen[user_id] = self._clock()

    def online(self, user_id: int) -> bool:
        last = self._last_seen.get(user_id)
        return (
            last is not None
            and (self._clock() - last) <= AGENT_ONLINE_TTL_SECONDS
        )

    def last_seen(self, user_id: int) -> float | None:
        return self._last_seen.get(user_id)

    # --- long-poll ------------------------------------------------------

    async def poll(self, user_id: int,
                   hold: float = AGENT_POLL_HOLD_SECONDS) -> dict | None:
        """Fetch the next job for this user's agent, or None after
        ``hold`` seconds of quiet. Marks the heartbeat either way."""
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
        On timeout the queued job is removed if still pending - a late
        poll must not execute stale work nobody is waiting on.
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
        self._queues.setdefault(user_id, deque()).append({
            "job_id": job_id,
            "type": job_type,
            "args": args,
        })
        event = self._events.get(user_id)
        if event is not None:
            event.set()

        try:
            return await asyncio.wait_for(asyncio.shield(future),
                                          timeout=timeout)
        except asyncio.TimeoutError:
            self._drop_job(user_id, job_id)
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
