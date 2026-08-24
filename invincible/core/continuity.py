# invincible/core/continuity.py
"""Canonical task-state continuity shared by LLM requests and MCP tools
(Phase 15b - Phase 15 Requirement C).

Core idea: LLMs do NOT share memory. Invincible maintains canonical,
provider-neutral task state and hands a continuation brief to whichever
provider/model handles the next request - or to an MCP tool that asks for
it. Both worlds read/write through THIS engine; there is no separate
"MCP memory".

State model:

- ``task_states`` - versioned JSON payloads per ``(session_id, task_key)``.
  Writes are compare-and-set on ``version`` (optimistic concurrency);
  versions are monotonic per key, so "latest trusted state" is simply
  ``max(version)`` - never a timestamp comparison.
- ``checkpoints`` - immutable snapshots pinning a state version plus a
  human note ("completed through 37"). Recovery = latest checkpoint ∪ any
  newer committed state version.
- Interruption awareness: when the newest failed/slow-killed upstream
  attempt (from the ``runs`` records) post-dates the latest checkpoint,
  the rendered continuation brief says so explicitly, so the next model
  knows it is RESUMING, not starting.

Scope note: state enters this engine through EXPLICIT writes (MCP tools
today, admin/API later). Automatic extraction from LLM prose is
deliberately deferred - free text is never promoted to canonical state by
this module (structured state is the only canonical representation).

Provider neutrality: payloads are plain JSON dicts. Provider/model names
appear only in ``updated_by`` provenance strings and the runs table - the
same state serves GPT, Claude, Gemini, Qwen, anything.
"""
import asyncio
import json
import logging
import time

import aiosqlite

logger = logging.getLogger("invincible.continuity")

MAX_PAYLOAD_CHARS = 4096
MAX_RENDER_CHARS_PER_TASK = 1200
_MAX_TASK_KEYS_RENDERED = 5

_VALID_STATUSES = ("active", "blocked", "done", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    task_key TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL DEFAULT 'active',
    payload TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    request_id TEXT,
    updated_at REAL NOT NULL,
    UNIQUE(session_id, task_key, version)
);
CREATE INDEX IF NOT EXISTS idx_task_states_session
    ON task_states(session_id, task_key, version DESC);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    task_key TEXT NOT NULL DEFAULT 'default',
    state_version INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session
    ON checkpoints(session_id, task_key, created_at DESC);
"""


class ContinuityConflictError(Exception):
    """CAS rejection: another writer advanced the version first."""


class ContinuityEngine:
    def __init__(self, db_path: str | None = None, shared=None, runs=None):
        self._shared = shared
        self._runs = runs
        if db_path is None and shared is not None:
            db_path = getattr(shared, "db_path", None)
        if db_path is None and shared is None:
            from invincible.core.session_store import default_db_path

            db_path = default_db_path()
        self.db_path = db_path
        self._db = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        accessor = getattr(self._shared, "connection", None) if self._shared else None
        shared_db = accessor() if callable(accessor) else None
        if shared_db is not None:
            self._db = shared_db
        elif self.db_path is not None:
            self._shared = None
            self._db = await aiosqlite.connect(self.db_path)
        if self._db is None:
            return
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._shared is None and self._db is not None:
            await self._db.close()
        self._db = None

    # ------------------------------------------------------------------
    # State writes (CAS)

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
    ) -> dict:
        """Versioned upsert. Returns the new head
        ``{task_key,status,payload,version}``.

        Raises ValueError for oversized/non-dict payloads or bad statuses;
        raises :class:`ContinuityConflictError` when ``expected_version``
        no longer matches the current head.
        """
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object (dict)")
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

        async with self._lock:
            async with self._db.execute(
                """
                SELECT COALESCE(MAX(version), 0) FROM task_states
                WHERE session_id = ? AND task_key = ?
                """,
                (session_id, task_key),
            ) as cursor:
                (head,) = await cursor.fetchone()
            if expected_version is not None and expected_version != head:
                raise ContinuityConflictError(
                    f"expected version {expected_version} but current head "
                    f"is {head} for task '{task_key}'"
                )
            new_version = head + 1
            now = time.time()
            await self._db.execute(
                """
                INSERT INTO task_states (
                    session_id, task_key, status, payload, version,
                    updated_by, request_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    task_key,
                    status,
                    blob,
                    new_version,
                    actor,
                    request_id,
                    now,
                ),
            )
            await self._db.commit()
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
        self, session_id: str, task_key: str = "default"
    ) -> dict | None:
        return next(iter(await self.history(session_id, task_key, limit=1)), None)

    async def history(
        self, session_id: str, task_key: str = "default", limit: int = 20
    ) -> list[dict]:
        """Newest-first version history for one task key."""
        if self._db is None:
            return []
        async with self._db.execute(
            """
            SELECT status, payload, version, updated_by, updated_at
            FROM task_states WHERE session_id = ? AND task_key = ?
            ORDER BY version DESC LIMIT ?
            """,
            (session_id, task_key, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        out = []
        for status, payload_blob, version, updated_by, updated_at in rows:
            try:
                payload = json.loads(payload_blob)
            except json.JSONDecodeError:
                payload = {"raw": payload_blob}
            out.append(
                {
                    "session_id": session_id,
                    "task_key": task_key,
                    "status": status,
                    "payload": payload,
                    "version": version,
                    "updated_by": updated_by,
                    "updated_at": updated_at,
                }
            )
        return out

    async def active_task_keys(self, session_id: str, limit: int = 5) -> list[str]:
        """Task keys with any state, most-recently-updated first."""
        if self._db is None:
            return []
        async with self._db.execute(
            """
            SELECT task_key, MAX(updated_at) AS latest FROM task_states
            WHERE session_id = ? GROUP BY task_key
            ORDER BY latest DESC LIMIT ?
            """,
            (session_id, limit),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # Checkpoints

    async def create_checkpoint(
        self,
        session_id: str,
        task_key: str = "default",
        note: str = "",
        actor: str = "user",
    ) -> dict:
        """Pin the CURRENT head version (0 = nothing tracked yet)."""
        head = await self.get_state(session_id, task_key)
        version = head["version"] if head else 0
        now = time.time()
        cursor = await self._db.execute(
            """
            INSERT INTO checkpoints (
                session_id, task_key, state_version, note, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, task_key, version, note[:500], now),
        )
        await self._db.commit()
        return {
            "id": cursor.lastrowid,
            "session_id": session_id,
            "task_key": task_key,
            "state_version": version,
            "note": note[:500],
            "created_at": now,
        }

    async def checkpoints(
        self, session_id: str, task_key: str | None = None, limit: int = 20
    ) -> list[dict]:
        """Newest-first checkpoints, optionally scoped to one task key."""
        if self._db is None:
            return []
        if task_key is not None:
            sql = (
                "SELECT id, task_key, state_version, note, created_at "
                "FROM checkpoints WHERE session_id = ? AND task_key = ? "
                "ORDER BY id DESC LIMIT ?"
            )
            params: tuple = (session_id, task_key, limit)
        else:
            sql = (
                "SELECT id, task_key, state_version, note, created_at "
                "FROM checkpoints WHERE session_id = ? ORDER BY id DESC LIMIT ?"
            )
            params = (session_id, limit)
        async with self._db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "session_id": session_id,
                "task_key": r[1],
                "state_version": r[2],
                "note": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Continuation brief (the thing handed to the next model)

    async def interruption_note(self, session_id: str) -> str | None:
        """Public projection hook: describe the post-checkpoint upstream
        failure for this session, if one exists. The graph API surfaces it
        as the answer to \"why did work move / where did it stop?\"."""
        if self._runs is None:
            return None
        cps = await self.checkpoints(session_id, limit=1)
        cp_after = cps[0]["created_at"] if cps else 0.0
        recent = await self._runs.recent(session_id=session_id, limit=10)
        for run in recent:  # newest-first
            if float(run.get("finished_at") or 0) <= cp_after:
                continue
            # The NEWEST post-checkpoint run decides: a successful attempt
            # means work already continued past the interruption.
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

    async def context_message(self, session_id: str) -> dict | None:
        """The injectable system message carrying the continuation brief,
        or None when the session tracks no tasks.

        Rendered output is bounded (per-task truncation + key cap) because
        system messages are never trimmed by ``trim_messages``.
        """
        from invincible.core.settings import settings

        if self._db is None or not settings.continuity_enabled():
            return None
        task_keys = await self.active_task_keys(
            session_id, limit=_MAX_TASK_KEYS_RENDERED
        )
        if not task_keys:
            return None

        interruption = await self.interruption_note(session_id)
        lines = [
            "[Session continuity — canonical task state maintained by "
            "Invincible. Trust this over reconstructed transcript details.]"
        ]
        if interruption:
            lines.append(interruption)
        for task_key in task_keys:
            state = await self.get_state(session_id, task_key)
            if state is None:
                continue
            lines.append(
                f"Task '{task_key}' (status: {state['status']}, "
                f"v{state['version']}):"
            )
            lines.append(self._render_payload(state["payload"]))
            cps = await self.checkpoints(session_id, task_key, limit=1)
            if cps:
                cp = cps[0]
                lines.append(
                    f"Latest checkpoint #{cp['id']} (at v{cp['state_version']}): "
                    f"{cp['note']}"
                )
        lines.append("[End session continuity]")
        return {"role": "system", "content": "\n".join(lines)}


async def context_system_message(engine, session_id: str) -> dict | None:
    """Toggle-aware wrapper used by endpoints (mirrors memory.py style)."""
    if engine is None:
        return None
    return await engine.context_message(session_id)
