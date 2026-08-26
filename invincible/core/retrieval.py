# invincible/core/retrieval.py
"""Lexical memory retrieval (Platform Phase 4).

Scores ``memories`` rows against the incoming user text so only a bounded,
relevant slice reaches the prompt (locked principle: maximize continuity
per token, never dump stores).

Score = full-text rank x recency x kind weight x confidence.

The SQL half uses the migration-0005 generated tsvector + GIN index for
the match path; ranking happens in Python (:func:`score_memory`) so the
weighting model stays hermetically testable and tunable without touching
the query. The regconfig constant is shared with the schema - a mismatch
would silently bypass the index.
"""
import re
import time
from dataclasses import dataclass

from sqlalchemy import text

from invincible.core.db import MEMORY_FTS_CONFIG
from invincible.core.settings import (
    settings,
)

# Exponential recency decay: a memory is worth half as much after this
# many seconds (14 days). Deliberately generous - facts go stale slower
# than chat turns.
RECENCY_HALF_LIFE_SECONDS = 14 * 24 * 3600

# Coarse relevance multipliers by memory kind. Tasks and decisions carry
# more continuity value than generic facts.
KIND_WEIGHTS = {
    "decision": 1.25,
    "task": 1.2,
    "preference": 1.1,
    "fact": 1.0,
}

# Candidate pool fetched from SQL before Python-side rescoring: several
# multiples of top_n so ordering changes from recency/confidence can pull
# lower-lexical-rank rows into the final cut.
_POOL_FACTOR = 8
_MAX_POOL = 64

# Query-shape fallback: websearch_to_tsquery ANDs every term, so a natural
# question ("how should I configure postgres pooling?") misses memories
# that share only some terms. Strategy: strict-AND first (precision); when
# it matches nothing, retry with OR of sanitized tokens (recall) and let
# scoring + the relevance floor keep precision. Stopwords/short tokens are
# dropped by the text search configuration itself.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,40}")


def or_query_text(query: str) -> str | None:
    """Build a ``to_tsquery`` OR expression from sanitized query tokens."""
    tokens = _TOKEN_RE.findall(query)
    if not tokens:
        return None
    return " | ".join(f"'{tok}'" for tok in tokens)


@dataclass(frozen=True)
class RetrievedMemory:
    id: int
    content: str
    kind: str
    layer: str
    confidence: float
    created_at: float
    score: float


def score_memory(
    *,
    ts_rank: float,
    age_seconds: float,
    kind: str,
    confidence: float,
) -> float:
    """Pure Phase 4 ranking function."""
    recency = 0.5 ** (max(age_seconds, 0.0) / RECENCY_HALF_LIFE_SECONDS)
    return (
        max(ts_rank, 0.0)
        * recency
        * KIND_WEIGHTS.get(kind, 1.0)
        * max(confidence, 0.0)
    )


class RetrievalService:
    def __init__(self, engine):
        self.engine = engine

    async def init(self) -> None:
        """Schema owned by core.db metadata."""

    async def close(self) -> None:
        """Engine owned/disposed by the lifespan."""

    async def retrieve(
        self,
        *,
        user_id: int,
        query: str,
        project_id: int | None = None,
        limit: int | None = None,
        floor: float | None = None,
        now: float | None = None,
    ) -> list[RetrievedMemory]:
        """Top-N memories lexically matching ``query`` above the floor.

        Scope predicate: the owner's user-scope rows plus rows belonging
        to ``project_id`` when given - never another user's, never other
        projects'. Empty query returns nothing (a blank message must not
        degenerate into latest-N injection).
        """
        if not query.strip():
            return []
        limit = limit if limit is not None else settings.memory_top_n()
        floor = floor if floor is not None else settings.memory_min_score()
        if limit <= 0:
            return []

        # Stemmer asymmetry under the shared 'english' regconfig: query
        # terms stem but stored forms may not — 'postgres' -> 'postgr'
        # never matches stored 'postgresql' (which stays whole), while
        # 'tuning'/'tune' match fine. Partial-term queries can therefore
        # silently miss. The tests use symmetric term pairs on purpose
        # (tests/test_phase4_schema.py, tests/test_retrieval.py); if
        # MEMORY_FTS_CONFIG or the text-search configuration ever changes,
        # re-verify those pairs first.
        and_sql = text(
            "SELECT id, content, kind, layer, confidence, created_at,"
            " ts_rank(search_vector,"
            "         websearch_to_tsquery(CAST(:cfg AS regconfig), :q))"
            "   AS rank"
            " FROM memories"
            " WHERE user_id = :uid"
            "   AND (project_id IS NULL OR project_id = :pid)"
            "   AND search_vector @@"
            "       websearch_to_tsquery(CAST(:cfg AS regconfig), :q)"
            " ORDER BY rank DESC"
            " LIMIT :pool"
        )
        or_expr = or_query_text(query)
        or_sql = text(
            "SELECT id, content, kind, layer, confidence, created_at,"
            " ts_rank(search_vector,"
            "         to_tsquery(CAST(:cfg AS regconfig), :q)) AS rank"
            " FROM memories"
            " WHERE user_id = :uid"
            "   AND (project_id IS NULL OR project_id = :pid)"
            "   AND search_vector @@ to_tsquery(CAST(:cfg AS regconfig), :q)"
            " ORDER BY rank DESC"
            " LIMIT :pool"
        )
        params = {
            "cfg": MEMORY_FTS_CONFIG,
            "q": query,
            "uid": user_id,
            "pid": project_id,
            "pool": min(limit * _POOL_FACTOR, _MAX_POOL),
        }
        now = time.time() if now is None else now

        def _to_memories(rows) -> list[RetrievedMemory]:
            scored = [
                RetrievedMemory(
                    id=r["id"],
                    content=r["content"],
                    kind=r["kind"],
                    layer=r["layer"],
                    confidence=float(r["confidence"]),
                    created_at=float(r["created_at"]),
                    score=score_memory(
                        ts_rank=float(r["rank"]),
                        age_seconds=max(now - float(r["created_at"]), 0.0),
                        kind=r["kind"],
                        confidence=float(r["confidence"]),
                    ),
                )
                for r in rows
            ]
            scored.sort(key=lambda m: m.score, reverse=True)
            return [m for m in scored if m.score >= floor][:limit]

        async with self.engine.connect() as conn:
            rows = (await conn.execute(and_sql, params)).mappings().all()
            if not rows and or_expr:
                rows = (await conn.execute(
                    or_sql, {**params, "q": or_expr}
                )).mappings().all()
        return _to_memories(rows)
