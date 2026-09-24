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

from invincible.core.db import (
    checkpoints as checkpoints_table,
)
from invincible.core.db import (
    sessions,
    task_states,
)
from invincible.core.scope import (
    UNSCOPED,
    UnresolvedScopeError,
    _Unscoped,
)

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
        session_pk: int | None | _Unscoped = UNSCOPED,
    ) -> dict:
        """Versioned upsert. Returns the new head
        ``{session_id,task_key,status,payload,version}``.

        ``session_pk`` (Phase 2): the owning surrogate session resolved by
        the caller under the acting principal - when given, the version
        chain, advisory lock, and uniqueness all scope to it, so two
        principals sharing a client string never interact. OMITTED keeps
        the pre-isolation string-keyed path (tests / local-only callers);
        ``None`` means the caller asked for ownership scoping and resolved
        no owner, which raises rather than writing unscoped
        (``core/scope.py``).

        Raises ValueError for oversized/non-dict payloads/bad statuses.
        Raises :class:`ContinuityConflictError` when ``expected_version``
        no longer matches, or when a concurrent writer wins the insert race.
        """
        if session_pk is None:
            raise UnresolvedScopeError(
                "set_state needs a resolved owning session: session_pk=None "
                "means the caller is not the owner. Omit session_pk entirely "
                "for the legacy single-tenant path."
            )
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

        # The legacy path stores a NULL surrogate (never the sentinel, which
        # is not a SQL value); ``scope_pk`` is what the column takes and what
        # the predicates below differ on.
        scope_pk = None if session_pk is UNSCOPED else session_pk
        pk_filter = (
            task_states.c.session_pk == scope_pk
            if scope_pk is not None
            else task_states.c.session_id == session_id
        )

        async with self.engine.begin() as conn:
            # Serialize writers per (session, task_key): without this, two
            # concurrent no-expected_version writes can both read the same
            # head and race for version N+1. The transaction-scoped advisory
            # lock replaces the SQLite era's process-wide write lock; CAS
            # callers with expected_version still conflict deterministically.
            if scope_pk is not None:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(:p, hashtext(:k))"),
                    {"p": scope_pk, "k": task_key},
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
                        session_pk=scope_pk,
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
        *, session_pk: int | None | _Unscoped = UNSCOPED,
    ) -> dict | None:
        return next(iter(await self.history(
            session_id, task_key, limit=1, session_pk=session_pk)), None)

    async def history(
        self, session_id: str, task_key: str = "default", limit: int = 20,
        *, session_pk: int | None | _Unscoped = UNSCOPED,
    ) -> list[dict]:
        """Newest-first version history for one task key."""
        return await self._history_rows(
            session_id, task_key, limit, session_pk=session_pk)

    async def _history_rows(self, session_id, task_key, limit,
                            session_pk=UNSCOPED):
        if session_pk is None:
            # Unresolved scope: a scoped caller with no owner sees nothing.
            return []
        scope = (
            task_states.c.session_pk == session_pk
            if session_pk is not UNSCOPED
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
        *, session_pk: int | None | _Unscoped = UNSCOPED,
    ) -> list[str]:
        if session_pk is None:
            return []
        scope = (
            task_states.c.session_pk == session_pk
            if session_pk is not UNSCOPED
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
        *,
        session_pk: int | None | _Unscoped = UNSCOPED,
    ) -> dict:
        """Pin the CURRENT head version (0 = nothing tracked yet).

        There is deliberately no ``actor`` parameter. It used to be accepted
        and silently dropped, so callers believed they were recording who
        made the checkpoint while nothing was stored (deep code review
        2026-09-24, finding 6). The ``checkpoints`` table has no column for
        it, and for the one caller that cared the information is already in
        the note text - the failover hook writes "auto: pre-failover
        snapshot (...)". Recording it properly is a schema change for a
        field nothing reads; delete the parameter rather than let the
        signature keep lying.

        ``session_pk=None`` raises rather than pinning unscoped - see
        ``core/scope.py``.
        """
        if session_pk is None:
            raise UnresolvedScopeError(
                "create_checkpoint needs a resolved owning session: "
                "session_pk=None means the caller is not the owner. Omit "
                "session_pk entirely for the legacy single-tenant path."
            )
        # NULL on the legacy path - the sentinel is not a SQL value.
        scope_pk = None if session_pk is UNSCOPED else session_pk
        scope = (
            task_states.c.session_pk == scope_pk
            if scope_pk is not None
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
                checkpoints_table.insert().values(
                    session_id=session_id,
                    session_pk=scope_pk,
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
        *, session_pk: int | None | _Unscoped = UNSCOPED,
    ) -> list[dict]:
        """Newest-first checkpoints for this session.

        The table is imported as ``checkpoints_table`` so this method's own
        name does not shadow it inside the class body (deep code review
        2026-09-24, finding 9) - the shadowing was harmless, since a class
        attribute is not a module global, but it read as though the body
        were calling itself.
        """
        if session_pk is None:
            return []
        scope = (
            checkpoints_table.c.session_pk == session_pk
            if session_pk is not UNSCOPED
            else checkpoints_table.c.session_id == session_id
        )
        query = (
            checkpoints_table.select()
            .where(scope)
            .order_by(checkpoints_table.c.id.desc())
            .limit(limit)
        )
        if task_key is not None:
            query = query.where(checkpoints_table.c.task_key == task_key)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Reactive failover checkpointing (Platform Phase 4)

    async def reactive_checkpoint(
        self, session_id: str,
        *, session_pk: int | None | _Unscoped = UNSCOPED,
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
                session_id, task_key, note=note,
                session_pk=session_pk,
            ))
        return created

    def failover_hook(self):
        """Build the callable wired into ``Router.failover_hook`` by the
        lifespan - the Router stays continuity-agnostic (layering rule)."""

        async def _hook(*, request_id: str, session_id: str,
                        session_pk: int | None | _Unscoped,
                        failed_provider: str | None,
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

    async def interruption_note(
        self, session_id: str,
        *, session_pk: int | None | _Unscoped = UNSCOPED,
    ) -> str | None:
        """Public projection hook: describe the post-checkpoint upstream
        failure for this session, if one exists."""
        if self._runs is None or session_pk is None:
            return None
        cps = await self.checkpoints(session_id, limit=1,
                                     session_pk=session_pk)
        recent = await self._runs.recent(
            session_id=session_id, limit=10, session_pk=session_pk)
        return self._interruption_from(cps[0] if cps else None, recent)

    @staticmethod
    def _interruption_from(
        latest_checkpoint: dict | None, recent_runs: list[dict] | None,
    ) -> str | None:
        """The note text for an upstream failure newer than the last
        checkpoint, decided from rows already in hand.

        Shared tail of :meth:`interruption_note` and
        :meth:`context_snapshot` so the graph projection and the
        continuation brief can never disagree about whether the previous
        attempt ended badly.
        """
        if recent_runs is None:
            return None
        cp_after = (
            latest_checkpoint["created_at"] if latest_checkpoint else 0.0
        )
        for run in recent_runs:  # newest-first
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

    async def context_snapshot(
        self, session_id: str,
        *, session_pk: int | None | _Unscoped = UNSCOPED,
        task_limit: int = _MAX_TASK_KEYS_RENDERED,
    ) -> dict:
        """Everything :meth:`context_message` renders, in a FIXED four
        queries.

        The brief used to await ``get_state`` and ``checkpoints`` once per
        task key - 13 round-trips at the five keys it renders, on the
        critical path of every chat request (deep code review 2026-09-24,
        finding 7). PostgreSQL's ``DISTINCT ON`` is the "latest row per
        ``task_key``" primitive that collapses those per-key reads, so the
        brief now costs the same whether the session tracks one task or
        five: the key list, the heads, the newest checkpoint per key, and
        the recent runs.

        Returns ``{"task_keys", "states", "checkpoints",
        "latest_checkpoint", "runs"}``. ``runs`` is ``None`` when no run
        store is attached. ``latest_checkpoint`` is the session's newest
        checkpoint row - also the max-``id`` row of the per-key result,
        since the globally newest checkpoint belongs to some key and for
        that key it *is* the newest.

        Scope follows ``core/scope.py`` exactly as the reads it replaces:
        ``session_pk=None`` (scoped, owner unresolved) returns an empty
        snapshot and issues NO SQL, the same fail-closed answer
        ``get_state``/``checkpoints`` give, while an omitted ``session_pk``
        keeps the legacy string-keyed path.
        """
        if session_pk is None:
            return self._empty_snapshot()
        task_keys = await self.active_task_keys(
            session_id, limit=task_limit, session_pk=session_pk
        )
        if not task_keys:
            # Same answer ``context_message`` gives, without the rest.
            return self._empty_snapshot()

        state_scope = (
            task_states.c.session_pk == session_pk
            if session_pk is not UNSCOPED
            else task_states.c.session_id == session_id
        )
        checkpoint_scope = (
            checkpoints_table.c.session_pk == session_pk
            if session_pk is not UNSCOPED
            else checkpoints_table.c.session_id == session_id
        )
        heads = (
            select(
                task_states.c.task_key,
                task_states.c.status,
                task_states.c.payload,
                task_states.c.version,
                task_states.c.updated_by,
                task_states.c.updated_at,
            )
            .distinct(task_states.c.task_key)
            .where(state_scope, task_states.c.task_key.in_(task_keys))
            .order_by(task_states.c.task_key,
                      task_states.c.version.desc())
        )
        # One row per task key for the WHOLE session, not just the rendered
        # five: the newest checkpoint may belong to a key outside that
        # window, and the interruption note must still see it.
        newest_checkpoint = (
            select(checkpoints_table)
            .distinct(checkpoints_table.c.task_key)
            .where(checkpoint_scope)
            .order_by(checkpoints_table.c.task_key,
                      checkpoints_table.c.id.desc())
        )
        async with self.engine.connect() as conn:
            head_rows = (await conn.execute(heads)).mappings().all()
            cp_rows = (await conn.execute(newest_checkpoint)).mappings().all()

        states = {
            row["task_key"]: {
                "session_id": session_id,
                "task_key": row["task_key"],
                "status": row["status"],
                "payload": row["payload"],   # JSONB -> dict already
                "version": row["version"],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
            }
            for row in head_rows
        }
        by_key: dict[str, dict] = {}
        latest = None
        for row in cp_rows:
            cp = dict(row)
            by_key[cp["task_key"]] = cp
            if latest is None or cp["id"] > latest["id"]:
                latest = cp

        runs = None
        if self._runs is not None:
            runs = await self._runs.recent(
                session_id=session_id, limit=10, session_pk=session_pk)
        return {
            "task_keys": task_keys,
            "states": states,
            "checkpoints": by_key,
            "latest_checkpoint": latest,
            "runs": runs,
        }

    @staticmethod
    def _empty_snapshot() -> dict:
        """The no-tasks / unresolved-owner answer: nothing to render, and
        no query worth issuing to find that out."""
        return {
            "task_keys": [],
            "states": {},
            "checkpoints": {},
            "latest_checkpoint": None,
            "runs": None,
        }

    @staticmethod
    def _render_payload(payload: dict) -> str:
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(rendered) > MAX_RENDER_CHARS_PER_TASK:
            rendered = rendered[:MAX_RENDER_CHARS_PER_TASK] + "…[truncated]"
        return rendered

    async def context_message(self, session_id: str, *,
                              session_pk: int | None | _Unscoped = UNSCOPED,
                              ) -> dict | None:
        """The injectable system message carrying the continuation brief,
        or None when the session tracks no tasks.

        Composed from ONE :meth:`context_snapshot` (a fixed four queries)
        rather than a ``get_state``/``checkpoints`` pair per task key -
        13 round-trips per chat request at the five rendered keys (deep
        code review 2026-09-24, finding 7). Rendering, the whole-brief
        char cap, and the omission marker are unchanged.
        """
        from invincible.core.settings import settings

        if not settings.continuity_enabled() or session_pk is None:
            return None
        snap = await self.context_snapshot(
            session_id, session_pk=session_pk,
            task_limit=_MAX_TASK_KEYS_RENDERED,
        )
        task_keys = snap["task_keys"]
        if not task_keys:
            return None

        lines = [
            "[Session continuity — canonical task state maintained by "
            "Invincible. Trust this over reconstructed transcript details.]"
        ]
        interruption = self._interruption_from(
            snap["latest_checkpoint"], snap["runs"])
        if interruption:
            lines.append(interruption)

        used = sum(len(line) + 1 for line in lines)
        omitted = False
        for idx, task_key in enumerate(task_keys):
            state = snap["states"].get(task_key)
            if state is None:
                continue
            chunk_lines = [
                f"Task '{task_key}' (status: {state['status']}, "
                f"v{state['version']}):",
                self._render_payload(state["payload"]),
            ]
            cp = snap["checkpoints"].get(task_key)
            if cp:
                chunk_lines.append(
                    f"Latest checkpoint #{cp['id']} "
                    f"(at v{cp['state_version']}): {cp['note']}"
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
    engine_or_engine_holder, session_id: str, *,
    session_pk: int | None | _Unscoped = UNSCOPED,
) -> dict | None:
    """Toggle-aware wrapper used by endpoints."""
    engine = engine_or_engine_holder
    if engine is None:
        return None
    return await engine.context_message(session_id, session_pk=session_pk)
