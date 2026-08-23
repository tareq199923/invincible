# invincible/core/run_store.py
"""Provider-run records (Phase 13.5).

One row per upstream attempt - successes, failovers, and terminal errors -
so "which provider/model actually handled a request and why did it move"
is queryable state rather than only log lines. This is the data source for
the future continuity graph and dashboard; Phase 16 carries the same shape
into PostgreSQL.

Shares the session SQLite database via ``SessionStore.connection()``
(same pattern as MemoryStore; required for ``:memory:`` databases, where a
second connection would be a different database). Writes are best-effort
from the caller's perspective: :meth:`record` raises, but the Router wraps
every call so persistence problems can never fail a chat completion.
"""
import json
import time

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL,
    session_id TEXT,
    provider_name TEXT NOT NULL,
    model_id TEXT NOT NULL,
    attempt_index INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    error_class TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    meta TEXT
)
"""


class RunStore:
    def __init__(self, db_path: str | None = None, shared=None):
        self._shared = shared
        if db_path is None and shared is not None:
            db_path = getattr(shared, "db_path", None)
        if db_path is None and shared is None:
            from invincible.core.session_store import default_db_path

            db_path = default_db_path()
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        accessor = getattr(self._shared, "connection", None) if self._shared else None
        shared_db = accessor() if callable(accessor) else None
        if shared_db is not None:
            self._db = shared_db
        elif self.db_path is not None:
            # Shared store without a live connection (test stubs) or a
            # standalone store: open our own connection. A stub with no
            # path leaves _db None - inert, so tests never touch the real
            # cwd database.
            self._shared = None
            self._db = await aiosqlite.connect(self.db_path)
        if self._db is None:
            return
        await self._db.execute(_SCHEMA)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_session "
            "ON runs(session_id, started_at)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_outcome ON runs(outcome)"
        )
        await self._db.commit()

    async def close(self) -> None:
        # A shared connection is owned (and closed) by the SessionStore.
        if self._shared is None and self._db is not None:
            await self._db.close()
        self._db = None

    async def record(self, entry: dict) -> int:
        """Insert one run row; returns the new row id.

        Required keys: request_id, provider_name, model_id, attempt_index,
        outcome, started_at. Optional: session_id, error_class, finished_at,
        meta (JSON-serialized mapping).
        """
        cursor = await self._db.execute(
            """
            INSERT INTO runs (
                request_id, session_id, provider_name, model_id,
                attempt_index, outcome, error_class, started_at,
                finished_at, meta
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["request_id"],
                entry.get("session_id"),
                entry["provider_name"],
                entry["model_id"],
                entry["attempt_index"],
                entry["outcome"],
                entry.get("error_class"),
                entry["started_at"],
                entry.get("finished_at"),
                json.dumps(entry["meta"]) if entry.get("meta") else None,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def recent(
        self, session_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Most recent runs, newest first; optionally scoped to a session."""
        if session_id is None:
            cursor = await self._db.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM runs WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            )
        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description]
        out = []
        for row in rows:
            item = dict(zip(columns, row, strict=False))
            if isinstance(item.get("meta"), str):
                item["meta"] = json.loads(item["meta"])
            out.append(item)
        return out


def new_run_entry(
    *,
    request_id: str,
    provider_name: str,
    model_id: str,
    attempt_index: int,
    started_at: float,
    outcome: str,
    session_id: str | None = None,
    error_class: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Assemble one record with finished_at stamped now."""
    return {
        "request_id": request_id,
        "session_id": session_id,
        "provider_name": provider_name,
        "model_id": model_id,
        "attempt_index": attempt_index,
        "outcome": outcome,
        "error_class": error_class,
        "started_at": started_at,
        "finished_at": time.time(),
        "meta": meta,
    }
