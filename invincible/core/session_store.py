# invincible/core/session_store.py
import asyncio
import json
import os
import time

import aiosqlite

from invincible.core.trimming import group_into_turns


def default_db_path() -> str:
    """Pick the session database location.

    Priority: explicit ``db_path`` argument (handled by the caller), then
    the INVINCIBLE_DB_PATH environment variable, then ``sessions.db`` in the
    current working directory. Never resolves inside the installed package.
    """
    env = os.getenv("INVINCIBLE_DB_PATH")
    if env:
        return env
    return os.path.join(os.getcwd(), "sessions.db")


class SessionStore:
    """Single-user local conversation memory backed by SQLite.

    Not a security boundary: session_id is a partition key, not a credential.
    Auth is handled entirely by GATEWAY_API_KEY upstream of this class.

    A single asyncio.Lock serializes the read-modify-write in :meth:`append`
    so two concurrent requests to the same session can never lose each
    other's turns (a plain load-then-save would last-write-wins).
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or default_db_path()
        self._db: aiosqlite.Connection | None = None
        self._append_lock = asyncio.Lock()

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                messages TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await self._db.commit()

    async def close(self):
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def load(self, session_id: str) -> list:
        async with self._db.execute(
            "SELECT messages FROM sessions WHERE session_id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return []
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            # Corrupt row (manual edit, interrupted write): treat as empty
            # rather than crashing the request.
            return []

    async def save(self, session_id: str, messages: list):
        await self._db.execute(
            """
            INSERT INTO sessions (session_id, messages, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                messages = excluded.messages,
                updated_at = excluded.updated_at
            """,
            (session_id, json.dumps(messages), time.time()),
        )
        await self._db.commit()

    async def append(self, session_id: str, new_messages: list):
        """Atomically load, extend and save a session's history.

        The lock makes this safe under concurrency: two requests to the
        same session each append their own turns instead of one clobbering
        the other's write. Callers should pass only the *new* turns for
        this request (e.g. the user messages plus the assistant reply) -
        never the history they loaded themselves.

        Retention (Phase 10): history is bounded to the most recent
        ``INVINCIBLE_HISTORY_MAX_TURNS`` turns (default 200; ``0``/``off``
        disables). Facts extracted from rolled-off turns survive in the
        facts table (core/memory.py); turns are dropped atomically via the
        router's ``group_into_turns`` so a tool_call is never separated
        from its tool result.
        """
        async with self._append_lock:
            current = await self.load(session_id)
            await self.save(session_id, current + new_messages)
            await self._enforce_retention(session_id)

    async def _enforce_retention(self, session_id: str):
        limit = history_max_turns()
        if limit is None:
            return
        messages = await self.load(session_id)
        turns = group_into_turns(messages)
        if len(turns) > limit:
            kept = [m for turn in turns[-limit:] for m in turn]
            await self.save(session_id, kept)


def history_max_turns() -> int | None:
    """Stored-history turn cap (default 200); ``0``/``off`` disables."""
    raw = os.getenv("INVINCIBLE_HISTORY_MAX_TURNS", "").strip().lower()
    if raw in ("0", "off"):
        return None
    try:
        return max(1, int(raw)) if raw else 200
    except ValueError:
        return 200
