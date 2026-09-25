# invincible/core/harness_bus.py
"""Harness event bus (H0 in-memory, H5 durable) — port of Hendrixer
`harness/bus.ts`.

Two jobs:
  1. broadcast every event live to subscribers (WS inspector, dashboard)
  2. keep a bounded in-memory history for reconnect replay, AND persist
     workflow-scoped events to Postgres (H5 ``workflow_events`` table).

Persistence rule: only events carrying a ``workflow_id`` are written —
that is the durable per-workflow timeline (crash resume, days-long
approvals). Ambient chatter without one (e.g. MCP ``tool.requested``)
stays in-memory only. Writes are fire-and-forget on the running loop
(the ``PendingActionStore._persist`` pattern): storage trouble never
breaks the harness flow, memory stays the source of truth.

Best-effort throughout: `emit()` never raises.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Callable
from typing import Any

from invincible.core.harness_events import HarnessEvent, HarnessEventType, new_event

logger = logging.getLogger("invincible.harness_bus")

# Bounded replay buffer — enough for an inspector reconnect, small enough
# that a hot tool loop can't grow memory unbounded.
HISTORY_LIMIT = 500


class HarnessBus:
    """Fan-out bus: `emit` stamps + stores + broadcasts (sync subscribers).

    Subscribers are plain callables (sync). Async WS fan-out lives in the
    endpoint layer (H1), which polls `history(since_ts)` — this module never
    touches websockets, keeping it hermetic and dependency-free.
    """

    def __init__(self, history_limit: int = HISTORY_LIMIT) -> None:
        self._listeners: set[Callable[[HarnessEvent], None]] = set()
        self._history: deque[HarnessEvent] = deque(maxlen=max(1, history_limit))
        self._engine = None
        self._background_tasks: set = set()

    def attach_engine(self, engine) -> None:
        """Opt-in persistence target (shared PG engine, lifespan-owned)."""
        self._engine = engine

    async def flush_persisted(self) -> None:
        """Wait for outstanding fire-and-forget persistence writes.

        Used at shutdown (and by tests) so a clean exit never drops the
        last workflow events."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    def _persist(self, event: HarnessEvent) -> None:
        """Schedule a best-effort workflow_events insert (H5). Only
        workflow-scoped events persist; anything without a workflow_id
        is memory-only by design. No running loop (sync contexts, plain
        unit tests) means no persistence — memory stays the truth."""
        if self._engine is None or not event.get("workflow_id"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _run() -> None:
            try:
                from invincible.core.db import workflow_events

                async with self._engine.begin() as conn:
                    # JSONB column: native dict bind (never pre-dumped).
                    await conn.execute(
                        workflow_events.insert().values(
                            workflow_id=event["workflow_id"],
                            type=event["type"],
                            payload=dict(event),
                            created_at=float(event.get("ts", 0) or 0),
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "HarnessBus persistence failed (%s); continuing "
                    "in memory only", exc,
                )
            finally:
                self._background_tasks.discard(task)

        task = loop.create_task(_run())
        self._background_tasks.add(task)

    def subscribe(self, listener: Callable[[HarnessEvent], None]) -> Callable[[], None]:
        """Register a listener; returns an `unsubscribe()` callable."""
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    def emit(
        self, type: HarnessEventType | str, **fields: Any
    ) -> HarnessEvent:
        """Stamp, store, broadcast, and (H5) persist workflow-scoped
        events. Never raises."""
        event = new_event(type, **fields)
        self._history.append(event)
        for listener in list(self._listeners):
            with contextlib.suppress(Exception):
                listener(event)
        with contextlib.suppress(Exception):
            self._persist(event)
        return event

    def history(self, since_ts: float | None = None) -> list[HarnessEvent]:
        """Bounded replay, oldest-first. `since_ts` filters incrementally
        for WS reconnects (H1)."""
        if since_ts is None:
            return list(self._history)
        return [e for e in self._history if float(e.get("ts", 0)) > since_ts]

    def __len__(self) -> int:
        return len(self._history)
