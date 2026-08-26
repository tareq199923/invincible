# tests/test_memory_v2.py
"""Phase 4 memory writes: scoped ``memories`` rows from deterministic
auto-extraction plus explicit "remember this" triggers.

Extraction is hermetic; storage tests run against real Postgres (the
memories table's generated tsvector column and GIN index land in
migration 0005 - retrieval itself is RetrievalService, tested separately).
"""
import pytest

from invincible.core.db import ensure_local_owner
from invincible.core.memory import (
    AUTO_CONFIDENCE,
    MemoryStore,
    extract_explicit,
    extract_facts,
    memory_row_from_fact,
)


def user(content):
    return {"role": "user", "content": content}


def assistant(content):
    return {"role": "assistant", "content": content}


@pytest.fixture
async def memory(pg_engine):
    yield MemoryStore(engine=pg_engine)


@pytest.fixture
async def owner_ids(pg_engine):
    return await ensure_local_owner(pg_engine)


# --- explicit triggers (pure) -----------------------------------------------


def test_extracts_explicit_saves():
    msgs = [
        user("Remember that the deploy window is Friday"),
        user("remember this: always prune before rebase"),
        user("save this: staging token lives in vault"),
    ]
    assert extract_explicit(msgs) == [
        "the deploy window is Friday",
        "always prune before rebase",
        "staging token lives in vault",
    ]


def test_explicit_skips_assistant_and_deduplicates():
    msgs = [
        user("remember that X marks the spot"),
        assistant("Sure, remember that X marks the spot too"),
        user("remember that X marks the spot"),
    ]
    assert extract_explicit(msgs) == ["X marks the spot"]


def test_explicit_ignores_short_noise():
    assert extract_explicit([user("remember that ok")]) == []


# --- triple -> memories rendering (pure) --------------------------------------


def test_fact_renders_to_kind_and_content():
    assert memory_row_from_fact("user", "name", "Sark") == ("fact", "name: Sark")
    assert memory_row_from_fact(
        "project", "preference", "dark themes"
    ) == ("preference", "preference: dark themes")
    assert memory_row_from_fact(
        "project", "current_task", "Phase 4"
    ) == ("task", "current_task: Phase 4")


# --- storage --------------------------------------------------------------------


async def test_record_memories_writes_auto_and_explicit_rows(memory, owner_ids):
    uid, _pid = owner_ids
    msgs = [user("my name is Sark. Remember that deploys freeze on Fridays")]
    added = await memory.record_memories(
        user_id=uid, client_session_id="sess-1", messages_list=msgs
    )
    assert added == 2

    async with memory.engine.connect() as conn:
        from invincible.core.db import memories

        rows = (await conn.execute(
            memories.select().order_by(memories.c.layer)
        )).mappings().all()
    by_layer = {r["layer"]: r for r in rows}
    assert by_layer["auto"]["kind"] == "fact"
    assert by_layer["auto"]["content"] == "name: Sark"
    assert by_layer["auto"]["confidence"] == pytest.approx(AUTO_CONFIDENCE)
    assert by_layer["explicit"]["confidence"] == 1.0
    assert by_layer["explicit"]["content"] == "deploys freeze on Fridays"
    for r in rows:
        assert r["scope"] == "user"
        assert r["project_id"] is None
        assert r["provenance"] == "chat:sess-1"


async def test_record_memories_is_idempotent_per_user(memory, owner_ids):
    uid, _pid = owner_ids
    msgs = [user("I prefer dark themes")]
    assert await memory.record_memories(
        user_id=uid, client_session_id="s", messages_list=msgs
    ) == 1
    assert await memory.record_memories(
        user_id=uid, client_session_id="s", messages_list=msgs
    ) == 0


async def test_same_content_stays_separate_between_users(memory, owner_ids):
    uid_a, _ = owner_ids
    async with memory.engine.begin() as conn:
        from invincible.core.db import users

        uid_b = await conn.execute(
            users.insert().values(
                email="b@example.com", created_at=1700000000.0
            )
        )
        uid_b = uid_b.inserted_primary_key[0]
    for uid in (uid_a, uid_b):
        assert await memory.record_memories(
            user_id=uid, client_session_id="s",
            messages_list=[user("my name is Shared")],
        ) == 1
    async with memory.engine.connect() as conn:
        from sqlalchemy import func, select

        from invincible.core.db import memories

        count = (await conn.execute(
            select(func.count()).select_from(memories)
        )).scalar_one()
    assert count == 2


async def test_explicit_toggle_off_keeps_auto_only(
    memory, owner_ids, monkeypatch
):
    monkeypatch.setenv("INVINCIBLE_MEMORY_EXPLICIT", "0")
    uid, _pid = owner_ids
    added = await memory.record_memories(
        user_id=uid,
        client_session_id="s",
        messages_list=[user("remember that nothing should save. "
                           "My name is Sark")],
    )
    # Explicit OFF: the remember-that phrase falls back to the legacy auto
    # note pattern (facts-era behavior), so name + note both land.
    assert added == 2


async def test_save_memory_project_scope(memory, owner_ids):
    uid, pid = owner_ids
    written = await memory.save_memory(
        user_id=uid,
        content="decision: postgres only",
        kind="decision",
        provenance="mcp:test",
        project_id=pid,
    )
    assert isinstance(written, int)
    async with memory.engine.connect() as conn:
        from invincible.core.db import memories

        row = (await conn.execute(memories.select())).mappings().one()
    assert row["scope"] == "project"
    assert row["project_id"] == pid


async def test_save_memory_rejects_bad_layer(memory, owner_ids):
    uid, _pid = owner_ids
    with pytest.raises(ValueError, match="layer"):
        await memory.save_memory(user_id=uid, content="x", layer="nope")


def test_legacy_extraction_still_yields_triples():
    # The facts pipeline stays alive (dual-write era) until ContextBuilder
    # retires it; its behavior must not drift while memories land beside it.
    triples = extract_facts([user("my name is Sark")])
    assert ("user", "name", "Sark") in triples
