# invincible/core/memory.py
"""Graph-lite session fact memory (Roadmap Phase 10).

When turns are appended to a session, simple ``(entity, relation, target)``
facts are extracted via conservative pattern matching and stored in a
``facts`` table in the same SQLite database as the session history. On
later requests a compact, size-bounded summary is injected as a system
message, so context survives even after ``trim_messages`` drops the
original turns — including across mid-task provider/model switches.

Design constraints (from the roadmap phase):

- **Phase 4-proof schema** — rows are keyed ``(user_id, session_id)`` with
  ``user_id`` defaulting to the sentinel ``"default"`` until multi-user
  auth populates it; Phase 4 backfills instead of rebuilding.
- **Idempotent extraction** — a ``UNIQUE`` constraint on the fact tuple
  means re-persisting a turn (retries, both endpoints) never duplicates.
- **Bounded injection** — system messages are never trimmed, so the
  injected summary is capped (most-recent-first) or it would become the
  new unbounded-growth problem.
- **Precision over recall** — patterns target explicit statements
  ("remember that…", "my name is…", decisions, current task). A missed
  fact behaves like today; a wrong fact would mislead every future turn.

Toggles: ``INVINCIBLE_MEMORY=0/false/off`` disables extraction and
injection (default on). ``INVINCIBLE_MEMORY_MAX_FACTS`` sets the injection
cap (default 40).
"""
import re
import time

import aiosqlite

from invincible.core.settings import DEFAULT_MEMORY_MAX_FACTS, settings

SENTINEL_USER_ID = "default"
DEFAULT_MAX_FACTS = DEFAULT_MEMORY_MAX_FACTS
_TARGET_MAX_CHARS = 160

# (entity, relation) paired with a pattern whose first group is the target.
# Ordered roughly by confidence; all are matched case-insensitively.
_NAME = r"([A-Za-z][\w'-]*(?: [A-Za-z][\w'-]*){0,2})"
_PATTERNS = [
    ("user", "note", re.compile(r"\bremember that\s+(.+)", re.I)),
    ("user", "name", re.compile(r"\bmy name is\s+" + _NAME, re.I)),
    ("user", "name", re.compile(r"\bcall me\s+" + _NAME, re.I)),
    ("user", "preference", re.compile(r"\bI prefer\s+(.+)", re.I)),
    ("user", "toolchain", re.compile(r"\bI (?:use|work with)\s+(.+)", re.I)),
    ("project", "decision", re.compile(r"\bwe decided(?:\s+to)?\s+(.+)", re.I)),
    ("project", "decision", re.compile(r"\blet's go with\s+(.+)", re.I)),
    (
        "project",
        "current_task",
        re.compile(r"\b(?:I'?m |I am )?(?:currently )?working on\s+(.+)", re.I),
    ),
    ("project", "next_step", re.compile(r"\bthe next step is\s+(.+)", re.I)),
]


def memory_enabled() -> bool:
    """Whether fact extraction/injection is active (default on)."""
    return settings.memory_enabled()


def max_injected_facts() -> int:
    """Injection cap; most-recent-first beyond the limit."""
    return settings.memory_max_facts()


def _clean_target(raw: str) -> str:
    """Normalize a captured target: first sentence of the first line,
    trimmed and capped (free-text ``(.+)`` captures run to end-of-line,
    so cut at the first sentence boundary to keep facts atomic)."""
    target = raw.splitlines()[0].split(". ")[0].strip().strip('"').rstrip(".")
    return target[:_TARGET_MAX_CHARS]


def extract_facts(messages: list) -> list:
    """Extract ``(entity, relation, target)`` tuples from message contents.

    Scans user and assistant messages alike, but the patterns only fire on
    explicit statements, so assistant verbosity rarely matches. Returns
    tuples in first-seen order, deduplicated within the batch.
    """
    facts = []
    seen = set()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        for entity, relation, pattern in _PATTERNS:
            for match in pattern.finditer(content):
                target = _clean_target(match.group(1))
                if len(target) < 3:
                    continue
                key = (entity, relation, target)
                if key not in seen:
                    seen.add(key)
                    facts.append(key)
    return facts


def render_facts_message(facts: list) -> dict | None:
    """Render stored fact rows as one injectable system message, or None.

    ``facts`` rows are ``(entity, relation, target)`` as returned by
    :meth:`MemoryStore.facts_for`. The message is marked so it is
    identifiable; callers must route it but never persist it (system
    messages are excluded from persistence already).
    """
    if not facts:
        return None
    lines = [
        "[Session memory — key facts from earlier in this conversation. "
        "Use them for continuity; they may be stale if contradicted lately.]"
    ]
    lines += [f"- {entity} {relation}: {target}" for entity, relation, target in facts]
    return {"role": "system", "content": "\n".join(lines)}


class MemoryStore:
    """Fact storage in the same SQLite database as the session history.

    Pass ``shared`` a initialized :class:`SessionStore` to reuse its
    connection (required for ``:memory:`` databases, where a second
    connection would be a different database); otherwise opens its own
    connection to ``db_path`` (or the same default path SessionStore uses).
    """

    def __init__(self, db_path: str = None, shared=None):
        self._shared = shared
        if db_path is None and shared is not None:
            db_path = getattr(shared, "db_path", None)
        if db_path is None and shared is None:
            from invincible.core.session_store import default_db_path

            db_path = default_db_path()
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        # Prefer the shared store's public accessor; the duck-typed guard
        # keeps connection-less test stubs on the standalone path below.
        accessor = getattr(self._shared, "connection", None) if self._shared else None
        shared_db = accessor() if callable(accessor) else None
        if shared_db is not None:
            self._db = shared_db
        elif self.db_path is not None:
            # Shared store without a live connection (test stubs) or a
            # standalone store: open our own connection. A stub with no
            # path (lifespan tests) leaves _db None - inert, so tests
            # never touch the real cwd database.
            self._shared = None
            self._db = await aiosqlite.connect(self.db_path)
        if self._db is None:
            return
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'default',
                session_id TEXT NOT NULL,
                entity TEXT NOT NULL,
                relation TEXT NOT NULL,
                target TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(user_id, session_id, entity, relation, target)
            )
            """
        )
        await self._db.commit()

    async def close(self):
        # A shared connection is owned (and closed) by the SessionStore.
        if self._shared is None and self._db is not None:
            await self._db.close()
        self._db = None

    async def record(self, session_id: str, messages: list) -> int:
        """Extract facts from new turns and store them; returns rows added.

        Idempotent: the UNIQUE constraint plus INSERT OR IGNORE means
        re-persisting a turn never duplicates facts.
        """
        facts = extract_facts(messages)
        added = 0
        for entity, relation, target in facts:
            cursor = await self._db.execute(
                """
                INSERT OR IGNORE INTO facts
                    (user_id, session_id, entity, relation, target, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (SENTINEL_USER_ID, session_id, entity, relation, target, time.time()),
            )
            added += cursor.rowcount
        if facts:
            await self._db.commit()
        return added

    async def facts_for(self, session_id: str, limit: int = None) -> list:
        """Most recent facts for a session as ``(entity, relation, target)``."""
        limit = limit if limit is not None else max_injected_facts()
        async with self._db.execute(
            """
            SELECT entity, relation, target FROM facts
            WHERE user_id = ? AND session_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (SENTINEL_USER_ID, session_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        rows.reverse()  # chronological order for the injected summary
        return rows


async def memory_system_message(memory, session_id: str) -> dict | None:
    """Build the injectable memory system message for a session, or None."""
    if memory is None or not memory_enabled():
        return None
    return render_facts_message(await memory.facts_for(session_id))


async def record_turns(memory, session_id: str, messages: list) -> None:
    """Extract and store facts from a request's new turns (best-effort)."""
    if memory is None or not memory_enabled():
        return
    await memory.record(session_id, messages)
