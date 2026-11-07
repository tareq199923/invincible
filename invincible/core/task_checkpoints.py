# invincible/core/task_checkpoints.py
"""Durable per-tool-call checkpoints for background tasks (Step 3).

One row per SUCCESSFULLY completed tool call, so an interrupted task keeps
a record of the progress it made. This is progress TRACKING, not resume:
nothing here replays a tool call, and a checkpoint alone is not enough to
restart a task safely (the conversation, tool results, and loop state are
not stored).

Metadata only - tool name, call id, a SHA-256 of the arguments, and the
size of the result. Never arguments, paths, commands, or result content.

Ownership: ``record`` refuses a session the user does not own, and every
read filters by ``user_id``, so a foreign or unknown workflow id reads
exactly like an empty one.

Layering: pure ``core/`` logic over the injected async engine. No FastAPI.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from invincible.core.db import background_task_checkpoints as checkpoints
from invincible.core.db import sessions

logger = logging.getLogger("invincible.task_checkpoints")

_LOCK_KEY = "bg_task_checkpoint"
_MAX_LIST = 500
_NAME_CHARS = 200


class CheckpointOwnershipError(Exception):
    """The session does not belong to the user the checkpoint is for."""


def _digest(value: Any) -> tuple[str, int]:
    """Return (sha256 hex, byte length) of a JSON-ish value.

    Never raises on hostile shapes: extremely deep nesting makes both
    ``json.dumps`` and ``repr`` hit the recursion limit, so that case
    falls back to a fixed placeholder keyed by type name. The hash is a
    progress fingerprint, not an integrity guarantee, so a shared
    placeholder for undigestable values is acceptable.
    """
    try:
        blob = json.dumps(
            value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        try:
            blob = repr(value)
        except RecursionError:
            blob = f"<undigestable {type(value).__name__}>"
    raw = blob.encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest(), len(raw)


class TaskCheckpointStore:
    def __init__(self, engine) -> None:
        self.engine = engine

    async def record(
        self,
        *,
        user_id: int,
        session_pk: int,
        workflow_id: str,
        call_id: str,
        tool_name: str,
        args: Any,
        result: Any,
    ) -> dict:
        """Persist one checkpoint; idempotent on ``call_id``.

        Returns the row as a dict plus ``created`` (False when this
        ``call_id`` was already recorded - the existing row is returned
        unchanged) and ``conflict`` (True only when the repeated
        ``call_id`` belongs to a DIFFERENT tool or arguments, i.e. an id
        collision whose call was not recorded; also logged as a
        warning). Raises :class:`CheckpointOwnershipError` when the
        session is not the user's, and lets database errors propagate so
        the CALLER decides how a persistence failure affects the task.
        """
        args_hash, _ = _digest(args)
        _, result_bytes = _digest(result)
        async with self.engine.begin() as conn:
            owned = (await conn.execute(
                select(sessions.c.id).where(
                    sessions.c.id == session_pk,
                    sessions.c.user_id == user_id,
                )
            )).first()
            if owned is None:
                raise CheckpointOwnershipError(
                    "session is not owned by this user")
            # Serialise writers per session so two inserts can never both
            # read the same head and race for the same seq.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:p, hashtext(:k))"),
                {"p": session_pk, "k": _LOCK_KEY},
            )
            head = (await conn.execute(
                select(func.max(checkpoints.c.seq)).where(
                    checkpoints.c.session_pk == session_pk)
            )).scalar_one() or 0
            inserted = (await conn.execute(
                pg_insert(checkpoints)
                .values(
                    user_id=user_id,
                    session_pk=session_pk,
                    workflow_id=workflow_id,
                    seq=head + 1,
                    call_id=call_id,
                    tool_name=str(tool_name)[:_NAME_CHARS],
                    args_hash=args_hash,
                    result_bytes=result_bytes,
                    created_at=time.time(),
                )
                .on_conflict_do_nothing(
                    constraint="uq_bg_checkpoints_session_call")
                .returning(*checkpoints.c)
            )).mappings().first()
            if inserted is not None:
                return {**dict(inserted), "created": True, "conflict": False}
            existing = dict((await conn.execute(
                select(checkpoints).where(
                    checkpoints.c.session_pk == session_pk,
                    checkpoints.c.call_id == call_id,
                )
            )).mappings().one())
            # Same call_id AND same tool + arguments is a genuine retry
            # (idempotent, silent). Anything else is a different call
            # that reused an id: it is NOT recorded, so say so loudly.
            # Metadata only in the log - never arguments or results.
            conflict = (
                existing["tool_name"] != str(tool_name)[:_NAME_CHARS]
                or existing["args_hash"] != args_hash
            )
            if conflict:
                logger.warning(
                    "checkpoint call_id collision in session %s: "
                    "call_id %r already recorded for tool %r (seq %s); "
                    "new call to tool %r was not recorded",
                    session_pk, call_id, existing["tool_name"],
                    existing["seq"], str(tool_name)[:_NAME_CHARS])
            return {**existing, "created": False, "conflict": conflict}

    async def latest(
        self, user_id: int, workflow_id: str
    ) -> dict | None:
        """Highest-``seq`` checkpoint of one owned workflow, else None."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(checkpoints)
                .where(
                    checkpoints.c.user_id == user_id,
                    checkpoints.c.workflow_id == workflow_id,
                )
                .order_by(checkpoints.c.seq.desc())
                .limit(1)
            )).mappings().first()
        return dict(row) if row is not None else None

    async def history(
        self, user_id: int, workflow_id: str, limit: int = _MAX_LIST
    ) -> list[dict]:
        """Checkpoints of one owned workflow, oldest first."""
        capped = max(1, min(int(limit), _MAX_LIST))
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(checkpoints)
                .where(
                    checkpoints.c.user_id == user_id,
                    checkpoints.c.workflow_id == workflow_id,
                )
                .order_by(checkpoints.c.seq.asc())
                .limit(capped)
            )).mappings().all()
        return [dict(r) for r in rows]
