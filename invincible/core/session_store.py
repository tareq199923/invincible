# invincible/core/session_store.py
"""Canonical session persistence on PostgreSQL (Phase 16).

Backed by the ``sessions`` / ``turns`` / ``messages`` tables declared in
``core.db`` (SQLAlchemy Core over asyncpg). Turn boundaries still reproduce
``core.trimming.group_into_turns`` exactly, message payloads remain full
JSON documents, retention deletes whole turns only - every behavioral
guarantee from Phase 15a survives the backend swap.

Public API unchanged except the constructor now takes an async engine
(created once per process in ``main.lifespan`` from ``INVINCIBLE_DB_URL``).
Legacy SQLite files are migrated via ``invincible db import``
(core/db_import.py) - this module never touches them.
"""
import time

from sqlalchemy import Text, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from invincible.core.db import messages, sessions, turns
from invincible.core.settings import settings


def history_max_turns() -> int | None:
    """Stored-history turn cap (default 200); ``0``/``off`` disables."""
    return settings.history_max_turns()


class SessionStore:
    def __init__(self, engine):
        self.engine = engine

    async def init(self) -> None:
        """Schema is owned by core.db metadata (create_all / Alembic);
        kept as a no-op so lifespan/fixture call sites stay stable."""

    async def close(self) -> None:
        """Engines are owned/disposed by the process lifespan."""

    # ------------------------------------------------------------------
    # Reads

    async def load(self, session_id: str) -> list:
        async with self.engine.begin() as conn:
            rows = (await conn.execute(
                select(messages.c.payload)
                .join(turns, messages.c.turn_id == turns.c.id)
                .where(turns.c.session_id == session_id)
                .order_by(turns.c.seq.asc(), messages.c.seq.asc())
            )).scalars().all()
        # payload is JSONB: SQLAlchemy already decoded each row to a dict.
        return [r for r in rows if isinstance(r, dict)]

    async def session_meta(self, session_id: str) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(
                    sessions.c.created_at,
                    sessions.c.updated_at,
                ).where(sessions.c.session_id == session_id)
            )).first()
        return (
            {"created_at": row[0], "updated_at": row[1]} if row else None
        )

    async def turn_overview(self, session_id: str) -> list[dict]:
        """Per-turn message counts + first-payload snippet (graph projection)."""
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
            rows = (await conn.execute(
                select(turns.c.seq, msg_count, snippet)
                .where(turns.c.session_id == session_id)
                .order_by(turns.c.seq.asc())
            )).all()
        return [
            {"seq": seq, "message_count": count, "first_payload_snippet": snip}
            for seq, count, snip in rows
        ]

    # ------------------------------------------------------------------
    # Writes (each call = one transaction; PG isolation replaces the old
    # process-wide write lock)

    async def save(self, session_id: str, new_messages: list) -> None:
        """Full replace: wipe the session's turns/messages and re-insert
        ``messages`` through the boundary walker."""
        async with self.engine.begin() as conn:
            await self._lock_session_row(conn, session_id)
            await self._delete_turn_rows(conn, session_id)
            await self._insert_grouped(conn, session_id, new_messages)
            await self._bump_updated_at(conn, session_id, time.time())
            await self._enforce_retention(conn, session_id)

    async def append(self, session_id: str, new_messages: list) -> None:
        """Insert this request's new messages, opening/closing turns by the
        group_into_turns boundary rule (see Phase 15a docstring in git
        history / core.db comments for the rule).

        Concurrency (Phase 16 scope item 3): every write takes
        ``SELECT ... FOR UPDATE`` on the session row inside its transaction,
        so concurrent appends to one session serialize instead of racing
        on MAX(seq)+1 - PostgreSQL isolation replaces the old process-wide
        write lock.

        Retention: stored history bounded to the most recent
        INVINCIBLE_HISTORY_MAX_TURNS whole turns (default 200; 0/off off).
        """
        if not new_messages:
            return
        async with self.engine.begin() as conn:
            await self._lock_session_row(conn, session_id)
            await self._insert_grouped(conn, session_id, new_messages)
            await self._bump_updated_at(conn, session_id, time.time())
            await self._enforce_retention(conn, session_id)

    # ------------------------------------------------------------------
    # Internals

    @staticmethod
    async def _lock_session_row(
        conn, session_id: str, now: float | None = None
    ) -> None:
        """Ensure the session row exists and take ``FOR UPDATE`` on it, so
        every writer for this session queues behind one row lock."""
        await conn.execute(
            pg_insert(sessions)
            .values(session_id=session_id,
                    created_at=now if now is not None else time.time(),
                    updated_at=now if now is not None else time.time())
            .on_conflict_do_nothing(index_elements=["session_id"])
        )
        await conn.execute(
            select(sessions.c.session_id)
            .where(sessions.c.session_id == session_id)
            .with_for_update()
        )

    @staticmethod
    async def _bump_updated_at(conn, session_id: str, now: float) -> None:
        await conn.execute(
            update(sessions)
            .where(sessions.c.session_id == session_id)
            .values(updated_at=now)
        )

    async def _last_turn(self, conn, session_id: str):
        """Newest ``(turn_id, has_messages, next_msg_seq)`` or None."""
        row = (await conn.execute(
            select(
                turns.c.id,
                func.count(messages.c.id) > 0,
                func.coalesce(func.max(messages.c.seq) + 1, 0),
            )
            .outerjoin(messages, messages.c.turn_id == turns.c.id)
            .where(turns.c.session_id == session_id)
            .group_by(turns.c.id)
            .order_by(turns.c.seq.desc())
            .limit(1)
        )).first()
        if row is None:
            return None
        turn_id, any_msg, next_seq = row
        return turn_id, bool(any_msg), int(next_seq or 0)

    async def _insert_grouped(self, conn, session_id: str, msgs: list) -> int:
        current = await self._last_turn(conn, session_id)
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
                        turns.c.session_id == session_id)
                )).scalar_one()
                result = await conn.execute(
                    turns.insert()
                    .values(session_id=session_id,
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

    async def _enforce_retention(self, conn, session_id: str) -> None:
        limit = history_max_turns()
        if limit is None:
            return
        count = (await conn.execute(
            select(func.count()).select_from(turns)
            .where(turns.c.session_id == session_id)
        )).scalar_one()
        if count <= limit:
            return
        keep_from = (await conn.execute(
            select(func.min(turns.c.seq)).where(
                turns.c.id.in_(
                    select(turns.c.id)
                    .where(turns.c.session_id == session_id)
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
                        turns.c.session_id == session_id,
                        turns.c.seq < keep_from,
                    )
                )
            )
        )
        await conn.execute(
            delete(turns).where(
                turns.c.session_id == session_id, turns.c.seq < keep_from
            )
        )
        # Re-sequence remaining turns densely (ordering stable, arithmetic
        # for MAX(seq)+1 stays trivial).
        ids = (await conn.execute(
            select(turns.c.id)
            .where(turns.c.session_id == session_id)
            .order_by(turns.c.seq.asc())
        )).scalars().all()
        for new_seq, turn_pk in enumerate(ids):
            await conn.execute(
                update(turns).where(turns.c.id == turn_pk).values(seq=new_seq)
            )

    @staticmethod
    async def _delete_turn_rows(conn, session_id: str) -> None:
        """Delete a session's turns/messages, keeping the session row (its
        FOR UPDATE lock and created_at survive full replaces)."""
        await conn.execute(
            delete(messages).where(
                messages.c.turn_id.in_(
                    select(turns.c.id).where(
                        turns.c.session_id == session_id)
                )
            )
        )
        await conn.execute(
            delete(turns).where(turns.c.session_id == session_id)
        )
