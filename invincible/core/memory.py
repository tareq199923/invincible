# invincible/core/memory.py
"""Session fact memory on PostgreSQL (Phase 16).

Regex-extracted ``(entity, relation, target)`` facts persisted to the
``facts`` table and injected as a bounded system message - identical
behavior to the SQLite era, now through SQLAlchemy Core over asyncpg.
"""
import re

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from invincible.core.db import facts
from invincible.core.settings import DEFAULT_MEMORY_MAX_FACTS, settings

SENTINEL_USER_ID = "default"
DEFAULT_MAX_FACTS = DEFAULT_MEMORY_MAX_FACTS
_TARGET_MAX_CHARS = 160


def scope_for_principal(principal) -> str:
    """Facts namespace for a Principal (Phase 2 isolation).

    Local-mode principals keep the sentinel ``default`` namespace so
    pre-Phase-2 rows stay reachable with zero behavior change; every
    other principal gets its own ``user:<id>`` namespace. The facts table
    itself is superseded by scoped ``memories`` after Phase 4.
    """
    if principal is None or getattr(principal, "is_local", False):
        return SENTINEL_USER_ID
    return f"user:{principal.user_id}"

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
    return settings.memory_enabled()


def max_injected_facts() -> int:
    return settings.memory_max_facts()


def _clean_target(raw: str) -> str:
    target = raw.splitlines()[0].split(". ")[0].strip().strip('"').rstrip(".")
    return target[:_TARGET_MAX_CHARS]


def extract_facts(messages: list) -> list[tuple[str, str, str]]:
    facts_out = []
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
                    facts_out.append(key)
    return facts_out


def render_facts_message(rows: list) -> dict | None:
    if not rows:
        return None
    lines = [
        "[Session memory — key facts from earlier in this conversation. "
        "Use them for continuity; they may be stale if contradicted lately.]"
    ]
    lines += [f"- {e} {rel}: {t}" for e, rel, t in rows]
    return {"role": "system", "content": "\n".join(lines)}


class MemoryStore:
    def __init__(self, engine):
        self.engine = engine

    async def init(self) -> None:
        """Schema owned by core.db metadata."""

    async def close(self) -> None:
        """Engine owned/disposed by the lifespan."""

    async def record(self, session_id: str, messages_list: list, *,
                     scope_user: str = SENTINEL_USER_ID) -> int:
        added = 0
        extracted = extract_facts(messages_list)
        if not extracted:
            return 0
        import time

        async with self.engine.begin() as conn:
            for entity, relation, target in extracted:
                result = await conn.execute(
                    pg_insert(facts)
                    .values(
                        user_id=scope_user,
                        session_id=session_id,
                        entity=entity,
                        relation=relation,
                        target=target,
                        created_at=time.time(),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            "user_id", "session_id",
                            "entity", "relation", "target",
                        ]
                    )
                )
                added += result.rowcount
        return added

    async def facts_for(
        self, session_id: str, limit: int | None = None, *,
        scope_user: str = SENTINEL_USER_ID,
    ) -> list[tuple[str, str, str]]:
        limit = limit if limit is not None else max_injected_facts()
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(
                    facts.c.entity, facts.c.relation, facts.c.target
                )
                .where(facts.c.user_id == scope_user,
                       facts.c.session_id == session_id)
                .order_by(facts.c.id.desc())
                .limit(limit)
            )).all()
        rows = list(reversed(rows))
        return [tuple(r) for r in rows]


async def wipe_session(engine, session_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            delete(facts).where(facts.c.session_id == session_id)
        )


# Convenience wrappers preserved from the SQLite era (endpoints import these).

async def memory_system_message(memory, session_id: str, *,
                                scope_user: str = SENTINEL_USER_ID) -> dict | None:
    """Build the injectable memory system message for a session, or None."""
    if memory is None or not memory_enabled():
        return None
    return render_facts_message(
        await memory.facts_for(session_id, scope_user=scope_user))


async def record_turns(memory, session_id: str, messages_list: list, *,
                       scope_user: str = SENTINEL_USER_ID) -> None:
    """Extract and store facts from a request's new turns (best-effort)."""
    if memory is None or not memory_enabled():
        return
    await memory.record(session_id, messages_list, scope_user=scope_user)
