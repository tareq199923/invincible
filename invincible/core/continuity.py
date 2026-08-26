# invincible/core/continuity.py
"""Canonical task-state continuity on PostgreSQL (Phase 16 port of 15b).

Same model as Phase 15b: versioned ``task_states`` per
``(session_id, task_key)`` with optimistic CAS, immutable ``checkpoints``
pinning a state version, and the rendered continuation brief.

Concurrency note: the SQLite-era asyncio lock is GONE. PostgreSQL gives us
the guarantee natively - the UNIQUE(session, task_key, version) constraint
makes a racing duplicate insert fail, which this engine maps to
:class:`ContinuityConflictError`. "Latest trusted state" is still simply
``max(version)``.
"""
import json
import logging
import time

from sqlalchemy import and_, func, select

from invincible.core.db import checkpoints, sessions, task_states

logger = logging.getLogger("invincible.continuity")

MAX_PAYLOAD_CHARS = 4096
MAX_RENDER_CHARS_PER_TASK = 1200
_BRIEF_TOTAL_CHAR_CAP = 4096  # m5: whole-brief budget, not just per-task
_MAX_TASK_KEYS_RENDERED = 5

_VALID_STATUSES = ("active", "blocked", "done", "cancelled")

_SCHEMA_NOTE = """Schema owned by core.db metadata (task_states,
checkpoints tables)."""


class ContinuityConflictError(Exception):
    """CAS rejection or concurrent-update race (UNIQUE violation)."""


class ContinuityEngine:
    def __init__(self, engine, runs=None):
        self.engine = engine
        self._runs = runs

    async def init(self) -> None:
        """Schema owned by core.db metadata."""

    async def close(self) -> None:
        """Engine owned/disposed by the lifespan."""

    # ------------------------------------------------------------------
    # State writes (versioned upsert; native UNIQUE = race safety)

    async def set_state(
        self,
        session_id: str,
        payload: dict,
        *,
        actor: str,
        task_key: str = "default",
        status: str = "active",
        expected_version: int | None = None,
        request_id: str | None = None,
        session_pk: int | None = None,
    ) -> dict:
        """Versioned upsert. Returns the new head
        ``{session_id,task_key,status,payload,version}``.

        ``session_pk`` (Phase 2): the owning surrogate session resolved by
        the caller under the acting principal - when given, the version
        chain, advisory lock, and uniqueness all scope to it, so two
        principals sharing a client string never interact. None keeps the
        pre-isolation string-keyed path (tests / local-only callers).

        Raises ValueError for oversized/non-dict payloads/bad statuses.
        Raises :class:`ContinuityConflictError` when ``expected_version``
        no longer matches, or when a concurrent writer wins the insert race.
        """
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object (dict)")
        # Size guard only - the column is JSONB, so the dict itself is bound
        # (SQLAlchemy serializes once; never pre-dump into a JSONB column).
        blob = json.dumps(payload, ensure_ascii=False)
        if len(blob) > MAX_PAYLOAD_CHARS:
            raise ValueError(
                f"payload exceeds {MAX_PAYLOAD_CHARS} chars "
                f"(got {len(blob)}); summarize before storing"
            )
        if status not in _VALID_STATUSES:
            raise ValueError(
                f"status must be one of: {', '.join(_VALID_STATUSES)}"
            )

        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        pk_filter = (
            task_states.c.session_pk == session_pk
            if session_pk is not None
            else task_states.c.session_id == session_id
        )

        async with self.engine.begin() as conn:
            # Serialize writers per (session, task_key): without this, two
            # concurrent no-expected_version writes can both read the same
            # head and race for version N+1. The transaction-scoped advisory
            # lock replaces the SQLite era's process-wide write lock; CAS
            # callers with expected_version still conflict deterministically.
            if session_pk is not None:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(:p, hashtext(:k))"),
                    {"p": session_pk, "k": task_key},
                )
            else:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock("
                         "hashtext(:s), hashtext(:k))"),
                    {"s": session_id, "k": task_key},
                )
            head = (await conn.execute(
                select(func.max(task_states.c.version))
                .where(pk_filter,
                       task_states.c.task_key == task_key)
            )).scalar_one()
            head = head or 0
            if expected_version is not None and expected_version != head:
                raise ContinuityConflictError(
                    f"expected version {expected_version} but current head "
                    f"is {head} for task '{task_key}'"
                )
            new_version = head + 1
            now = time.time()
            try:
                await conn.execute(
                    task_states.insert().values(
                        session_id=session_id,
                        session_pk=session_pk,
                        task_key=task_key,
                        status=status,
                        payload=payload,
                        version=new_version,
                        updated_by=actor,
                        request_id=request_id,
                        updated_at=now,
                    )
                )
            except IntegrityError as exc:
                raise ContinuityConflictError(
                    f"concurrent update on task '{task_key}' "
                    f"(lost race for v{new_version})"
                ) from exc
        return {
            "session_id": session_id,
            "task_key": task_key,
            "status": status,
            "payload": payload,
            "version": new_version,
        }

    # ------------------------------------------------------------------
    # State reads

    async def get_state(
        self, session_id: str, task_key: str = "default",
        *, session_pk: int | None = None,
    ) -> dict | None:
        return next(iter(await self.history(
            session_id, task_key, limit=1, session_pk=session_pk)), None)

    async def history(
        self, session_id: str, task_key: str = "default", limit: int = 20,
        *, session_pk: int | None = None,
    ) -> list[dict]:
        """Newest-first version history for one task key."""
        return await self._history_rows(
            session_id, task_key, limit, session_pk=session_pk)

    async def _history_rows(self, session_id, task_key, limit,
                            session_pk=None):
        scope = (
            task_states.c.session_pk == session_pk
            if session_pk is not None
            else task_states.c.session_id == session_id
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(
                    task_states.c.status,
                    task_states.c.payload,
                    task_states.c.version,
                    task_states.c.updated_by,
                    task_states.c.updated_at,
                )
                .where(scope,
                       task_states.c.task_key == task_key)
                .order_by(task_states.c.version.desc())
                .limit(limit)
            )).all()
        out = []
        for status, payload_blob, version, updated_by, updated_at in rows:
            out.append({
                "session_id": session_id,
                "task_key": task_key,
                "status": status,
                "payload": payload_blob,   # JSONB -> dict already
                "version": version,
                "updated_by": updated_by,
                "updated_at": updated_at,
            })
        return out

    async def active_task_keys(
        self, session_id: str, limit: int = 5,
        *, session_pk: int | None = None,
    ) -> list[str]:
        scope = (
            task_states.c.session_pk == session_pk
            if session_pk is not None
            else task_states.c.session_id == session_id
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(task_states.c.task_key)
                .where(scope)
                .group_by(task_states.c.task_key)
                .order_by(func.max(task_states.c.updated_at).desc())
                .limit(limit)
            )).scalars().all()
        return list(rows)

    async def list_for_user(
        self, user_id: int, *, status: str = "active", limit: int = 100,
    ) -> list[dict]:
        """Task heads across ALL of one user's sessions, newest activity
        first (dashboard cross-session task list).

        Ownership flows entirely through the ``sessions`` join - task
        rows carry no user column, so the surrogate-session join IS the
        isolation predicate. Pre-isolation rows (``session_pk`` NULL) can
        never match and stay inert history.
        """
        head = (
            select(
                task_states.c.session_pk,
                task_states.c.task_key,
                func.max(task_states.c.version).label("head_version"),
            )
            .where(task_states.c.session_pk.isnot(None))
            .group_by(task_states.c.session_pk, task_states.c.task_key)
            .subquery()
        )
        query = (
            select(
                task_states.c.session_pk,
                task_states.c.task_key,
                task_states.c.status,
                task_states.c.payload,
                task_states.c.version,
                task_states.c.updated_by,
                task_states.c.updated_at,
                sessions.c.client_session_id,
                sessions.c.project_id,
            )
            .join(head, and_(
                head.c.session_pk == task_states.c.session_pk,
                head.c.task_key == task_states.c.task_key,
                head.c.head_version == task_states.c.version,
            ))
            .join(sessions, sessions.c.id == task_states.c.session_pk)
            .where(sessions.c.user_id == user_id,
                   task_states.c.status == status)
            .order_by(task_states.c.updated_at.desc())
            .limit(limit)
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Checkpoints

    async def create_checkpoint(
        self,
        session_id: str,
        task_key: str = "default",
        note: str = "",
        actor: str = "user",
        *,
        session_pk: int | None = None,
    ) -> dict:
        """Pin the CURRENT head version (0 = nothing tracked yet)."""
        scope = (
            task_states.c.session_pk == session_pk
            if session_pk is not None
            else task_states.c.session_id == session_id
        )
        async with self.engine.begin() as conn:
            head = (await conn.execute(
                select(func.max(task_states.c.version))
                .where(scope,
                       task_states.c.task_key == task_key)
            )).scalar_one()
            version = head or 0
            now = time.time()
            result = await conn.execute(
                checkpoints.insert().values(
                    session_id=session_id,
                    session_pk=session_pk,
                    task_key=task_key,
                    state_version=version,
                    note=(note or "")[:500],
                    created_at=now,
                )
            )
        return {
            "id": result.inserted_primary_key[0],
            "session_id": session_id,
            "task_key": task_key,
            "state_version": version,
            "note": (note or "")[:500],
            "created_at": now,
        }

    async def checkpoints(
        self, session_id: str, task_key: str | None = None, limit: int = 20,
        *, session_pk: int | None = None,
    ) -> list[dict]:
        scope = (
            checkpoints.c.session_pk == session_pk
            if session_pk is not None
            else checkpoints.c.session_id == session_id
        )
        query = (
            checkpoints.select()
            .where(scope)
            .order_by(checkpoints.c.id.desc())
            .limit(limit)
        )
        if task_key is not None:
            query = query.where(checkpoints.c.task_key == task_key)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Reactive failover checkpointing (Platform Phase 4)

    async def reactive_checkpoint(
        self, session_id: str, *, session_pk: int | None = None,
        note: str = "",
    ) -> list[dict]:
        """Snapshot every tracked task's current head BEFORE work moves to
        another provider. Deliberately silent when the session tracks no
        task state - a checkpoint row per failed request would be noise,
        and there would be nothing meaningful to pin anyway.
        """
        task_keys = await self.active_task_keys(
            session_id, limit=_MAX_TASK_KEYS_RENDERED, session_pk=session_pk
        )
        created = []
        for task_key in task_keys:
            created.append(await self.create_checkpoint(
                session_id, task_key, note=note, actor="system-failover",
                session_pk=session_pk,
            ))
        return created

    def failover_hook(self):
        """Build the callable wired into ``Router.failover_hook`` by the
        lifespan - the Router stays continuity-agnostic (layering rule)."""

        async def _hook(*, request_id: str, session_id: str,
                        session_pk: int | None, failed_provider: str | None,
                        error_class: str | None) -> None:
            await self.reactive_checkpoint(
                session_id,
                session_pk=session_pk,
                note=(
                    "auto: pre-failover snapshot "
                    f"({failed_provider or '?'} failed: "
                    f"{error_class or '?'})"
                ),
            )

        return _hook

    # ------------------------------------------------------------------
    # Continuation brief

    async def interruption_note(self, session_id: str,
                                *, session_pk: int | None = None) -> str | None:
        """Public projection hook: describe the post-checkpoint upstream
        failure for this session, if one exists."""
        if self._runs is None:
            return None
        cps = await self.checkpoints(session_id, limit=1,
                                     session_pk=session_pk)
        cp_after = cps[0]["created_at"] if cps else 0.0
        recent = await self._runs.recent(
            session_id=session_id, limit=10, session_pk=session_pk)
        for run in recent:  # newest-first
            if float(run.get("finished_at") or 0) <= cp_after:
                continue
            if run.get("outcome") == "ok":
                return None
            provider = run.get("provider_name", "?")
            err = run.get("error_class") or run.get("outcome")
            return (
                f"The previous attempt ended unexpectedly on provider "
                f"'{provider}' ({err}). Continue from the trusted state "
                f"below instead of restarting."
            )
        return None

    @staticmethod
    def _render_payload(payload: dict) -> str:
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(rendered) > MAX_RENDER_CHARS_PER_TASK:
            rendered = rendered[:MAX_RENDER_CHARS_PER_TASK] + "…[truncated]"
        return rendered

    async def context_message(self, session_id: str, *,
                              session_pk: int | None = None) -> dict | None:
        """The injectable system message carrying the continuation brief,
        or None when the session tracks no tasks."""
        from invincible.core.settings import settings

        if not settings.continuity_enabled():
            return None
        task_keys = await self.active_task_keys(
            session_id, limit=_MAX_TASK_KEYS_RENDERED, session_pk=session_pk
        )
        if not task_keys:
            return None

        interruption = await self.interruption_note(session_id,
                                                    session_pk=session_pk)
        lines = [
            "[Session continuity — canonical task state maintained by "
            "Invincible. Trust this over reconstructed transcript details.]"
        ]
        if interruption:
            lines.append(interruption)

        used = sum(len(line) + 1 for line in lines)
        omitted = False
        for idx, task_key in enumerate(task_keys):
            state = await self.get_state(session_id, task_key,
                                         session_pk=session_pk)
            if state is None:
                continue
            chunk_lines = [
                f"Task '{task_key}' (status: {state['status']}, "
                f"v{state['version']}):",
                self._render_payload(state["payload"]),
            ]
            cps = await self.checkpoints(session_id, task_key, limit=1,
                                         session_pk=session_pk)
            if cps:
                cp = cps[0]
                chunk_lines.append(
                    f"Latest checkpoint #{cp['id']} (at v{cp['state_version']}): "
                    f"{cp['note']}"
                )
            chunk = "\n".join(chunk_lines)
            if used + len(chunk) > _BRIEF_TOTAL_CHAR_CAP and idx > 0:
                omitted = True
                break
            lines.append(chunk)
            used += len(chunk) + 1
        if omitted:
            lines.append("[…additional tasks omitted to bound prompt size]")
        lines.append("[End session continuity]")
        return {"role": "system", "content": "\n".join(lines)}


async def context_system_message(
    engine_or_engine_holder, session_id: str, *, session_pk: int | None = None
) -> dict | None:
    """Toggle-aware wrapper used by endpoints."""
    engine = engine_or_engine_holder
    if engine is None:
        return None
    return await engine.context_message(session_id, session_pk=session_pk)
