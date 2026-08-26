# invincible/core/memory.py
"""Scoped user memory on PostgreSQL (Platform Phase 4).

Two write sources feed the scoped ``memories`` table: deterministic regex
extraction (the Phase 10/16 pattern set, retargeted from the legacy per-
session ``facts`` triples) and explicit \"remember this\" / \"save this\"
triggers. Retrieval and injection live in ``core/retrieval.py`` and
``core/context_builder.py``.

The ``facts`` table itself is inert history as of Phase 4: nothing reads
or writes it in service code (only the legacy importer fills it).
"""
import re
import time

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from invincible.core.db import MEMORY_FTS_CONFIG, memories
from invincible.core.settings import settings

_TARGET_MAX_CHARS = 160
# Confidence for deterministic auto-extracted rows; explicit "remember
# this" saves are user-asserted and land at 1.0.
AUTO_CONFIDENCE = 0.6

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


# Explicit-save triggers (Phase 4): the user deliberately asks for
# persistence. Matched against USER messages only - an assistant echoing
# "remember that..." must never mint a memory on its own.
_EXPLICIT_PATTERNS = [
    re.compile(r"\bremember\s+(?:that|this)\s*[:,-]?\s+(.+)", re.I),
    re.compile(r"\bsave\s+(?:this|that)\s*[:,-]?\s+(.+)", re.I),
]


def extract_explicit(messages: list) -> list[str]:
    """Verbatim-ish payloads of deliberate save requests, deduplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        for pattern in _EXPLICIT_PATTERNS:
            for match in pattern.finditer(content):
                target = _clean_target(match.group(1))
                if len(target) < 3 or target in seen:
                    continue
                seen.add(target)
                out.append(target)
    return out


# Triple relation -> memory kind (the memories table's coarse classifier).
_KIND_BY_RELATION = {
    "name": "fact",
    "note": "fact",
    "preference": "preference",
    "toolchain": "fact",
    "decision": "decision",
    "current_task": "task",
    "next_step": "task",
}


def memory_row_from_fact(
        entity: str, relation: str, target: str) -> tuple[str, str]:
    """Render one extracted triple as a ``(kind, content)`` memories row.

    Content keeps the terse ``relation: target`` shape - keyword-complete
    for lexical retrieval, no natural-language generation to get wrong.
    Chat-derived rows are always user-scope (project_id NULL): they follow
    the person across projects; provenance records where they came from.
    """
    del entity  # user/project distinction is moot under user-scope storage
    return _KIND_BY_RELATION.get(relation, "fact"), f"{relation}: {target}"


class MemoryStore:
    def __init__(self, engine):
        self.engine = engine

    async def init(self) -> None:
        """Schema owned by core.db metadata."""

    async def close(self) -> None:
        """Engine owned/disposed by the lifespan."""

    # ------------------------------------------------------------------
    # Scoped memories (Phase 4): user/project-scoped rows replacing the
    # per-session facts triple store. Retrieval lives in RetrievalService;
    # this store only writes and deduplicates.

    async def save_memory(
        self,
        *,
        user_id: int,
        content: str,
        layer: str = "auto",
        kind: str = "fact",
        confidence: float = 1.0,
        provenance: str | None = None,
        project_id: int | None = None,
    ) -> int | None:
        """Insert one memory row; returns its id, or None when skipped.

        Scope follows the ownership shape: a project_id makes it
        project-scope, otherwise user-scope. No unique constraint exists on
        content, so callers that need idempotency use ``record_memories``
        (which batch-checks before insert).
        """
        if layer not in ("explicit", "auto"):
            raise ValueError("layer must be 'explicit' or 'auto'")
        async with self.engine.begin() as conn:
            result = await conn.execute(
                pg_insert(memories).values(
                    user_id=user_id,
                    project_id=project_id,
                    scope="project" if project_id is not None else "user",
                    layer=layer,
                    kind=kind,
                    content=content,
                    confidence=confidence,
                    provenance=provenance,
                    created_at=time.time(),
                )
            )
            return result.inserted_primary_key[0] if result.rowcount else None

    async def record_memories(
        self,
        *,
        user_id: int,
        client_session_id: str,
        messages_list: list,
        explicit_enabled: bool | None = None,
    ) -> int:
        """Extract memories from a request's new turns and persist them.

        Two sources per the Phase 4 design: deterministic auto-extraction
        (regex patterns, confidence 0.6) over every message, and explicit
        \"remember this\" / \"save this\" triggers (confidence 1.0) from
        user messages only. Rows are user-scope; provenance records the
        originating client session. Returns how many NEW rows landed.

        ``INVINCIBLE_MEMORY`` is the master kill-switch for both sources;
        ``INVINCIBLE_MEMORY_EXPLICIT`` silences only the explicit triggers.
        """
        if not settings.memory_enabled():
            return 0
        if explicit_enabled is None:
            explicit_enabled = settings.memory_explicit_enabled()
        # Explicit wins: a phrase the user deliberately saved must not ALSO
        # land as an auto-extracted note (the legacy remember-that pattern
        # would otherwise double-capture the same words).
        explicit_targets = extract_explicit(messages_list) \
            if explicit_enabled else []
        explicit_set = set(explicit_targets)
        rows: list[dict] = []
        for relation, target in sorted({
            r: t
            for _, r, t in extract_facts(messages_list)
            if t not in explicit_set
        }.items()):
            kind, content = memory_row_from_fact("user", relation, target)
            rows.append({
                "layer": "auto", "kind": kind, "content": content,
                "confidence": AUTO_CONFIDENCE,
            })
        for target in explicit_targets:
            rows.append({
                "layer": "explicit", "kind": "note",
                "content": target, "confidence": 1.0,
            })
        if not rows:
            return 0

        provenance = f"chat:{client_session_id}"
        async with self.engine.begin() as conn:
            # Check-before-insert dedup: no unique constraint backs memory
            # content, so idempotency comes from this batch-aware lookup.
            existing = {
                (layer, content)
                for layer, content in (await conn.execute(
                    select(memories.c.layer, memories.c.content).where(
                        memories.c.user_id == user_id,
                        memories.c.project_id.is_(None),
                        memories.c.content.in_(
                            [r["content"] for r in rows]
                        ),
                    )
                )).all()
            }
            added = 0
            for row in rows:
                key = (row["layer"], row["content"])
                if key in existing:
                    continue
                await conn.execute(
                    pg_insert(memories).values(
                        user_id=user_id,
                        project_id=None,
                        scope="user",
                        layer=row["layer"],
                        kind=row["kind"],
                        content=row["content"],
                        confidence=row["confidence"],
                        provenance=provenance,
                        created_at=time.time(),
                    )
                )
                existing.add(key)
                added += 1
        return added

    # ------------------------------------------------------------------
    # User-facing management reads/writes (Phase 5 dashboard). Every
    # method takes a MANDATORY user_id - no local-owner fallback exists
    # on any of these paths.

    @staticmethod
    def _owner_filter(user_id: int, *, layer: str | None, kind: str | None):
        """Shared WHERE clauses for browse/count paths."""
        clauses = [memories.c.user_id == user_id]
        if layer is not None:
            if layer not in ("explicit", "auto"):
                raise ValueError("layer must be 'explicit' or 'auto'")
            clauses.append(memories.c.layer == layer)
        if kind is not None:
            clauses.append(memories.c.kind == kind)
        return clauses

    async def list_for_user(
        self, user_id: int, *, layer: str | None = None,
        kind: str | None = None, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        """Newest-first memory rows for one owner (dashboard browse)."""
        query = (
            select(
                memories.c.id,
                memories.c.scope,
                memories.c.layer,
                memories.c.kind,
                memories.c.content,
                memories.c.confidence,
                memories.c.provenance,
                memories.c.created_at,
            )
            .where(*self._owner_filter(user_id, layer=layer, kind=kind))
            .order_by(memories.c.created_at.desc(), memories.c.id.desc())
            .limit(max(0, limit))
            .offset(max(0, offset))
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [dict(r) for r in rows]

    async def count_for_user(
        self, user_id: int, *, layer: str | None = None,
        kind: str | None = None,
    ) -> int:
        """Exact row count for one owner, honoring the same filters."""
        query = (
            select(func.count())
            .select_from(memories)
            .where(*self._owner_filter(user_id, layer=layer, kind=kind))
        )
        async with self.engine.connect() as conn:
            return int((await conn.execute(query)).scalar_one())

    async def search_for_user(
        self, user_id: int, query: str, *, layer: str | None = None,
        kind: str | None = None, limit: int = 50,
    ) -> list[dict]:
        """Lexical search scoped to ONE owner (dashboard search box).

        Same query-shape strategy as RetrievalService: strict-AND via
        ``websearch_to_tsquery`` first; when nothing matches, OR of the
        sanitized tokens via ``to_tsquery``. Rank-ordered newest-first on
        ties; each row carries its ts_rank under ``"rank"``.
        """
        if not query.strip():
            return []
        filter_sql = ""
        params: dict = {
            "cfg": MEMORY_FTS_CONFIG,
            "q": query,
            "uid": user_id,
            "limit": max(1, limit),
        }
        if layer is not None:
            if layer not in ("explicit", "auto"):
                raise ValueError("layer must be 'explicit' or 'auto'")
            filter_sql += " AND layer = :layer"
            params["layer"] = layer
        if kind is not None:
            filter_sql += " AND kind = :kind"
            params["kind"] = kind

        and_sql = text(
            "SELECT id, scope, layer, kind, content, confidence,"
            " provenance, created_at,"
            " ts_rank(search_vector,"
            "         websearch_to_tsquery(CAST(:cfg AS regconfig), :q))"
            "   AS rank"
            " FROM memories"
            " WHERE user_id = :uid"
            "   AND search_vector @@"
            "       websearch_to_tsquery(CAST(:cfg AS regconfig), :q)"
            + filter_sql +
            " ORDER BY rank DESC, created_at DESC, id DESC"
            " LIMIT :limit"
        )
        tokens = re.findall(r"[A-Za-z0-9]{3,40}", query)
        or_expr = " | ".join(f"'{tok}'" for tok in tokens) or None
        or_sql = text(
            "SELECT id, scope, layer, kind, content, confidence,"
            " provenance, created_at,"
            " ts_rank(search_vector,"
            "         to_tsquery(CAST(:cfg AS regconfig), :q)) AS rank"
            " FROM memories"
            " WHERE user_id = :uid"
            "   AND search_vector @@ to_tsquery(CAST(:cfg AS regconfig), :q)"
            + filter_sql +
            " ORDER BY rank DESC, created_at DESC, id DESC"
            " LIMIT :limit"
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(and_sql, params)).mappings().all()
            if not rows and or_expr is not None:
                rows = (await conn.execute(
                    or_sql, {**params, "q": or_expr})).mappings().all()
        return [dict(r) for r in rows]

    async def delete(self, memory_id: int, *, user_id: int) -> bool:
        """Delete THIS owner's row by id. False when unknown OR foreign -
        callers cannot distinguish the two (anti-enumeration)."""
        async with self.engine.begin() as conn:
            result = await conn.execute(
                delete(memories).where(
                    memories.c.id == memory_id,
                    memories.c.user_id == user_id,
                )
            )
        return bool(result.rowcount)
