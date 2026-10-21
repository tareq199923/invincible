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
    FUZZY_TERM_BASE_RANK,
    RECENCY_HALF_LIFE_SECONDS,
    RetrievalService,
    _build_fuzzy_sql,
    fuzzy_tokens,
    half_life_for,
    lexical_relevance,
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


async def _backdate_many(pg_engine, memory_ids, age_seconds: float):
    """Set several rows to the exact same timestamp for fair comparisons."""
    at = time.time() - age_seconds
    async with pg_engine.begin() as conn:
        for memory_id in memory_ids:
            await conn.execute(
                text("UPDATE memories SET created_at = :at WHERE id = :i"),
                {"at": at, "i": memory_id},
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


# --- fuzzy scoring and query building -----------------------------------------


def test_fuzzy_tokens_first_six_long_tokens():
    tokens = [
        "a", "postgres", "db", "about", "kubernetes",
        "deployment", "gateway", "routing", "ignored",
    ]
    assert fuzzy_tokens(tokens) == [
        "postgres", "about", "kubernetes", "deployment", "gateway",
        "routing",
    ]
    assert fuzzy_tokens(["db", "go", "sql"]) == []


def test_lexical_relevance_adds_capped_bonus():
    assert lexical_relevance(0.0, 0.0) == 0.0
    assert lexical_relevance(0.1, 0.0) == pytest.approx(0.1)
    assert lexical_relevance(0.0, 0.8) == pytest.approx(
        FUZZY_TERM_BASE_RANK * 0.8)
    assert lexical_relevance(0.0, 1.0) == pytest.approx(
        FUZZY_TERM_BASE_RANK)
    # The cap matters when several fuzzy tokens each look strong.
    assert lexical_relevance(0.0, 2.5) == pytest.approx(
        FUZZY_TERM_BASE_RANK)
    assert lexical_relevance(0.0, -0.5) == 0.0


def test_score_memory_keeps_stage1_formula_when_fuzzy_sum_is_zero():
    without = score_memory(
        ts_rank=0.06, age_seconds=0, kind="fact", confidence=1.0,
    )
    explicit_zero = score_memory(
        ts_rank=0.06, age_seconds=0, kind="fact", confidence=1.0,
        fuzzy_sum=0.0,
    )
    assert without == explicit_zero
    boosted = score_memory(
        ts_rank=0.06, age_seconds=0, kind="fact", confidence=1.0,
        fuzzy_sum=0.8,
    )
    assert boosted == pytest.approx(
        without + FUZZY_TERM_BASE_RANK * 0.8)


def test_fuzzy_sql_is_bounded_and_uses_only_bound_tokens():
    query = " ".join(f"token{i:03d}" for i in range(200))
    tokens = tokenize_query(query)
    fuzzy = fuzzy_tokens(tokens)
    assert len(fuzzy) == 6

    sql, params = _build_fuzzy_sql(fuzzy)
    rendered = str(sql)

    assert rendered.count("<%") == 6
    assert params == {
        "t0": fuzzy[0], "p0": "'token000':*",
        "t1": fuzzy[1], "p1": "'token001':*",
        "t2": fuzzy[2], "p2": "'token002':*",
        "t3": fuzzy[3], "p3": "'token003':*",
        "t4": fuzzy[4], "p4": "'token004':*",
        "t5": fuzzy[5], "p5": "'token005':*",
    }
    for token in fuzzy:
        assert token not in rendered

    with pytest.raises(ValueError):
        _build_fuzzy_sql([])


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


# --- pg_trgm fuzzy fallback ---------------------------------------------------


async def test_single_typo_reaches_or_stage(pg_engine, owner_ids, retrieval):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    memory_id = await store.save_memory(
        user_id=uid, content="toolchain: PostgreSQL", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )
    # AND finds nothing and or_expr == and_expr here; the fuzzy token must
    # be what makes the fallback run.
    hits = await retrieval.retrieve(user_id=uid, query="postgress")
    assert [m.id for m in hits] == [memory_id]


async def test_fuzzy_and_exact_hits_return_together(
    pg_engine, owner_ids, retrieval
):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    fuzzy_id = await store.save_memory(
        user_id=uid, content="toolchain: PostgreSQL", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )
    exact_id = await store.save_memory(
        user_id=uid, content="task: pooling bug", layer="explicit",
        kind="task", confidence=1.0, provenance="t",
    )
    await store.save_memory(
        user_id=uid, content="rust borrow checker", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )

    hits = await retrieval.retrieve(
        user_id=uid, query="postgress pooling", floor=0.0)

    assert {m.id for m in hits} == {fuzzy_id, exact_id}
    assert all(m.content != "rust borrow checker" for m in hits)


async def test_exact_outranks_fuzzy_only(pg_engine, owner_ids, retrieval):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    exact_id = await store.save_memory(
        user_id=uid, content="postgress deployment note", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )
    fuzzy_id = await store.save_memory(
        user_id=uid, content="PostgreSQL deployment note", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )
    await _backdate_many(pg_engine, [exact_id, fuzzy_id], 0)

    hits = await retrieval.retrieve(
        user_id=uid, query="postgress pooling", floor=0.0, limit=10)

    assert [m.id for m in hits] == [exact_id, fuzzy_id]
    assert hits[0].score > hits[1].score


async def test_exact_token_has_no_fuzzy_bonus(
    pg_engine, owner_ids, retrieval
):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    memory_id = await store.save_memory(
        user_id=uid, content="PostgreSQL", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )
    now = time.time()

    hits = await retrieval.retrieve(
        user_id=uid, query="postgres postgress", floor=0.0, now=now)
    assert len(hits) == 1

    async with pg_engine.connect() as conn:
        rank = (await conn.execute(
            text(
                "SELECT ts_rank(search_vector,"
                " to_tsquery(CAST(:cfg AS regconfig), :q))"
                " FROM memories WHERE id = :i"
            ),
            {
                "cfg": "english",
                "q": "'postgres':* | 'postgress':*",
                "i": memory_id,
            },
        )).scalar_one()
    expected = score_memory(
        ts_rank=float(rank),
        fuzzy_sum=0.8,  # postgress only; postgres is an exact match
        age_seconds=max(now - hits[0].created_at, 0.0),
        kind="fact",
        confidence=1.0,
    )
    # If the exact "postgres" similarity (0.889) were also added, the capped
    # sum would be 1.0 and this score would be materially higher.
    assert hits[0].score == pytest.approx(expected, rel=1e-5)


async def test_stopword_token_does_not_enter_fuzzy_pool(
    pg_engine, owner_ids, retrieval
):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    await store.save_memory(
        user_id=uid, content="this is about the dashboard", layer="auto",
        kind="fact", confidence=0.6, provenance="t",
    )
    await store.save_memory(
        user_id=uid, content="toolchain: PostgreSQL", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )

    # "about" has no tsquery lexeme (stopword) but word_similarity() is 1.0
    # against the dashboard row. The non-empty-tsquery guard must keep it
    # out of the fuzzy candidate pool.
    hits = await retrieval.retrieve(
        user_id=uid, query="about postgress", floor=0.0)
    assert [m.content for m in hits] == ["toolchain: PostgreSQL"]


async def test_fuzzy_scope_isolation_user_and_project(
    pg_engine, owner_ids, retrieval
):
    uid_a, pid_a = owner_ids
    async with pg_engine.begin() as conn:
        from invincible.core.db import projects, users

        uid_b = (await conn.execute(
            users.insert().values(email="fuzzy-b@example.com",
                                  created_at=time.time())
        )).inserted_primary_key[0]
        pid_b = (await conn.execute(
            projects.insert().values(user_id=uid_b, name="fuzzy-other",
                                     is_default=True,
                                     created_at=time.time())
        )).inserted_primary_key[0]

    store = MemoryStore(engine=pg_engine)
    await store.save_memory(
        user_id=uid_a, content="toolchain: PostgreSQL",
        layer="explicit", kind="fact", confidence=1.0, provenance="t",
    )
    await store.save_memory(
        user_id=uid_a, content="toolchain: PostgreSQL project",
        project_id=pid_a, layer="explicit", kind="fact",
        confidence=1.0, provenance="t",
    )
    await store.save_memory(
        user_id=uid_b, content="toolchain: PostgreSQL other",
        layer="explicit", kind="fact", confidence=1.0, provenance="t",
    )

    def contents(hits):
        return {m.content for m in hits}

    assert contents(await retrieval.retrieve(
        user_id=uid_a, query="postgress", floor=0.0
    )) == {"toolchain: PostgreSQL"}
    assert contents(await retrieval.retrieve(
        user_id=uid_a, project_id=pid_a, query="postgress", floor=0.0
    )) == {"toolchain: PostgreSQL", "toolchain: PostgreSQL project"}
    assert contents(await retrieval.retrieve(
        user_id=uid_a, project_id=pid_b, query="postgress", floor=0.0
    )) == {"toolchain: PostgreSQL"}
    assert contents(await retrieval.retrieve(
        user_id=uid_b, project_id=pid_b, query="postgress", floor=0.0
    )) == {"toolchain: PostgreSQL other"}


async def test_fuzzy_switch_off_matches_stage1(
    pg_engine, owner_ids, retrieval, monkeypatch
):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    memory_id = await store.save_memory(
        user_id=uid, content="toolchain: PostgreSQL", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )
    now = time.time()

    on_hits = await retrieval.retrieve(
        user_id=uid, query="postgres", now=now)
    assert [m.id for m in on_hits] == [memory_id]
    assert [m.id for m in await retrieval.retrieve(
        user_id=uid, query="postgress", now=now)] == [memory_id]

    monkeypatch.setenv("INVINCIBLE_MEMORY_FUZZY", "0")
    off_hits = await retrieval.retrieve(
        user_id=uid, query="postgres", now=now)
    assert [
        (m.id, m.content, m.score) for m in off_hits
    ] == [
        (m.id, m.content, m.score) for m in on_hits
    ]
    assert await retrieval.retrieve(
        user_id=uid, query="postgress", now=now) == []
    # A multi-token OR still runs on the Stage 1 path; it just has no
    # unused exact match to return (no fuzzy pooling candidate here).
    assert await retrieval.retrieve(
        user_id=uid, query="postgress pooling", now=now) == []


async def test_fuzzy_off_never_issues_trgm_sql(
    pg_engine, owner_ids, retrieval, monkeypatch, statements
):
    uid, _pid = owner_ids
    monkeypatch.setenv("INVINCIBLE_MEMORY_FUZZY", "0")
    del statements[:]
    await retrieval.retrieve(user_id=uid, query="postgress")
    rendered = "\n".join(sql for sql, _ in statements)
    assert "<%" not in rendered
    assert "word_similarity" not in rendered
    assert "SET LOCAL" not in rendered


async def test_200_word_query_issues_at_most_six_trgm_predicates(
    pg_engine, owner_ids, retrieval, statements
):
    uid, _pid = owner_ids
    store = MemoryStore(engine=pg_engine)
    await store.save_memory(
        user_id=uid, content="token000", layer="explicit",
        kind="fact", confidence=1.0, provenance="t",
    )
    query = " ".join(f"token{i:03d}" for i in range(200))

    del statements[:]
    hits = await retrieval.retrieve(user_id=uid, query=query, floor=0.0)

    assert [m.content for m in hits] == ["token000"]
    rendered = "\n".join(sql for sql, _ in statements)
    assert rendered.count("<%") == 6
    assert "token000" not in rendered
