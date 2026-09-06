# tests/test_session_store_v2.py
"""Normalized sessions/turns/messages storage (Phase 15a behavior, Phase 16
PostgreSQL engine).

Covers the review guarantees that survive the backend swap:
  #1 boundary rule == group_into_turns (incl. tool/assistant-led batches)
  #2 retention deletes WHOLE turns only
  #3 save() full replace via the same walker
  #5 focused Claude-Code-style tool-result batch test
Plus concurrent appends (FOR UPDATE serialization), sibling-store
coexistence on one engine, and a seeded property-equivalence test against
the blob-era semantics.

SQLite-era mechanics tests (blob-table migration, _invincible_schema
marker, shared-connection lock) were retired with the SQLite backend; the
legacy-data path is covered by `invincible db import` tests instead.

Ownership: SessionStore takes a REQUIRED owner on every call (the
local-owner fallback was removed - multi-tenant audit Step 2). These
mechanics tests pin the system local owner explicitly via the ``owner``
fixture; the loud-failure contract itself is pinned by
``test_owner_less_calls_raise``.
"""
import random

import pytest
from sqlalchemy import func, select

from invincible.core.db import (
    LOCAL_OWNER_EMAIL,
    LOCAL_PROJECT_NAME,
    ensure_local_owner,
)
from invincible.core.db import (
    messages as messages_table,
)
from invincible.core.db import (
    projects as projects_table,
)
from invincible.core.db import (
    sessions as sessions_table,
)
from invincible.core.db import (
    turns as turns_table,
)
from invincible.core.db import (
    users as users_table,
)
from invincible.core.memory import MemoryStore
from invincible.core.run_store import RunStore
from invincible.core.session_store import SessionStore
from invincible.core.trimming import group_into_turns


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


@pytest.fixture
async def store(pg_engine):
    return SessionStore(engine=pg_engine)


@pytest.fixture
async def owner(pg_engine):
    """``**owner`` kwargs for the system local owner - the explicit
    identity mechanics tests write and read under."""
    uid, pid = await ensure_local_owner(pg_engine)
    return {"user_id": uid, "project_id": pid}


async def local_pk(store, session_id="s"):
    """Surrogate session pk for ``session_id`` under the system *local*
    owner (the identity the ``owner`` fixture pins)."""
    async with store.engine.connect() as conn:
        row = (await conn.execute(
            select(sessions_table.c.id)
            .join(users_table, users_table.c.id == sessions_table.c.user_id)
            .join(
                projects_table,
                projects_table.c.id == sessions_table.c.project_id,
            )
            .where(
                users_table.c.email == LOCAL_OWNER_EMAIL,
                projects_table.c.name == LOCAL_PROJECT_NAME,
                sessions_table.c.client_session_id == session_id,
            )
        )).first()
    assert row is not None, f"session {session_id!r} not found"
    return int(row[0])


async def turn_count(store, session_id="s"):
    pk = await local_pk(store, session_id)
    async with store.engine.connect() as conn:
        return (await conn.execute(
            select(func.count()).select_from(turns_table)
            .where(turns_table.c.session_id == pk)
        )).scalar_one()


async def turn_sizes(store, session_id="s"):
    pk = await local_pk(store, session_id)
    msg_count = (
        select(func.count(messages_table.c.id))
        .where(messages_table.c.turn_id == turns_table.c.id)
        .correlate(turns_table)
        .scalar_subquery()
    )
    async with store.engine.connect() as conn:
        rows = (await conn.execute(
            select(turns_table.c.seq, msg_count)
            .where(turns_table.c.session_id == pk)
            .order_by(turns_table.c.seq.asc())
        )).all()
    return [count for _, count in rows]


# ---------------------------------------------------------------- basics


async def test_owner_less_calls_raise(pg_engine):
    """Multi-tenant audit Step 2: the local-owner fallback is gone. An
    owner-less call must fail loudly (TypeError from the required kwargs),
    never silently write to or read from the operator's sessions."""
    store = SessionStore(engine=pg_engine)
    with pytest.raises(TypeError):
        await store.append("s", [user("hi")])
    with pytest.raises(TypeError):
        await store.load("s")
    with pytest.raises(TypeError):
        await store.save("s", [])


async def test_append_load_roundtrip_matches_concat(store, owner):
    await store.append("s", [user("hi"), assistant("hello")], **owner)
    await store.append("s", [user("more"), assistant("ok")], **owner)
    assert await store.load("s", **owner) == [
        user("hi"),
        assistant("hello"),
        user("more"),
        assistant("ok"),
    ]


async def test_exotic_payload_fields_round_trip(store, owner):
    exotic = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "ünïcode ✅.txt"}',
                    },
                }
            ],
            "custom_future_field": {"nested": [1, 2, {"x": None}]},
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "data"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "blocks"}],
        },
    ]
    await store.append("s", exotic, **owner)
    assert await store.load("s", **owner) == exotic


async def test_load_missing_session_is_empty(store, owner):
    assert await store.load("nope", **owner) == []


# ------------------------------------------- boundary rule (note #1/#5)


async def test_tool_led_batch_attaches_to_previous_turn(store, owner):
    """Note #5: Claude Code style - tool results arrive as their OWN batch
    right after an assistant tool_calls reply."""
    first = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "f", "arguments": "{}"}}
        ],
    }
    await store.append("s", [first], **owner)
    await store.append(
        "s",
        [
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
            user("continue"),
        ],
        **owner,
    )
    flat = await store.load("s", **owner)
    assert flat == [first,
                    {"role": "tool", "tool_call_id": "c1",
                     "content": "result"},
                    user("continue")]
    # group_into_turns over the flat list must see exactly 2 turns and
    # the stored layout must match that grouping shape.
    sizes = await turn_sizes(store)
    assert sizes == [2, 1]
    grouped = group_into_turns(flat)
    assert [len(t) for t in grouped] == sizes


async def test_assistant_led_batch_attaches_to_previous_turn(store, owner):
    await store.append("s", [user("q")], **owner)
    await store.append(
        "s", [assistant("partial-a"), assistant("partial-b")], **owner)
    assert await turn_sizes(store) == [3]
    flat = await store.load("s", **owner)
    assert [len(t) for t in group_into_turns(flat)] == [3]


async def test_first_message_any_role_opens_first_turn(store, owner):
    await store.append("s", [assistant("opening")], **owner)
    assert await turn_count(store) == 1
    assert await turn_sizes(store) == [1]


# ------------------------------------------------- retention (note #2)


async def test_retention_deletes_whole_turns_only(monkeypatch, store, owner):
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "2")
    await store.append(
        "s", [user("t1"), assistant("a1"), assistant("a1b")], **owner)
    await store.append("s", [user("t2"), assistant("a2")], **owner)
    await store.append("s", [user("t3"), assistant("a3")], **owner)

    loaded = await store.load("s", **owner)
    assert loaded == [user("t2"), assistant("a2"), user("t3"), assistant("a3")]
    sizes = await turn_sizes(store)
    assert sizes == [2, 2]
    # No partial turn: every surviving turn is complete by construction.
    for size in sizes:
        assert size > 0


async def test_retention_keeps_single_oversized_turn(monkeypatch, store,
                                                      owner):
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "1")
    big_turn = [user("q")] + [assistant("x")] * 50
    await store.append("s", big_turn, **owner)
    assert await store.load("s", **owner) == big_turn
    assert await turn_sizes(store) == [51]


async def test_retention_disabled_via_off(monkeypatch, store, owner):
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "off")
    for i in range(5):
        await store.append("s", [user(str(i)), assistant("r")], **owner)
    assert await turn_count(store) == 5


# ------------------------------------------------- save full replace (#3)


async def test_save_full_replace_through_same_walker(monkeypatch, store,
                                                      owner):
    """Note #3: save() deletes everything and re-inserts via the walker -
    proven by boundary structure surviving identical to append-path."""
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "off")
    await store.append("s", [user("old1"), assistant("o1")], **owner)
    await store.append("s", [user("old2"), assistant("o2")], **owner)

    replacement = [
        assistant("led-by-assistant"),
        {"role": "tool", "tool_call_id": "c", "content": "r"},
        user("new"),
    ]
    await store.save("s", replacement, **owner)

    assert await store.load("s", **owner) == replacement
    # Same walker -> same grouping the append path would have produced.
    sizes = await turn_sizes(store)
    assert [len(t) for t in group_into_turns(replacement)] == sizes
    # Old content really is gone at the row level.
    total = sum(await turn_sizes(store))
    assert total == len(replacement)


async def test_save_empty_clears_session(store, owner):
    await store.append("s", [user("bye")], **owner)
    await store.save("s", [], **owner)
    assert await store.load("s", **owner) == []
    assert await turn_count(store) == 0


# ------------------------------------------------- concurrency & coexist


async def test_concurrent_appends_both_persist(store, owner):
    await asyncio_gather_appends(store, owner)
    flat = await store.load("s", **owner)
    assert len(flat) == 4
    assert {flat[0]["content"], flat[2]["content"]} == {"one", "two"}
    # Whatever the interleaving, grouping stays consistent.
    assert [len(t) for t in group_into_turns(flat)] == await turn_sizes(store)


async def asyncio_gather_appends(store, owner):
    import asyncio

    await asyncio.gather(
        store.append("s", [user("one"), assistant("1")], **owner),
        store.append("s", [user("two"), assistant("2")], **owner),
    )


async def test_memory_and_run_stores_coexist_on_shared_engine(pg_engine):
    """The Phase 16 analogue of shared-connection coexistence: all three
    stores over ONE engine see each other's committed rows."""
    store = SessionStore(engine=pg_engine)
    memory = MemoryStore(engine=pg_engine)
    runs = RunStore(engine=pg_engine)

    uid, pid = await ensure_local_owner(pg_engine)
    await store.append(
        "sess", [user("remember that x is 9"), assistant("ok")],
        user_id=uid, project_id=pid)
    from invincible.core.db import memories

    assert await memory.record_memories(
        user_id=uid,
        client_session_id="sess",
        messages_list=await store.load("sess", user_id=uid, project_id=pid),
    ) == 1
    async with pg_engine.connect() as conn:
        assert (await conn.execute(
            memories.select()
        )).first() is not None  # memory extracted from the turn
    await runs.record(
        {
            "request_id": "r1",
            "session_id": "sess",
            "provider_name": "p",
            "model_id": "m",
            "attempt_index": 1,
            "outcome": "ok",
            "started_at": 1.0,
            "finished_at": 2.0,
        }
    )
    assert len(await runs.recent(session_id="sess")) == 1


# ------------------------------------------------- property equivalence


async def test_property_equivalence_with_blob_semantics(store, owner):
    """Seeded random streams: normalized storage output must equal the
    blob-era concat + group_into_turns reference, boundaries included."""
    rng = random.Random(1507)
    reference: list = []
    roles = ["user", "assistant", "tool"]
    for _ in range(60):
        batch = []
        for _ in range(rng.randint(1, 4)):
            role = rng.choice(roles)
            if role == "tool":
                batch.append({"role": "tool", "tool_call_id": "c",
                              "content": rng.randint(0, 99)})
            else:
                batch.append({"role": role, "content": rng.random()})
        await store.append("s", batch, **owner)
        reference.extend(batch)

    loaded = await store.load("s", **owner)
    assert loaded == reference
    stored_sizes = await turn_sizes(store)
    grouped_sizes = [len(t) for t in group_into_turns(reference)]
    assert stored_sizes == grouped_sizes
