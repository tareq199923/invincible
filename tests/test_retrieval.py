# tests/test_retrieval.py
"""Phase 4 RetrievalService: lexical match x recency x kind x confidence.

The scoring function is pure (hermetic tests); the SQL path runs against
real Postgres and exercises the migration-0005 tsvector/GIN artifacts,
scope predicates, and the relevance floor.
"""
import time
import unicodedata

import pytest
from sqlalchemy import text

from invincible.core.db import ensure_local_owner
from invincible.core.memory import MemoryStore
from invincible.core.retrieval import (
    RECENCY_HALF_LIFE_SECONDS,
    RetrievalService,
    half_life_for,
    prefix_tsquery,
    score_memory,
    tokenize_query,
)


@pytest.fixture
async def retrieval(pg_engine):
    yield RetrievalService(engine=pg_engine)


@pytest.fixture
async def owner_ids(pg_engine):
    return await ensure_local_owner(pg_engine)


async def _backdate(pg_engine, memory_id: str, age_seconds: float):
    async with pg_engine.begin() as conn:
        await conn.execute(
            text("UPDATE memories SET created_at = :at WHERE id = :i"),
            {"at": time.time() - age_seconds, "i": memory_id},
        )


# --- pure scoring --------------------------------------------------------------


def test_recency_decay_prefers_newer_rows():
    fresh = score_memory(ts_rank=0.1, age_seconds=0, kind="fact",
                         confidence=1.0)
    stale = score_memory(
        ts_rank=0.1, age_seconds=half_life_for("fact") * 4,
        kind="fact", confidence=1.0,
    )
    assert fresh > stale
    # Four half-lives out the weight is 1/16th.
    assert stale == pytest.approx(fresh / 16)


def test_confidence_and_kind_scale_the_score():
    base = score_memory(ts_rank=0.1, age_seconds=0, kind="fact",
                        confidence=1.0)
    assert score_memory(ts_rank=0.1, age_seconds=0, kind="fact",
                        confidence=0.5) == pytest.approx(base * 0.5)
    assert score_memory(ts_rank=0.1, age_seconds=0, kind="decision",
                        confidence=1.0) > base
    assert score_memory(ts_rank=0.1, age_seconds=0, kind="mystery-kind",
                        confidence=1.0) == pytest.approx(base)


def test_score_never_negative():
    assert score_memory(ts_rank=-5, age_seconds=-5, kind="fact",
                        confidence=-1) == 0.0


# --- per-kind recency ----------------------------------------------------------


def test_half_life_for_defaults_and_kind_ordering():
    assert half_life_for("task") == RECENCY_HALF_LIFE_SECONDS
    assert half_life_for("mystery-kind") == RECENCY_HALF_LIFE_SECONDS
    assert (
        half_life_for("preference")
        > half_life_for("decision")
        > half_life_for("fact")
        > half_life_for("task")
    )


def test_preference_scores_more_than_four_tasks_at_40_days():
    preference = score_memory(
        ts_rank=0.06, age_seconds=40 * 24 * 3600,
        kind="preference", confidence=0.9,
    )
    task = score_memory(
        ts_rank=0.06, age_seconds=40 * 24 * 3600,
        kind="task", confidence=0.9,
    )
    assert preference > task * 4


# --- query tokenizer -----------------------------------------------------------


def test_tokenize_query_lowercases_dedupes_and_drops_short():
    assert tokenize_query(
        "Postgres postgres POOLING, pooling! a"
    ) == ["postgres", "pooling"]


def test_tokenize_query_keeps_two_char_tech_terms():
    assert tokenize_query("db & go?") == ["db", "go"]
    assert tokenize_query("  ?! ") == []


def test_tokenize_query_caps_input_and_token_count():
    words = " ".join(f"word{i:03d}" for i in range(200))
    tokens = tokenize_query(words)
    assert len(tokens) == 12
    assert tokens[0] == "word000"
    assert tokenize_query("x" * 1000 + " lateword") == []


def test_tokenize_query_keeps_combining_marks_and_splits_danda():
    assert tokenize_query(
        "আমার পছন্দের ডাটাবেস কোনটা।"
    ) == ["আমার", "পছন্দের", "ডাটাবেস", "কোনটা"]


def test_tokenize_query_strips_tsquery_syntax():
    tokens = tokenize_query("o'neil \\ ' : & | ! ( ) <->")
    assert tokens == ["neil"]
    for token in tokens:
        assert all(
            ch.isalnum()
            or unicodedata.category(ch) in {"Mn", "Mc", "Me"}
            for ch in token
        )
    expression = prefix_tsquery(tokens, "&")
    assert expression == "'neil':*"
    assert expression.count("'") == 2


def test_prefix_tsquery_shapes():
    assert prefix_tsquery(["db", "postgres"], "&") == "'db' & 'postgres':*"
    assert prefix_tsquery(["go", "tune"], "|") == "'go' | 'tune':*"
    assert prefix_tsquery([], "&") is None


# --- SQL path -------------------------------------------------------------------


async def test_relevant_outranks_irrelevant(pg_engine, owner_ids, retrieval):
    uid, _pid = owner_ids
    # Both rows match BOTH query terms, so lexical rank is comparable and
    # confidence + freshness decide the ordering (the acceptance core).
    store = MemoryStore(engine=pg_engine)
    weak = await store.save_memory(
        user_id=uid, content="postgres pooling scripts", layer="auto",
        kind="fact", confidence=0.6, provenance="t")
    strong = await store.save_memory(
        user_id=uid, content="postgres connection pooling setup",
        layer="explicit", kind="note", confidence=1.0, provenance="t")
    await _backdate(pg_engine, weak, half_life_for("fact") * 4)

    # With the floor disabled, both surface and scoring separates them
    # decisively (explicit + fresh vs auto + four half-lives stale).
    hits = await retrieval.retrieve(
        user_id=uid, query="postgres pooling", floor=0.0)
    assert [h.id for h in hits] == [strong, weak]
    assert hits[0].score > hits[1].score * 4

    # At the default relevance floor the stale low-confidence row is
    # correctly dropped entirely - it must not be injected.
    assert [h.id for h in await retrieval.retrieve(
        user_id=uid, query="postgres pooling")] == [strong]


async def test_scope_isolation_user_and_project(
    pg_engine, owner_ids, retrieval
):
    uid_a, pid_a = owner_ids
    async with pg_engine.begin() as conn:
        from invincible.core.db import projects, users

        uid_b = (await conn.execute(
            users.insert().values(email="r@example.com",
                                  created_at=time.time())
        )).inserted_primary_key[0]
        pid_b = (await conn.execute(
            projects.insert().values(user_id=uid_b, name="other",
                                     is_default=True,
                                     created_at=time.time())
        )).inserted_primary_key[0]

    store = MemoryStore(engine=pg_engine)
    await store.save_memory(user_id=uid_a, content="kubernetes ingress tips",
                            provenance="t")
    await store.save_memory(user_id=uid_b, content="kubernetes secrets rotation",
                            provenance="t")
    mine_only = await retrieval.retrieve(
        user_id=uid_a, query="kubernetes")
    assert [m.content for m in mine_only] == ["kubernetes ingress tips"]

    # Project-scoped row visible in its own project...
    await store.save_memory(user_id=uid_a, content="kubernetes helm charts",
                            provenance="t", project_id=pid_a)
    both = await retrieval.retrieve(
        user_id=uid_a, project_id=pid_a, query="kubernetes")
    assert {m.content for m in both} == {
        "kubernetes ingress tips", "kubernetes helm charts"}

    # ...but foreign projects' scoped rows never appear, and another
    # user's rows are unreachable no matter what project id is passed.
    def contents(hits):
        return [m.content for m in hits]

    assert contents(await retrieval.retrieve(
        user_id=uid_a, project_id=pid_b, query="kubernetes"
    )) == ["kubernetes ingress tips"]  # own user-scope row only
    assert contents(await retrieval.retrieve(
        user_id=uid_b, project_id=pid_b, query="kubernetes"
    )) == ["kubernetes secrets rotation"]  # B never sees A's rows
    assert contents(await retrieval.retrieve(
        user_id=uid_b, query="helm charts")) == []


async def test_floor_and_limit_bound_results(
    pg_engine, owner_ids, retrieval, monkeypatch
):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    for i in range(12):
        await store.save_memory(
            user_id=uid, content=f"kubernetes networking note {i}",
            layer="explicit", kind="note", confidence=1.0,
            provenance="t")
    hits = await retrieval.retrieve(user_id=uid, query="kubernetes networking")
    assert 0 < len(hits) <= 8

    monkeypatch.setenv("INVINCIBLE_MEMORY_TOP_N", "3")
    hits = await retrieval.retrieve(user_id=uid, query="kubernetes networking")
    assert len(hits) <= 3

    monkeypatch.setenv("INVINCIBLE_MEMORY_MIN_SCORE", "999")
    assert await retrieval.retrieve(
        user_id=uid, query="kubernetes networking") == []


async def test_prefix_query_finds_stored_postgresql(
    pg_engine, owner_ids, retrieval
):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    await store.save_memory(
        user_id=uid, content="toolchain: PostgreSQL", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )
    hits = await retrieval.retrieve(
        user_id=uid, query="how do I tune postgres?")
    assert [m.content for m in hits] == ["toolchain: PostgreSQL"]


async def test_preference_survives_default_floor_at_40_days(
    pg_engine, owner_ids, retrieval
):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    memory_id = await store.save_memory(
        user_id=uid, content="dark mode dashboard theme", layer="explicit",
        kind="preference", confidence=0.9, provenance="t",
    )
    await _backdate(pg_engine, memory_id, 40 * 24 * 3600)
    hits = await retrieval.retrieve(user_id=uid, query="dashboard theme")
    assert [m.id for m in hits] == [memory_id]


async def test_pasted_query_is_capped_and_still_matches(
    pg_engine, owner_ids, retrieval
):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    await store.save_memory(
        user_id=uid, content="kubernetes ingress tips", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )
    hits = await retrieval.retrieve(
        user_id=uid,
        query="kubernetes " + "traceback error line " * 500,
    )
    assert [m.content for m in hits] == ["kubernetes ingress tips"]


async def test_stopword_only_query_returns_nothing(
    pg_engine, owner_ids, retrieval
):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    await store.save_memory(
        user_id=uid, content="how do you do deployment notes",
        provenance="t")
    assert await retrieval.retrieve(
        user_id=uid, query="how do you do") == []


async def test_blank_query_retrieves_nothing(owner_ids, retrieval):
    uid, _pid = owner_ids
    assert await retrieval.retrieve(user_id=uid, query="   ") == []


async def test_unmatched_terms_return_empty(pg_engine, owner_ids, retrieval):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    await store.save_memory(user_id=uid, content="rust borrow checker",
                            provenance="t")
    assert await retrieval.retrieve(
        user_id=uid, query="quantum chromodynamics") == []
