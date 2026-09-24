# invincible/core/session_store.py
"""Canonical session persistence on PostgreSQL.

Backed by the ``sessions`` / ``turns`` / ``messages`` tables declared in
``core.db`` (SQLAlchemy Core over asyncpg). Turn boundaries still reproduce
``core.trimming.group_into_turns`` exactly, message payloads remain full
JSON documents, retention deletes whole turns only - every behavioral
guarantee from Phase 15a survives.

Platform Phase 1 identity: ``sessions`` rows carry surrogate ownership -
``(user_id, project_id, client_session_id)`` UNIQUE - and ``turns.session_id``
is now a FK to ``sessions.id``. Every method takes the caller's
``user_id``/``project_id`` as REQUIRED arguments: there is deliberately no
local-owner fallback (multi-tenant audit Step 2, 2026-09-07), so a call site
that forgets its principal fails loudly instead of silently mixing users'
data. There is deliberately no cross-owner resolver either: the operator-era
``owner_context`` helper (bare client string -> any owner, oldest row wins)
was deleted with the role it served, so nothing here resolves a session
without a principal.

Concurrency: every write takes ``SELECT ... FOR UPDATE`` on the resolved
session row inside its transaction, so concurrent appends to one session
serialize instead of racing on MAX(seq)+1.
"""
import time

from sqlalchemy import Text, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from invincible.core.db import (
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

    async def resolve_or_create(
        self, session_id: str, *, user_id: int, project_id: int
    ) -> int:
        """Resolve-or-create within one transaction and take the row lock;
        returns the surrogate id (write paths, e.g. MCP task tools)."""
        async with self.engine.begin() as conn:
            return await self._resolve_for_write(
                conn, session_id, user_id, project_id
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
                   user_id: int, project_id: int) -> list:
        async with self.engine.begin() as conn:
            pk = await self._lookup_pk(conn, session_id, user_id, project_id)
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
                           user_id: int, project_id: int) -> dict | None:
        async with self.engine.connect() as conn:
            pk = await self._lookup_pk(conn, session_id, user_id, project_id)
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

    async def lookup_by_pk(
        self, session_pk: int, *, user_id: int, project_id: int,
    ) -> tuple[str, tuple[int, int]] | None:
        """``(client_session_id, owner_context)`` for this principal's
        surrogate row, or None - a foreign pk is indistinguishable from
        an unknown one (dashboard detail anti-enumeration)."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(
                    sessions.c.client_session_id,
                    sessions.c.user_id,
                    sessions.c.project_id,
                )
                .where(sessions.c.id == session_pk,
                       sessions.c.user_id == user_id,
                       sessions.c.project_id == project_id)
            )).first()
        if row is None:
            return None
        return str(row[0]), (int(row[1]), int(row[2]))

    async def turn_overview(self, session_id: str, *,
                            user_id: int, project_id: int) -> list[dict]:
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
            pk = await self._lookup_pk(conn, session_id, user_id, project_id)
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
                   user_id: int, project_id: int,
                   max_turns: int | None = None) -> None:
        """Full replace: wipe the session's turns/messages and re-insert
        ``messages`` through the boundary walker."""
        async with self.engine.begin() as conn:
            pk = await self._resolve_for_write(
                conn, session_id, user_id, project_id
            )
            await self._delete_turn_rows(conn, pk)
            await self._insert_grouped(conn, pk, new_messages)
            await self._bump_updated_at(conn, pk, time.time())
            await self._enforce_retention(conn, pk, max_turns)

    async def append(self, session_id: str, new_messages: list, *,
                     user_id: int, project_id: int,
                     max_turns: int | None = None) -> None:
        """Insert this request's new messages, opening/closing turns by the
        group_into_turns boundary rule.

        Retention: stored history bounded to the most recent
        INVINCIBLE_HISTORY_MAX_TURNS whole turns (default 200; 0/off off).
        ``max_turns`` (Phase 1 self-service) is the caller's per-user
        override; None = the env default.
        """
        if not new_messages:
            return
        async with self.engine.begin() as conn:
            pk = await self._resolve_for_write(
                conn, session_id, user_id, project_id
            )
            await self._insert_grouped(conn, pk, new_messages)
            await self._bump_updated_at(conn, pk, time.time())
            await self._enforce_retention(conn, pk, max_turns)

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
        """Newest ``(turn_id, has_messages, next_msg_seq, seq)`` or None.

        ``seq`` is the newest turn's own sequence, which lets the append
        walker number the turns it opens by counting up instead of
        re-querying ``MAX(seq)`` once per turn (deep code review
        2026-09-24, finding 7). Selecting it under ``GROUP BY turns.id``
        is fine: the id is the primary key, so every other column of
        ``turns`` is functionally dependent on it.
        """
        row = (await conn.execute(
            select(
                turns.c.id,
                turns.c.seq,
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
        turn_id, seq, any_msg, next_seq = row
        return turn_id, bool(any_msg), int(next_seq or 0), int(seq)

    async def _insert_grouped(
        self, conn, session_pk: int, msgs: list
    ) -> int:
        """Insert ``msgs``, opening a turn wherever the boundary rule says.

        The rule is ``core.trimming.group_into_turns`` exactly: a new turn
        opens on a user message that follows a turn with messages. It is
        walked FIRST, then the turns this batch opens go in as one
        multi-VALUES INSERT and the messages as one more - three statements
        for any batch. The loop this replaces issued one INSERT per message
        plus a ``MAX(seq)`` probe per turn opened: 17 statements for a
        10-message batch that opens 3 turns (deep code review 2026-09-24,
        finding 7). Turn sequences count up from the newest turn already on
        the session instead of re-probing, which is safe because the caller
        holds ``FOR UPDATE`` on the session row.

        Turn ids are mapped back by ``seq`` rather than trusting the order
        RETURNING hands them back. ``payload`` is JSONB: the message dict is
        bound as-is and serialized once by SQLAlchemy, never pre-dumped.
        """
        current = await self._last_turn(conn, session_pk)
        if current is None:
            turn_id, has_msgs, position, next_seq = None, False, 0, 0
        else:
            turn_id, has_msgs, position, last_seq = current
            next_seq = last_seq + 1

        # Walk the boundary rule once, recording where every message lands.
        # ``new_turns`` holds the sequence of each turn opened, in order; a
        # message placed in one refers to it by index.
        plan = []        # (turn_ref, position, message)
        new_turns = []   # seq of each newly opened turn
        for message in msgs:
            role = message.get("role")
            if turn_id is None or (role == "user" and has_msgs):
                turn_id = ("new", len(new_turns))
                new_turns.append(next_seq)
                next_seq += 1
                has_msgs = False
                position = 0
            plan.append((turn_id, position, message))
            has_msgs = True
            position += 1

        ids_by_seq: dict[int, int] = {}
        if new_turns:
            result = await conn.execute(
                turns.insert()
                .values([{"session_id": session_pk, "seq": seq}
                         for seq in new_turns])
                .returning(turns.c.id, turns.c.seq)
            )
            ids_by_seq = {int(seq): int(tid) for tid, seq in result.all()}

        rows = []
        for turn_ref, position, message in plan:
            role = message.get("role")
            rows.append({
                "turn_id": (
                    ids_by_seq[new_turns[turn_ref[1]]]
                    if isinstance(turn_ref, tuple) else turn_ref
                ),
                "seq": position,
                "role": role if isinstance(role, str) else str(role),
                "payload": message,
            })
        if rows:
            await conn.execute(messages.insert().values(rows))
        return len(rows)

    async def _enforce_retention(
        self, conn, session_pk: int, max_turns: int | None = None,
    ) -> None:
        limit = (
            history_max_turns() if max_turns is None else max(1, max_turns)
        )
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
        # for MAX(seq)+1 stays trivial) in ONE statement. The old loop read
        # the ids back and issued an UPDATE per turn - roughly 200 at the
        # default cap, inside the transaction already holding FOR UPDATE on
        # the session row (deep code review 2026-09-24, finding 7).
        await conn.execute(
            text(
                "UPDATE turns AS t SET seq = ranked.rn - 1"
                " FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY seq) AS rn"
                "         FROM turns WHERE session_id = :pk) AS ranked"
                " WHERE ranked.id = t.id"
            ),
            {"pk": session_pk},
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
