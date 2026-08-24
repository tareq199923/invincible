# invincible/core/session_store.py
"""Canonical session persistence: sessions_v2 / turns / messages (Phase 15a).

Storage layout:

- ``sessions_v2`` - one row per session (id + timestamps).
- ``turns`` - ordered turns per session. Turn boundaries reproduce
  ``core.trimming.group_into_turns`` exactly: a new turn starts at each
  ``user`` message (when the current turn already has messages); anything
  else attaches to the current turn. A batch whose FIRST message is
  ``role:"tool"`` or ``role:"assistant"`` therefore continues the previous
  turn, matching the historical concat-then-group semantics.
- ``messages`` - ordered messages per turn. ``payload`` holds the ENTIRE
  original message dict as JSON (with ``role`` extracted alongside purely
  for indexing), so arbitrary client fields round-trip losslessly.

Public API is unchanged from the JSON-blob era: ``load`` / ``save`` /
``append`` / ``connection``, plus the ``asyncio.Lock`` serializing appends.

LEGACY MIGRATION - ONE-SHOT, FROZEN BACKUP (read this before touching the
old table):

- The pre-15a table was ``sessions (session_id, messages TEXT /*JSON blob*/,
  updated_at REAL)``.
- On :meth:`init`, if that table exists and the ``_invincible_schema``
  marker is absent, every legacy row is transformed through the same
  boundary walker INSIDE ONE TRANSACTION, with a post-condition message-
  count check; any failure rolls back and raises loudly.
- The legacy table is a FROZEN BACKUP afterwards: never read, written, or
  dropped by this code. Rollback = revert this module to the previous
  commit (the old code reads the blob table again; chat turns appended
  after migration are lost - accepted and intentional).
- Corrupt legacy JSON is tolerated exactly as the old ``load`` tolerated
  it: treated as an empty history, with the session row preserved.
"""
import asyncio
import json
import os
import time

import aiosqlite

from invincible.core.settings import settings


def default_db_path() -> str:
    """Pick the session database location.

    Priority: explicit ``db_path`` argument (handled by the caller), then
    the INVINCIBLE_DB_PATH environment variable (via Settings), then
    ``sessions.db`` in the current working directory. Never resolves inside
    the installed package.
    """
    env = settings.db_path()
    if env:
        return env
    return os.path.join(os.getcwd(), "sessions.db")


def history_max_turns() -> int | None:
    """Stored-history turn cap (default 200); ``0``/``off`` disables."""
    return settings.history_max_turns()


_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions_v2 (
    session_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions_v2(session_id),
    seq INTEGER NOT NULL,
    UNIQUE(session_id, seq)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL REFERENCES turns(id),
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(turn_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_turn ON messages(turn_id, seq);
CREATE TABLE IF NOT EXISTS _invincible_schema (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Cross-store write serialization (Phase 15 review, M1).
#
# MemoryStore, RunStore, and ContinuityEngine share THIS store's single
# aiosqlite connection. SQLite transactions live on the connection, not the
# coroutine: if a sibling store commits while SessionStore's explicit
# BEGIN..commit window is open, the sibling's commit() finalizes SessionStore's
# PARTIAL transaction - and a later rollback() can no longer undo it, leaving
# malformed half-turns behind. Every writer that touches this connection must
# therefore hold this one process-wide lock across its whole transaction.
_WRITE_LOCK = asyncio.Lock()


def shared_db_write_lock() -> asyncio.Lock:
    """The lock every shared-connection writer must hold across its
    transaction. Lock ordering: any store-private lock FIRST, then this one
    - never the reverse."""
    return _WRITE_LOCK


class SessionStore:
    """Single-user local conversation storage backed by SQLite.

    Not a security boundary: session_id is a partition key, not a credential.
    Auth is handled entirely by GATEWAY_API_KEY upstream of this class.

    A single asyncio.Lock serializes mutations (:meth:`append`,
    :meth:`save`) so two concurrent requests to the same session can never
    interleave half-way; reads are lock-free snapshots of committed rows.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or default_db_path()
        self._db: aiosqlite.Connection | None = None
        self._append_lock = asyncio.Lock()

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        # Enforce the declared REFERENCES clauses (m3 review fix). Safe for
        # legacy databases: enforcement applies to NEW writes only.
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(_V2_SCHEMA)
        await self._db.commit()
        await self._migrate_legacy_if_needed()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def connection(self) -> aiosqlite.Connection | None:
        """The live aiosqlite connection, or ``None`` before :meth:`init`.

        Public accessor so companion stores (MemoryStore, RunStore) can
        share this store's single connection - required for ``:memory:``
        databases, where a second connection would be a different database -
        without reaching into the private ``_db`` attribute (Phase 13 fix).
        """
        return self._db

    # ------------------------------------------------------------------
    # Public API (unchanged shapes)

    async def load(self, session_id: str) -> list:
        if self._db is None:
            return []
        async with self._db.execute(
            """
            SELECT m.payload FROM turns t
            JOIN messages m ON m.turn_id = t.id
            WHERE t.session_id = ?
            ORDER BY t.seq ASC, m.seq ASC
            """,
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        try:
            return [json.loads(row[0]) for row in rows]
        except (json.JSONDecodeError, TypeError):
            # Corrupt payload (manual edit, interrupted write): treat the
            # session as empty rather than crashing the request.
            return []

    async def save(self, session_id: str, messages: list) -> None:
        """Full replace: wipe the session's turns/messages and re-insert
        ``messages`` through the same boundary walker used by appends."""
        async with self._append_lock, shared_db_write_lock():
            await self._db.execute("BEGIN")
            try:
                await self._delete_session_rows(session_id)
                await self._ensure_session_row(session_id, time.time())
                await self._insert_grouped(session_id, messages)
                await self._bump_updated_at(session_id, time.time())
                await self._enforce_retention(session_id)
            except Exception:
                await self._db.rollback()
                raise
            await self._db.commit()

    async def append(self, session_id: str, new_messages: list) -> None:
        """Insert this request's new messages, opening/closing turns by the
        group_into_turns boundary rule.

        Callers pass only the *new* turns for this request (e.g. the user
        messages plus the assistant reply) - never the history they loaded
        themselves.

        Retention (Phase 10): stored history is bounded to the most recent
        ``INVINCIBLE_HISTORY_MAX_TURNS`` TURNS (default 200; ``0``/``off``
        disables). Deletion is whole-turn only - a retained turn keeps every
        message it arrived with, so a tool_call is never separated from its
        tool result. Facts extracted from rolled-off turns survive in the
        facts table (core/memory.py).
        """
        if not new_messages:
            return
        async with self._append_lock, shared_db_write_lock():
            await self._db.execute("BEGIN")
            try:
                await self._ensure_session_row(session_id, time.time())
                await self._insert_grouped(session_id, new_messages)
                await self._bump_updated_at(session_id, time.time())
                await self._enforce_retention(session_id)
            except Exception:
                await self._db.rollback()
                raise
            await self._db.commit()

    # ------------------------------------------------------------------
    # Internals

    async def _ensure_session_row(self, session_id: str, now: float) -> None:
        await self._db.execute(
            """
            INSERT INTO sessions_v2 (session_id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO NOTHING
            """,
            (session_id, now, now),
        )

    async def _bump_updated_at(self, session_id: str, now: float) -> None:
        await self._db.execute(
            "UPDATE sessions_v2 SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )

    async def _last_turn(self, session_id: str) -> tuple[int, bool, int] | None:
        """The newest turn as ``(turn_id, has_messages, next_msg_seq)``,
        or None when the session has no turns yet."""
        async with self._db.execute(
            """
            SELECT t.id,
                   EXISTS(SELECT 1 FROM messages m WHERE m.turn_id = t.id),
                   COALESCE(
                       (SELECT MAX(m.seq) + 1 FROM messages m
                        WHERE m.turn_id = t.id),
                       0
                   )
            FROM turns t WHERE t.session_id = ?
            ORDER BY t.seq DESC LIMIT 1
            """,
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return (row[0], bool(row[1]), row[2]) if row else None

    async def _insert_grouped(self, session_id: str, messages: list) -> int:
        """Insert ``messages`` applying the group_into_turns boundary rule.

        Returns the number of messages inserted (used by migration's
        post-condition check).
        """
        current = await self._last_turn(session_id)
        if current is None:
            turn_id = None
            turn_has_messages = False
            position_in_turn = 0
        else:
            turn_id, turn_has_messages, position_in_turn = current
        inserted = 0
        for message in messages:
            role = message.get("role")
            open_new_turn = turn_id is None or (
                role == "user" and turn_has_messages
            )
            if open_new_turn:
                cursor = await self._db.execute(
                    """
                    INSERT INTO turns (session_id, seq)
                    SELECT ?, COALESCE(MAX(t.seq), -1) + 1 FROM turns t
                    WHERE t.session_id = ?
                    """,
                    (session_id, session_id),
                )
                turn_id = cursor.lastrowid
                turn_has_messages = False
                position_in_turn = 0
            await self._db.execute(
                """
                INSERT INTO messages (turn_id, seq, role, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    turn_id,
                    position_in_turn,
                    role if isinstance(role, str) else str(role),
                    json.dumps(message, ensure_ascii=False),
                ),
            )
            turn_has_messages = True
            position_in_turn += 1
            inserted += 1
        return inserted

    async def _enforce_retention(self, session_id: str) -> None:
        """Whole-turn deletion only: drop the OLDEST turns beyond the cap.

        A turn is never partially trimmed, so tool_calls stay attached to
        their results (matches the blob-era group_into_turns trimming).
        """
        limit = history_max_turns()
        if limit is None:
            return
        async with self._db.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id = ?", (session_id,)
        ) as cursor:
            (count,) = await cursor.fetchone()
        if count <= limit:
            return
        async with self._db.execute(
            """
            SELECT MIN(seq) FROM (
                SELECT seq FROM turns WHERE session_id = ?
                ORDER BY seq DESC LIMIT ?
            )
            """,
            (session_id, limit),
        ) as cursor:
            (keep_from_seq,) = await cursor.fetchone()
        if keep_from_seq is None:
            return
        await self._db.execute(
            """
            DELETE FROM messages WHERE turn_id IN (
                SELECT id FROM turns WHERE session_id = ? AND seq < ?
            )
            """,
            (session_id, keep_from_seq),
        )
        await self._db.execute(
            "DELETE FROM turns WHERE session_id = ? AND seq < ?",
            (session_id, keep_from_seq),
        )
        # Re-sequence remaining turns so seq stays dense (keeps future
        # MAX(seq)+1 arithmetic trivial and ordering stable).
        async with self._db.execute(
            "SELECT id FROM turns WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        ) as cursor:
            ids = [row[0] for row in await cursor.fetchall()]
        for new_seq, turn_pk in enumerate(ids):
            await self._db.execute(
                "UPDATE turns SET seq = ? WHERE id = ?", (new_seq, turn_pk)
            )

    async def _delete_session_rows(self, session_id: str) -> None:
        await self._db.execute(
            """
            DELETE FROM messages WHERE turn_id IN (
                SELECT id FROM turns WHERE session_id = ?
            )
            """,
            (session_id,),
        )
        await self._db.execute(
            "DELETE FROM turns WHERE session_id = ?", (session_id,)
        )
        await self._db.execute(
            "DELETE FROM sessions_v2 WHERE session_id = ?", (session_id,)
        )

    # ------------------------------------------------------------------
    # Legacy one-shot migration (see module docstring)

    async def _table_exists(self, name: str) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _migration_done(self) -> bool:
        if not await self._table_exists("_invincible_schema"):
            return False
        async with self._db.execute(
            "SELECT 1 FROM _invincible_schema WHERE key = 'sessions_migrated'"
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _mark_migrated(self, when: float) -> None:
        await self._db.execute(
            """
            INSERT INTO _invincible_schema (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("sessions_migrated", str(when)),
        )

    async def _migrate_legacy_if_needed(self) -> None:
        if await self._migration_done():
            return
        if not await self._table_exists("sessions"):
            # Fresh database - nothing to migrate; still mark so a legacy
            # table appearing later (file reuse across versions) is handled
            # explicitly rather than silently.
            await self._mark_migrated(time.time())
            await self._db.commit()
            return

        async with self._db.execute(
            "SELECT session_id, messages, updated_at FROM sessions"
        ) as cursor:
            legacy_rows = await cursor.fetchall()

        parsed: list[tuple[str, float, list]] = []
        expected_count = 0
        for session_id, blob, updated_at in legacy_rows:
            try:
                messages = json.loads(blob) if blob else []
            except (json.JSONDecodeError, TypeError):
                messages = []
            if not isinstance(messages, list):
                messages = []
            expected_count += len(messages)
            parsed.append((session_id, float(updated_at), messages))

        inserted = 0
        async with shared_db_write_lock():
            await self._db.execute("BEGIN")
            try:
                for session_id, updated_at, messages in parsed:
                    await self._db.execute(
                        """
                        INSERT INTO sessions_v2 (session_id, created_at, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(session_id) DO UPDATE SET
                            updated_at = excluded.updated_at
                        """,
                        (session_id, updated_at, updated_at),
                    )
                    # created_at is unknowable from the blob era; updated_at is
                    # the best available floor (documented trade-off).
                    inserted += await self._insert_grouped(session_id, messages)
                if inserted != expected_count:
                    raise RuntimeError(
                        "Legacy session migration post-condition failed: "
                        f"{inserted} messages inserted, {expected_count} expected"
                    )
                await self._mark_migrated(time.time())
            except Exception:
                await self._db.rollback()
                raise
            await self._db.commit()
