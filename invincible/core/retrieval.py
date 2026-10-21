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

Query text is tokenized here rather than handed to ``websearch_to_tsquery``:
chat messages are not search syntax (``or``, a leading ``-``, quotes), and
the default parser is ASCII-strict. Tokens become quoted prefix terms so
``postgres`` also finds a stored ``PostgreSQL``.
"""
import re
import time
import unicodedata
from dataclasses import dataclass

from sqlalchemy import text

from invincible.core.db import MEMORY_FTS_CONFIG
from invincible.core.settings import (
    settings,
)

_DAY_SECONDS = 24 * 3600

# Default exponential recency decay: a memory is worth half as much after
# this many seconds (14 days). Kind-specific overrides below: durable
# preferences and decisions should not fade at chat-turn speed.
RECENCY_HALF_LIFE_SECONDS = 14 * _DAY_SECONDS

RECENCY_HALF_LIFE_BY_KIND = {
    "task": 14 * _DAY_SECONDS,
    "fact": 45 * _DAY_SECONDS,
    "decision": 60 * _DAY_SECONDS,
    "preference": 90 * _DAY_SECONDS,
}


def half_life_for(kind: str) -> float:
    """Recency half-life in seconds for ``kind`` (14-day default)."""
    return RECENCY_HALF_LIFE_BY_KIND.get(kind, RECENCY_HALF_LIFE_SECONDS)


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


# Retrieval-side query tokenization. The dashboard search keeps
# ``or_query_text`` above; this path must handle chat text (not search
# syntax), cap pasted logs, and preserve non-Latin scripts.
_MAX_QUERY_CHARS = 1000
_MAX_QUERY_TOKENS = 12
_MIN_TOKEN_CHARS = 2
_MAX_TOKEN_CHARS = 40
_PREFIX_MIN_CHARS = 4
_MARK_CATEGORIES = frozenset({"Mn", "Mc", "Me"})
_JOINERS = frozenset({"\u200c", "\u200d"})


def _is_word_char(ch: str) -> bool:
    """True for letters/digits, combining marks, and ZWJ/ZWNJ."""
    return (
        ch.isalnum()
        or ch in _JOINERS
        or unicodedata.category(ch) in _MARK_CATEGORIES
    )


def tokenize_query(query: str) -> list[str]:
    """Split chat text into safe, bounded tsquery terms.

    Unicode-aware so Bangla (and other non-Latin scripts) survive: vowel
    signs and the virama are combining marks, not separators. Tokens are
    lower-cased and de-duplicated in first-seen order.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    current: list[str] = []

    def flush() -> None:
        token = "".join(current).lower()
        current.clear()
        if not token:
            return
        if (
            _MIN_TOKEN_CHARS <= len(token) <= _MAX_TOKEN_CHARS
            and any(ch.isalnum() for ch in token)
            and token not in seen
        ):
            seen.add(token)
            tokens.append(token)

    for ch in query[:_MAX_QUERY_CHARS]:
        if _is_word_char(ch):
            current.append(ch)
        else:
            flush()
        if len(tokens) >= _MAX_QUERY_TOKENS:
            return tokens
    flush()
    return tokens[:_MAX_QUERY_TOKENS]


def prefix_tsquery(tokens: list[str], operator: str) -> str | None:
    """Join tokens as quoted tsquery terms (prefix when long enough).

    ``operator`` is ``"&"`` (strict) or ``"|"`` (recall fallback). Tokens
    are already restricted to letters/digits/marks/joiners, so quoting is
    sufficient - no tsquery metacharacter can survive tokenization.
    """
    if not tokens:
        return None
    parts = [
        f"'{token}':*" if len(token) >= _PREFIX_MIN_CHARS else f"'{token}'"
        for token in tokens
    ]
    return f" {operator} ".join(parts)


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
    half_life = half_life_for(kind)
    recency = 0.5 ** (max(age_seconds, 0.0) / half_life)
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

        tokens = tokenize_query(query)
        if not tokens:
            return []

        # Prefix terms bridge the stemmer asymmetry under the shared
        # 'english' regconfig: 'postgres':* stems to 'postgr':* and finds a
        # stored 'postgresql' (which stays whole), while exact terms still
        # pair symmetrically. Tests that assert exact behavior keep using
        # symmetric term pairs (tests/test_phase4_schema.py,
        # tests/test_retrieval.py); if MEMORY_FTS_CONFIG or the text-search
        # configuration ever changes, re-verify those pairs first.
        sql = text(
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
        and_expr = prefix_tsquery(tokens, "&")
        or_expr = prefix_tsquery(tokens, "|")
        params = {
            "cfg": MEMORY_FTS_CONFIG,
            "q": and_expr,
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
            rows = (await conn.execute(sql, params)).mappings().all()
            if not rows and or_expr != and_expr:
                rows = (await conn.execute(
                    sql, {**params, "q": or_expr}
                )).mappings().all()
        return _to_memories(rows)
