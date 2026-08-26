# invincible/core/session_store.py
"""Canonical session persistence on PostgreSQL.

Backed by the ``sessions`` / ``turns`` / ``messages`` tables declared in
``core.db`` (SQLAlchemy Core over asyncpg). Turn boundaries still reproduce
``core.trimming.group_into_turns`` exactly, message payloads remain full
JSON documents, retention deletes whole turns only - every behavioral
guarantee from Phase 15a survives.

Platform Phase 1 identity: ``sessions`` rows carry surrogate ownership -
``(user_id, project_id, client_session_id)`` UNIQUE - and ``turns.session_id``
is now a FK to ``sessions.id``. The public API keeps taking the client
session string; every method accepts optional ``user_id``/``project_id``
and falls back to the system *local* owner when omitted, so single-tenant
call sites behave exactly as before. Ownership predicates on cross-user
paths arrive in Phase 2.

Concurrency: every write takes ``SELECT ... FOR UPDATE`` on the resolved
session row inside its transaction, so concurrent appends to one session
serialize instead of racing on MAX(seq)+1.
"""
import time

from sqlalchemy import Text, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from invincible.core.db import (
    ensure_local_owner,
    messages,
    sessions,
    turns,
)
from invincible.core.settings import settings


def history_max_turns() -> int | None:
    """Stored-history turn cap (default 200); ``0``/``off`` disables."""
    return settings.history_max_turns()


class SessionStore:
    def __init__(self, engine):
        self.engine = engine
        # Memoized fallback context: the system local owner, resolved once
        # per store instance on first owner-less call.
        self._local_owner: tuple[int, int] | None = None

    async def init(self) -> None:
        """Schema is owned by core.db metadata (create_all / Alembic);
        kept as a no-op so lifespan/fixture call sites stay stable."""

    async def close(self) -> None:
        """Engines are owned/disposed by the process lifespan."""

    # ------------------------------------------------------------------
    # Ownership resolution

    async def lookup(
        self, session_id: str, *, user_id: int, project_id: int
    ) -> int | None:
        """Surrogate session id for this ownership triple, or None when
        the principal has no such session (read paths)."""
        async with self.engine.begin() as conn:
            return await self._lookup_pk(conn, session_id, user_id, project_id)

    async def owner_context(self, session_id: str) -> tuple[int, int] | None:
        """The ``(user_id, project_id)`` owning ANY session with this
        client string (oldest row wins).

        Operator-only resolution for the graph override: a bare string is
        ambiguous under multi-user identity, so this is deliberately NOT
        part of any user-scoped path."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(sessions.c.user_id, sessions.c.project_id)
                .where(sessions.c.client_session_id == session_id)
                .order_by(sessions.c.id.asc())
                .limit(1)
            )).first()
        return (int(row[0]), int(row[1])) if row else None

    async def resolve_or_create(
        self, session_id: str, *, user_id: int, project_id: int
    ) -> int:
        """Resolve-or-create within one transaction and take the row lock;
        returns the surrogate id (write paths, e.g. MCP task tools)."""
        uid, pid = await self._owner(user_id, project_id)
        async with self.engine.begin() as conn:
            return await self._resolve_for_write(
                conn, session_id, uid, pid
            )

    async def _owner(
        self, user_id: int | None, project_id: int | None
    ) -> tuple[int, int]:
        """Effective ``(user_id, project_id)`` for this call. Either part
        may be pinned explicitly; missing parts fall back to the system
        local owner."""
        if user_id is not None and project_id is not None:
            return user_id, project_id
        if self._local_owner is None:
            self._local_owner = await ensure_local_owner(self.engine)
        fallback_user, fallback_project = self._local_owner
        return (
            fallback_user if user_id is None else user_id,
            fallback_project if project_id is None else project_id,
        )

    @staticmethod
    async def _lookup_pk(
        conn, client_session_id: str, user_id: int, project_id: int
    ) -> int | None:
        row = (await conn.execute(
            select(sessions.c.id).where(
                sessions.c.user_id == user_id,
                sessions.c.project_id == project_id,
                sessions.c.client_session_id == client_session_id,
            )
        )).first()
        return int(row[0]) if row else None

    # ------------------------------------------------------------------
    # Reads

    async def load(self, session_id: str, *,
                   user_id: int | None = None,
                   project_id: int | None = None) -> list:
        uid, pid = await self._owner(user_id, project_id)
        async with self.engine.begin() as conn:
            pk = await self._lookup_pk(conn, session_id, uid, pid)
            if pk is None:
                return []
            rows = (await conn.execute(
                select(messages.c.payload)
                .join(turns, messages.c.turn_id == turns.c.id)
                .where(turns.c.session_id == pk)
                .order_by(turns.c.seq.asc(), messages.c.seq.asc())
            )).scalars().all()
        # payload is JSONB: SQLAlchemy already decoded each row to a dict.
        return [r for r in rows if isinstance(r, dict)]

    async def session_meta(self, session_id: str, *,
                           user_id: int | None = None,
                           project_id: int | None = None) -> dict | None:
        uid, pid = await self._owner(user_id, project_id)
        async with self.engine.connect() as conn:
            pk = await self._lookup_pk(conn, session_id, uid, pid)
            if pk is None:
                return None
            row = (await conn.execute(
                select(sessions.c.created_at, sessions.c.updated_at)
                .where(sessions.c.id == pk)
            )).first()
        return (
            {"created_at": row[0], "updated_at": row[1]} if row else None
        )

    async def list_for_user(
        self, user_id: int, *, project_id: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Read-only session listing for one owner (Phase 3 account API).
        Ownership predicate is mandatory - there is no fallback here."""
        query = (
            select(
                sessions.c.id,
                sessions.c.project_id,
                sessions.c.client_session_id,
                sessions.c.created_at,
                sessions.c.updated_at,
            )
            .where(sessions.c.user_id == user_id)
            .order_by(sessions.c.updated_at.desc())
            .limit(limit)
        )
        if project_id is not None:
            query = query.where(sessions.c.project_id == project_id)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [dict(r) for r in rows]

    async def count_for_user(
        self, user_id: int, *, project_id: int | None = None,
    ) -> int:
        """Exact session count for one owner (dashboard overview card).
        Ownership predicate is mandatory - no local-owner fallback."""
        query = (
            select(func.count())
            .select_from(sessions)
            .where(sessions.c.user_id == user_id)
        )
        if project_id is not None:
            query = query.where(sessions.c.project_id == project_id)
        async with self.engine.connect() as conn:
            return int((await conn.execute(query)).scalar_one())

    async def turn_overview(self, session_id: str, *,
                            user_id: int | None = None,
                            project_id: int | None = None) -> list[dict]:
        """Per-turn message counts + first-payload snippet (graph projection)."""
        uid, pid = await self._owner(user_id, project_id)
        msg_count = (
            select(func.count(messages.c.id))
            .where(messages.c.turn_id == turns.c.id)
            .correlate(turns)
            .scalar_subquery()
        )
        snippet = (
            # payload is JSONB - cast to Text for substr (PG has no
            # substr(jsonb, ...)).
            select(func.substr(messages.c.payload.cast(Text), 1, 120))
            .where(messages.c.turn_id == turns.c.id)
            .order_by(messages.c.seq.asc())
            .limit(1)
            .correlate(turns)
            .scalar_subquery()
        )
        async with self.engine.connect() as conn:
            pk = await self._lookup_pk(conn, session_id, uid, pid)
            if pk is None:
                return []
            rows = (await conn.execute(
                select(turns.c.seq, msg_count, snippet)
                .where(turns.c.session_id == pk)
                .order_by(turns.c.seq.asc())
            )).all()
        return [
            {"seq": seq, "message_count": count, "first_payload_snippet": snip}
            for seq, count, snip in rows
        ]

    # ------------------------------------------------------------------
    # Writes (each call = one transaction; PG isolation replaces the old
    # process-wide write lock)

    async def save(self, session_id: str, new_messages: list, *,
                   user_id: int | None = None,
                   project_id: int | None = None) -> None:
        """Full replace: wipe the session's turns/messages and re-insert
        ``messages`` through the boundary walker."""
        uid, pid = await self._owner(user_id, project_id)
        async with self.engine.begin() as conn:
            pk = await self._resolve_for_write(conn, session_id, uid, pid)
            await self._delete_turn_rows(conn, pk)
            await self._insert_grouped(conn, pk, new_messages)
            await self._bump_updated_at(conn, pk, time.time())
            await self._enforce_retention(conn, pk)

    async def append(self, session_id: str, new_messages: list, *,
                     user_id: int | None = None,
                     project_id: int | None = None) -> None:
        """Insert this request's new messages, opening/closing turns by the
        group_into_turns boundary rule.

        Retention: stored history bounded to the most recent
        INVINCIBLE_HISTORY_MAX_TURNS whole turns (default 200; 0/off off).
        """
        if not new_messages:
            return
        uid, pid = await self._owner(user_id, project_id)
        async with self.engine.begin() as conn:
            pk = await self._resolve_for_write(conn, session_id, uid, pid)
            await self._insert_grouped(conn, pk, new_messages)
            await self._bump_updated_at(conn, pk, time.time())
            await self._enforce_retention(conn, pk)

    # ------------------------------------------------------------------
    # Internals

    @staticmethod
    async def _resolve_for_write(
        conn, client_session_id: str, user_id: int, project_id: int,
        now: float | None = None,
    ) -> int:
        """Resolve-or-create the ownership triple and take ``FOR UPDATE``
        on the session row, so every writer for it queues behind one row
        lock. Returns the surrogate session id."""
        stamp = now if now is not None else time.time()
        await conn.execute(
            pg_insert(sessions)
            .values(user_id=user_id,
                    project_id=project_id,
                    client_session_id=client_session_id,
                    created_at=stamp,
                    updated_at=stamp)
            .on_conflict_do_nothing(
                index_elements=["user_id", "project_id", "client_session_id"])
        )
        row = (await conn.execute(
            select(sessions.c.id).where(
                sessions.c.user_id == user_id,
                sessions.c.project_id == project_id,
                sessions.c.client_session_id == client_session_id,
            ).with_for_update()
        )).one()
        return int(row[0])

    @staticmethod
    async def _bump_updated_at(conn, session_pk: int, now: float) -> None:
        await conn.execute(
            update(sessions)
            .where(sessions.c.id == session_pk)
            .values(updated_at=now)
        )

    async def _last_turn(self, conn, session_pk: int):
        """Newest ``(turn_id, has_messages, next_msg_seq)`` or None."""
        row = (await conn.execute(
            select(
                turns.c.id,
                func.count(messages.c.id) > 0,
                func.coalesce(func.max(messages.c.seq) + 1, 0),
            )
            .outerjoin(messages, messages.c.turn_id == turns.c.id)
            .where(turns.c.session_id == session_pk)
            .group_by(turns.c.id)
            .order_by(turns.c.seq.desc())
            .limit(1)
        )).first()
        if row is None:
            return None
        turn_id, any_msg, next_seq = row
        return turn_id, bool(any_msg), int(next_seq or 0)

    async def _insert_grouped(
        self, conn, session_pk: int, msgs: list
    ) -> int:
        current = await self._last_turn(conn, session_pk)
        if current is None:
            turn_id, has_msgs, position = None, False, 0
        else:
            turn_id, has_msgs, position = current
        inserted = 0
        for message in msgs:
            role = message.get("role")
            open_new = turn_id is None or (role == "user" and has_msgs)
            if open_new:
                max_seq = (await conn.execute(
                    select(func.max(turns.c.seq)).where(
                        turns.c.session_id == session_pk)
                )).scalar_one()
                result = await conn.execute(
                    turns.insert()
                    .values(session_id=session_pk,
                            seq=(max_seq if max_seq is not None else -1) + 1)
                    .returning(turns.c.id)
                )
                turn_id = result.scalar_one()
                has_msgs = False
                position = 0
            await conn.execute(
                messages.insert().values(
                    turn_id=turn_id,
                    seq=position,
                    role=role if isinstance(role, str) else str(role),
                    # JSONB column: bind the message object; SQLAlchemy
                    # serializes once. Never pre-dump into a JSONB column.
                    payload=message,
                )
            )
            has_msgs = True
            position += 1
            inserted += 1
        return inserted

    async def _enforce_retention(self, conn, session_pk: int) -> None:
        limit = history_max_turns()
        if limit is None:
            return
        count = (await conn.execute(
            select(func.count()).select_from(turns)
            .where(turns.c.session_id == session_pk)
        )).scalar_one()
        if count <= limit:
            return
        keep_from = (await conn.execute(
            select(func.min(turns.c.seq)).where(
                turns.c.id.in_(
                    select(turns.c.id)
                    .where(turns.c.session_id == session_pk)
                    .order_by(turns.c.seq.desc())
                    .limit(limit)
                )
            )
        )).scalar_one()
        if keep_from is None:
            return
        await conn.execute(
            delete(messages).where(
                messages.c.turn_id.in_(
                    select(turns.c.id).where(
                        turns.c.session_id == session_pk,
                        turns.c.seq < keep_from,
                    )
                )
            )
        )
        await conn.execute(
            delete(turns).where(
                turns.c.session_id == session_pk, turns.c.seq < keep_from
            )
        )
        # Re-sequence remaining turns densely (ordering stable, arithmetic
        # for MAX(seq)+1 stays trivial).
        ids = (await conn.execute(
            select(turns.c.id)
            .where(turns.c.session_id == session_pk)
            .order_by(turns.c.seq.asc())
        )).scalars().all()
        for new_seq, turn_pk in enumerate(ids):
            await conn.execute(
                update(turns).where(turns.c.id == turn_pk).values(seq=new_seq)
            )

    @staticmethod
    async def _delete_turn_rows(conn, session_pk: int) -> None:
        """Delete a session's turns/messages, keeping the session row (its
        FOR UPDATE lock and created_at survive full replaces)."""
        await conn.execute(
            delete(messages).where(
                messages.c.turn_id.in_(
                    select(turns.c.id).where(
                        turns.c.session_id == session_pk)
                )
            )
        )
        await conn.execute(
            delete(turns).where(turns.c.session_id == session_pk)
        )
