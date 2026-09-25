# invincible/core/harness_approvals.py
"""Durable human-in-the-loop approvals (H5) — port of Hendrixer
`harness/approvals.ts` (`ApprovalStore` + `suspend`/`resume`).

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
            await conn.execute(
                pending_actions.delete().where(
                    pending_actions.c.token == token
                )
            )
            if not approve:
                return {"status": "declined", "workflow_id": workflow_id}
            return {
                "type": record["type"],
                "args": args,
                "workflow_id": workflow_id,
                "created_at": record["created_at"],
            }

    async def pending_for_workflow(self, workflow_id: str) -> list[dict]:
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
