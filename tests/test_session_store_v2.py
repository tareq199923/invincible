# tests/test_session_store_v2.py
"""Phase 15a: normalized sessions/turns/messages storage.

Covers the five review notes explicitly:
  #1 boundary rule == group_into_turns (incl. tool/assistant-led batches)
  #2 retention deletes WHOLE turns only
  #3 save() full replace via the same walker
  #4 module docstring documents frozen legacy backup + one-shot migration
     (asserted here via behavior: legacy rows byte-identical post-migration)
  #5 focused Claude-Code-style tool-result batch test
Plus migration idempotency, shared-store coexistence, concurrency, and a
seeded property-equivalence test against the blob-era semantics.
"""
import asyncio
import json
import random

import aiosqlite

from invincible.core.memory import MemoryStore
from invincible.core.run_store import RunStore
from invincible.core.session_store import SessionStore
from invincible.core.trimming import group_into_turns


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


async def make_store():
    store = SessionStore(db_path=":memory:")
    await store.init()
    return store


async def turn_count(store, session_id="s"):
    async with store.connection().execute(
        "SELECT COUNT(*) FROM turns WHERE session_id = ?", (session_id,)
    ) as cursor:
        return (await cursor.fetchone())[0]


async def turn_sizes(store, session_id="s"):
    async with store.connection().execute(
        """
        SELECT t.seq, COUNT(m.id) FROM turns t
        LEFT JOIN messages m ON m.turn_id = t.id
        WHERE t.session_id = ? GROUP BY t.id ORDER BY t.seq
        """,
        (session_id,),
    ) as cursor:
        return [row[1] for row in await cursor.fetchall()]


# ---------------------------------------------------------------- basics


async def test_append_load_roundtrip_matches_concat():
    store = await make_store()
    try:
        await store.append("s", [user("hi"), assistant("hello")])
        await store.append("s", [user("more"), assistant("ok")])
        assert await store.load("s") == [
            user("hi"),
            assistant("hello"),
            user("more"),
            assistant("ok"),
        ]
    finally:
        await store.close()


async def test_exotic_payload_fields_round_trip():
    store = await make_store()
    try:
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
        await store.append("s", exotic)
        assert await store.load("s") == exotic
    finally:
        await store.close()


async def test_load_missing_session_is_empty():
    store = await make_store()
    try:
        assert await store.load("nope") == []
    finally:
        await store.close()


# ------------------------------------------- boundary rule (note #1/#5)


async def test_tool_led_batch_attaches_to_previous_turn():
    """Note #5: Claude Code style - tool results arrive as their OWN batch
    right after an assistant tool_calls reply."""
    store = await make_store()
    try:
        first = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}
            ],
        }
        await store.append("s", [first])
        await store.append(
            "s",
            [
                {"role": "tool", "tool_call_id": "c1", "content": "result"},
                user("continue"),
            ],
        )
        flat = await store.load("s")
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
    finally:
        await store.close()


async def test_assistant_led_batch_attaches_to_previous_turn():
    store = await make_store()
    try:
        await store.append("s", [user("q")])
        await store.append("s", [assistant("partial-a"), assistant("partial-b")])
        assert await turn_sizes(store) == [3]
        flat = await store.load("s")
        assert [len(t) for t in group_into_turns(flat)] == [3]
    finally:
        await store.close()


async def test_first_message_any_role_opens_first_turn():
    store = await make_store()
    try:
        await store.append("s", [assistant("opening")])
        assert await turn_count(store) == 1
        assert await turn_sizes(store) == [1]
    finally:
        await store.close()


# ------------------------------------------------- retention (note #2)


async def test_retention_deletes_whole_turns_only(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "2")
    store = await make_store()
    try:
        await store.append("s", [user("t1"), assistant("a1"), assistant("a1b")])
        await store.append("s", [user("t2"), assistant("a2")])
        await store.append("s", [user("t3"), assistant("a3")])

        loaded = await store.load("s")
        assert loaded == [user("t2"), assistant("a2"), user("t3"), assistant("a3")]
        sizes = await turn_sizes(store)
        assert sizes == [2, 2]
        # No partial turn: every surviving turn is complete by construction.
        for size in sizes:
            assert size > 0
    finally:
        await store.close()


async def test_retention_keeps_single_oversized_turn(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "1")
    store = await make_store()
    try:
        big_turn = [user("q")] + [assistant("x")] * 50
        await store.append("s", big_turn)
        assert await store.load("s") == big_turn
        assert await turn_sizes(store) == [51]
    finally:
        await store.close()


async def test_retention_disabled_via_off(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "off")
    store = await make_store()
    try:
        for i in range(5):
            await store.append("s", [user(str(i)), assistant("r")])
        assert await turn_count(store) == 5
    finally:
        await store.close()


# ------------------------------------------------- save full replace (#3)


async def test_save_full_replace_through_same_walker(monkeypatch):
    """Note #3: save() deletes everything and re-inserts via the walker -
    proven by boundary structure surviving identical to append-path."""
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "off")
    store = await make_store()
    try:
        await store.append("s", [user("old1"), assistant("o1")])
        await store.append("s", [user("old2"), assistant("o2")])

        replacement = [
            assistant("led-by-assistant"),
            {"role": "tool", "tool_call_id": "c", "content": "r"},
            user("new"),
        ]
        await store.save("s", replacement)

        assert await store.load("s") == replacement
        # Same walker -> same grouping the append path would have produced.
        sizes = await turn_sizes(store)
        assert [len(t) for t in group_into_turns(replacement)] == sizes
        # Old content really is gone at the row level.
        async with store.connection().execute(
            "SELECT COUNT(*) FROM messages"
        ) as cursor:
            assert (await cursor.fetchone())[0] == len(replacement)
    finally:
        await store.close()


async def test_save_empty_clears_session():
    store = await make_store()
    try:
        await store.append("s", [user("bye")])
        await store.save("s", [])
        assert await store.load("s") == []
        assert await turn_count(store) == 0
    finally:
        await store.close()


# ------------------------------------------------------- migration (#4)


async def make_legacy_db(path, sessions: dict[str, list | str]):
    """Build a pre-15a database: sessions(id, messages JSON blob, updated_at)."""
    conn = await aiosqlite.connect(path)
    try:
        await conn.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                messages TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        for session_id, payload in sessions.items():
            blob = (
                json.dumps(payload, ensure_ascii=False)
                if not isinstance(payload, str)
                else payload  # raw string = deliberately corrupt
            )
            await conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (session_id, blob, 1700000000.0),
            )
        await conn.commit()
    finally:
        await conn.close()


async def snapshot_legacy_rows(path):
    conn = await aiosqlite.connect(path)
    try:
        async with conn.execute(
            "SELECT session_id, messages, updated_at FROM sessions ORDER BY session_id"
        ) as cursor:
            return await cursor.fetchall()
    finally:
        await conn.close()


async def test_migration_transforms_and_freezes_legacy(tmp_path):
    path = str(tmp_path / "legacy.db")
    alpha = [user("a1"), assistant("a2")]
    beta = [user("b1")]
    corrupt = "{not json!!"
    await make_legacy_db(
        path, {"alpha": alpha, "beta": beta, "corrupt": corrupt, "empty": []}
    )
    before = await snapshot_legacy_rows(path)

    store = SessionStore(db_path=path)
    try:
        await store.init()
        assert await store.load("alpha") == alpha
        assert await store.load("beta") == beta
        assert await store.load("corrupt") == []  # tolerated like blob-era
        assert await store.load("empty") == []

        # Note #4: legacy table frozen - byte-identical after migration.
        assert await snapshot_legacy_rows(path) == before

        # Marker written; second init is a no-op.
        async with store.connection().execute(
            "SELECT value FROM _invincible_schema WHERE key='sessions_migrated'"
        ) as cursor:
            assert await cursor.fetchone() is not None

        turns_before = await turn_count(store, "alpha")
        await store.init()  # re-run
        assert await turn_count(store, "alpha") == turns_before
        assert await snapshot_legacy_rows(path) == before
    finally:
        await store.close()


async def test_migration_preserves_grouping_boundaries(tmp_path):
    path = str(tmp_path / "legacy.db")
    blob = [
        user("u1"),
        assistant("a1"),
        {"role": "tool", "tool_call_id": "c", "content": "r"},
        user("u2"),
        assistant("a2"),
    ]
    await make_legacy_db(path, {"s": blob})

    store = SessionStore(db_path=path)
    try:
        await store.init()
        assert await store.load("s") == blob
        sizes = await turn_sizes(store, "s")
        assert [len(t) for t in group_into_turns(blob)] == sizes == [3, 2]
    finally:
        await store.close()


async def test_fresh_database_has_no_legacy_table_and_marks_done(tmp_path):
    store = SessionStore(db_path=str(tmp_path / "fresh.db"))
    try:
        await store.init()
        async with store.connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ) as cursor:
            assert await cursor.fetchone() is None
        async with store.connection().execute(
            "SELECT 1 FROM _invincible_schema WHERE key='sessions_migrated'"
        ) as cursor:
            assert await cursor.fetchone() is not None
    finally:
        await store.close()


# ------------------------------------------------- concurrency & coexist


async def test_concurrent_appends_both_persist():
    store = await make_store()
    try:
        await asyncio.gather(
            store.append("s", [user("one"), assistant("1")]),
            store.append("s", [user("two"), assistant("2")]),
        )
        flat = await store.load("s")
        assert len(flat) == 4
        assert {flat[0]["content"], flat[2]["content"]} == {"one", "two"}
        # Whatever the interleaving, grouping stays consistent.
        assert [len(t) for t in group_into_turns(flat)] == await turn_sizes(store)
    finally:
        await store.close()


async def test_memory_and_run_stores_coexist_on_shared_connection(tmp_path):
    store = SessionStore(db_path=str(tmp_path / "shared.db"))
    await store.init()
    try:
        memory = MemoryStore(shared=store)
        await memory.init()
        runs = RunStore(shared=store)
        await runs.init()

        await store.append("sess", [user("remember that x is 9"), assistant("ok")])
        assert await memory.record("sess", await store.load("sess")) >= 0
        assert await memory.facts_for("sess")  # fact extracted from the turn
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
        await memory.close()
        await runs.close()
    finally:
        await store.close()


# ------------------------------------------------- property equivalence


async def test_property_equivalence_with_blob_semantics():
    """Seeded random streams: v2 storage output must equal the blob-era
    concat + group_into_turns reference, turn boundaries included."""
    rng = random.Random(1507)
    store = await make_store()
    try:
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
            await store.append("s", batch)
            reference.extend(batch)

        loaded = await store.load("s")
        assert loaded == reference
        stored_sizes = await turn_sizes(store)
        grouped_sizes = [len(t) for t in group_into_turns(reference)]
        assert stored_sizes == grouped_sizes
    finally:
        await store.close()
