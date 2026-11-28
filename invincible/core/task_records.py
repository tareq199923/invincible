# invincible/core/task_records.py
"""Durable metadata-only records for background tasks (Step 5).

One row per task, written BEFORE the runner starts and closed on every
terminal path. The row is the source of truth for task existence and
durable status; ``workflow_events`` remains the detail timeline. Never
stores task text, commands, paths, tool args, answers, or free-form error
text - the text lives in the chat session whose id IS the workflow id.

Layering: a thin repository over SQLAlchemy async Core. No FastAPI
imports and no business logic beyond status validation.
"""
from __future__ import annotations

import time

from sqlalchemy import select, update

from invincible.core.db import background_tasks

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, CANCELLED, INTERRUPTED})
ALL_STATUSES = frozenset({RUNNING}) | TERMINAL_STATUSES

_MODES = frozenset({"plan", "manual"})


class TaskRecordStore:
    """Owner-scoped reads and runner-facing writes for task records."""

    def __init__(self, engine) -> None:
        self.engine = engine

    async def create_running(
        self,
        workflow_id: str,
        *,
        user_id: int,
        mode: str,
        session_pk: int | None = None,
        now: float | None = None,
    ) -> None:
        """Insert the one ``running`` row for a new task.

        The caller writes this BEFORE scheduling the runner and fails
        closed if the insert raises.
        """
        if mode not in _MODES:
            raise ValueError(f"unsupported background task mode: {mode}")
        ts = time.time() if now is None else float(now)
        async with self.engine.begin() as conn:
            await conn.execute(background_tasks.insert().values(
                workflow_id=workflow_id,
                user_id=user_id,
                session_pk=session_pk,
                mode=mode,
                status=RUNNING,
                created_at=ts,
                started_at=ts,
                finished_at=None,
            ))

    async def attach_session(
        self, workflow_id: str, session_pk: int, *, user_id: int
    ) -> bool:
        """Best-effort link to the session the runner just resolved.

        Owner-scoped. Returns False for an unknown/foreign workflow id;
        callers log and continue rather than failing the task.
        """
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(background_tasks)
                .where(
                    background_tasks.c.workflow_id == workflow_id,
                    background_tasks.c.user_id == user_id,
                )
                .values(session_pk=session_pk)
            )
        return bool(result.rowcount)

    async def mark_terminal(
        self,
        workflow_id: str,
        status: str,
        *,
        finished_at: float | None = None,
    ) -> bool:
        """Close a still-``running`` row exactly once.

        The ``status = 'running'`` predicate makes the write idempotent
        and prevents a late runner write from overwriting a startup
        sweep's ``interrupted``. Returns whether a row was closed.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"not a terminal task status: {status}")
        ts = time.time() if finished_at is None else float(finished_at)
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(background_tasks)
                .where(
                    background_tasks.c.workflow_id == workflow_id,
                    background_tasks.c.status == RUNNING,
                )
                .values(status=status, finished_at=ts)
            )
        return bool(result.rowcount)

    async def get(self, workflow_id: str, *, user_id: int) -> dict | None:
        """Owner-scoped row; foreign and unknown ids both return None."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(background_tasks).where(
                    background_tasks.c.workflow_id == workflow_id,
                    background_tasks.c.user_id == user_id,
                )
            )).mappings().first()
        return dict(row) if row is not None else None

    async def list_for_user(
        self, user_id: int, *, limit: int = 50
    ) -> list[dict]:
        """Newest-first rows for one owner (bounded)."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 100))
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(background_tasks)
                .where(background_tasks.c.user_id == user_id)
                .order_by(
                    background_tasks.c.created_at.desc(),
                    background_tasks.c.workflow_id.desc(),
                )
                .limit(limit)
            )).mappings().all()
        return [dict(row) for row in rows]

    async def mark_running_interrupted(
        self, *, now: float | None = None
    ) -> int:
        """Startup sweep: every stale ``running`` row becomes interrupted.

        Single-instance deployment by design, so one unconditional
        ``UPDATE ... WHERE status = 'running'`` is safe. Terminal rows are
        untouched. Returns the number of rows changed.
        """
        ts = time.time() if now is None else float(now)
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(background_tasks)
                .where(background_tasks.c.status == RUNNING)
                .values(status=INTERRUPTED, finished_at=ts)
            )
        return int(result.rowcount or 0)
