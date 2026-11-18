# invincible/core/harness_approvals.py
"""Durable human-in-the-loop approvals (H5).

Two approval paths, deliberately separate:

- FAST path (unchanged): ``tool_executor.confirm_action`` — 10-minute
  TTL, single-use token, in-memory by default. The AI asks, the holder
  answers within minutes, the holding ``/mcp`` request resolves.
- SLOW path (this module): ``ApprovalStore.suspend`` parks a confirmed
  action as a ``pending_actions`` row carrying ``suspended_workflow_id``
  + ``deadline`` (default 24h, configurable per call). The workflow
  suspends — the process may restart, the human may answer tomorrow —
  and ``resolve`` later resumes it. Unknown, expired, already-resolved,
  wrong-owner, AND fast-path tokens all answer identically (None): a
  replayed or forged result can never execute twice, mirroring
  ``PendingActionStore.take`` and ``AgentRegistry.submit_result``.

Separation is structural: ``suspend`` is the ONLY writer of
slow-path rows, ``resolve`` refuses rows with NULL
``suspended_workflow_id`` (fast-path tokens), and the fast path's
``load_persisted`` skips slow-path rows. Neither path can resolve the
other's tokens.

Secrets discipline: resolution returns the staged record (the executor
needs the command/args); audit/callers must log metadata only, never
raw commands/paths.
"""
from __future__ import annotations

import logging
import secrets
import time

from invincible.core.db import pending_actions
from invincible.core.tool_executor import _OWNER_SUBJECT_KEY

logger = logging.getLogger("invincible.harness_approvals")

# Default slow-path wait: a human approval is an unbounded wait (their
# APPROVAL_TIMEOUT_S is a day); 24h keeps rows from lingering forever
# while surviving overnight + timezone gaps.
DEFAULT_SUSPEND_TTL_SECONDS = 86400.0


def _owned_by(mapping, owner_subject: int | None) -> bool:
    """Whether a slow-path row was staged by ``owner_subject``.

    Fail closed: no subject never matches, and subject-less rows
    (``_owner_subject`` NULL) never match a real subject, mirroring
    ``resolve`` / ``PendingActionStore.take``.
    """
    if owner_subject is None:
        return False
    args = mapping["args"]
    if not isinstance(args, dict):
        return False
    return args.get(_OWNER_SUBJECT_KEY) == owner_subject


class ApprovalStore:
    """Durable suspend/resume over the shared ``pending_actions`` table.

    Thin repository (AGENTS.md layering): SQLAlchemy async Core only, no
    business logic beyond subject/deadline/path checks. ``engine`` is the
    shared async engine (lifespan-owned).
    """

    def __init__(self, engine) -> None:
        self._engine = engine

    async def suspend(
        self,
        *,
        workflow_id: str,
        action_type: str,
        args: dict,
        owner_subject: int | None = None,
        ttl_seconds: float = DEFAULT_SUSPEND_TTL_SECONDS,
        now: float | None = None,
    ) -> str:
        """Park an action awaiting a human. Returns the slow-path token
        (unpredictable, single-use, distinct from fast-path tokens only
        by its row shape — both are ``secrets.token_urlsafe(16)``)."""
        token = secrets.token_urlsafe(16)
        created = now if now is not None else time.time()
        async with self._engine.begin() as conn:
            await conn.execute(
                pending_actions.insert().values(
                    token=token,
                    type=action_type,
                    # JSONB column: native dict bind; the staging subject
                    # rides inside under the reserved key (same convention
                    # as PendingActionStore.put), stripped on resolve.
                    args={**args, _OWNER_SUBJECT_KEY: owner_subject},
                    created_at=created,
                    suspended_workflow_id=workflow_id,
                    deadline=created + ttl_seconds,
                )
            )
        return token

    async def resolve(
        self,
        token: str,
        *,
        approve: bool,
        requester_subject: int | None = None,
        now: float | None = None,
    ) -> dict | None:
        """Resolve a suspended action. Returns the staged record
        ``{"type", "args", "workflow_id", "created_at"}`` on approve, or
        ``{"status": "declined", "workflow_id": ...}`` on deny. None for
        unknown / expired / already-used / wrong-subject / fast-path
        tokens — indistinguishable on purpose. Single-use: the row is
        deleted on every definitive outcome (approve, deny, expired)."""
        ts = now if now is not None else time.time()
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    pending_actions.select().where(
                        pending_actions.c.token == token
                    )
                )
            ).first()
            if row is None:
                return None
            record = row._mapping
            workflow_id = record["suspended_workflow_id"]
            if workflow_id is None:
                # Fast-path token: not ours to resolve.
                return None
            deadline = record["deadline"]
            if deadline is not None and ts > deadline:
                await conn.execute(
                    pending_actions.delete().where(
                        pending_actions.c.token == token
                    )
                )
                return None
            args = dict(record["args"])
            owner = args.pop(_OWNER_SUBJECT_KEY, None)
            if owner is None and requester_subject is not None:
                # Fail closed (same rule as PendingActionStore.take):
                # subject-less rows are invisible to subjects.
                return None
            if owner is not None and requester_subject != owner:
                return None
            # Atomic claim: the DELETE is the single-use gate. Two
            # concurrent resolves can both pass the SELECT above, but
            # only one DELETE ... RETURNING removes the row; the loser
            # (zero rows) answers exactly like an unknown token, so an
            # approval can never be claimed - or executed - twice.
            claimed = (
                await conn.execute(
                    pending_actions.delete()
                    .where(
                        pending_actions.c.token == token,
                        pending_actions.c.suspended_workflow_id.is_not(None),
                    )
                    .returning(pending_actions.c.token)
                )
            ).first()
            if claimed is None:
                return None
            if not approve:
                return {"status": "declined", "workflow_id": workflow_id}
            return {
                "type": record["type"],
                "args": args,
                "workflow_id": workflow_id,
                "created_at": record["created_at"],
            }

    async def pending_for_workflow(
        self, workflow_id: str, *, owner_subject: int | None
    ) -> list[dict]:
        """Slow-path rows still awaiting a human for one workflow
        (metadata only — tokens included so the dashboard can render
        them; never args contents)."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    pending_actions.select().where(
                        pending_actions.c.suspended_workflow_id
                        == workflow_id
                    )
                )
            ).all()
        return [
            {
                "token": r._mapping["token"],
                "type": r._mapping["type"],
                "workflow_id": workflow_id,
                "created_at": r._mapping["created_at"],
                "deadline": r._mapping["deadline"],
            }
            for r in rows
            if _owned_by(r._mapping, owner_subject)
        ]

    async def sweep_expired(self, now: float | None = None) -> int:
        """Delete past-deadline slow-path rows. Returns the count."""
        ts = now if now is not None else time.time()
        async with self._engine.begin() as conn:
            result = await conn.execute(
                pending_actions.delete().where(
                    pending_actions.c.suspended_workflow_id.is_not(None),
                    pending_actions.c.deadline <= ts,
                )
            )
        return int(result.rowcount or 0)

    async def purge_workflow(
        self, workflow_id: str, *, owner_subject: int | None
    ) -> int:
        """Delete ONE owner's pending slow-path rows for ONE workflow
        (task cancelled / finished): a late approval then reads as an
        unknown token. Never touches another subject's rows, other
        workflows, or fast-path rows. Returns the count deleted."""
        if owner_subject is None:
            return 0
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    pending_actions.select().where(
                        pending_actions.c.suspended_workflow_id
                        == workflow_id
                    )
                )
            ).all()
            tokens = [
                r._mapping["token"] for r in rows
                if _owned_by(r._mapping, owner_subject)
            ]
            if not tokens:
                return 0
            result = await conn.execute(
                pending_actions.delete().where(
                    pending_actions.c.token.in_(tokens),
                    pending_actions.c.suspended_workflow_id
                    == workflow_id,
                )
            )
        return int(result.rowcount or 0)

    async def purge_all_suspended(self) -> int:
        """Startup cleanup: delete EVERY slow-path row. Background
        tasks are not resumed after a restart, so no suspended row can
        have a live workflow waiting on it; leaving them would let a
        stale approval be claimed with no task attached. Fast-path
        rows (NULL workflow) are untouched. Returns the count."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                pending_actions.delete().where(
                    pending_actions.c.suspended_workflow_id.is_not(None)
                )
            )
        return int(result.rowcount or 0)
