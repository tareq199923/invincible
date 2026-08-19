import json

import httpx
import pytest

from invincible.core.memory import (
    MemoryStore,
    extract_facts,
    memory_system_message,
    render_facts_message,
)
from invincible.core.session_store import SessionStore
from invincible.main import app


def user(content):
    return {"role": "user", "content": content}


def assistant(content):
    return {"role": "assistant", "content": content}


@pytest.fixture
async def memory():
    store = SessionStore(db_path=":memory:")
    await store.init()
    mem = MemoryStore(shared=store)
    await mem.init()
    yield mem
    await mem.close()
    await store.close()


# --- extraction ---------------------------------------------------------------


def test_extracts_explicit_facts():
    facts = extract_facts([
        user("Hi, my name is Sark. Remember that the deploy window is Friday."),
        assistant("Got it — we decided to ship after the freeze."),
    ])
    assert ("user", "name", "Sark") in facts
    assert ("user", "note", "the deploy window is Friday") in facts
    assert ("project", "decision", "ship after the freeze") in facts


def test_extracts_task_continuity_facts():
    facts = extract_facts([
        user("I'm currently working on Phase 10 memory. "
             "The next step is wiring the endpoints."),
    ])
    assert ("project", "current_task", "Phase 10 memory") in facts
    assert ("project", "next_step", "wiring the endpoints") in facts


def test_no_facts_from_ordinary_chatter():
    assert extract_facts([user("what time is it?"), assistant("4pm")]) == []


def test_extraction_is_batch_deduplicated():
    msgs = [user("my name is Sark"), user("my name is Sark")]
    facts = extract_facts(msgs)
    assert facts.count(("user", "name", "Sark")) == 1


def test_targets_are_capped_and_single_line():
    facts = extract_facts([user("remember that " + "x" * 500 + "\nignore this")])
    assert len(facts) == 1
    assert len(facts[0][2]) <= 160
    assert "ignore this" not in facts[0][2]


# --- storage / idempotency -----------------------------------------------------


@pytest.mark.asyncio
async def test_record_is_idempotent(memory):
    msgs = [user("my name is Sark")]
    assert await memory.record("s1", msgs) == 1
    assert await memory.record("s1", msgs) == 0
    assert await memory.facts_for("s1") == [("user", "name", "Sark")]


@pytest.mark.asyncio
async def test_facts_are_scoped_per_session(memory):
    await memory.record("s1", [user("my name is Sark")])
    await memory.record("s2", [user("my name is Other")])
    assert await memory.facts_for("s1") == [("user", "name", "Sark")]
    assert await memory.facts_for("s2") == [("user", "name", "Other")]


@pytest.mark.asyncio
async def test_facts_use_sentinel_user_key(memory):
    rows = await memory.facts_for("s1")
    assert rows == []
    await memory.record("s1", [user("remember that the sky is blue")])
    async with memory._db.execute(
        "SELECT user_id FROM facts WHERE session_id = 's1'"
    ) as cursor:
        (user_id,) = await cursor.fetchone()
    assert user_id == "default"


@pytest.mark.asyncio
async def test_facts_for_respects_limit_most_recent(memory):
    for i in range(5):
        await memory.record("s1", [user(f"remember that fact number {i} holds")])
    facts = await memory.facts_for("s1", limit=2)
    assert len(facts) == 2
    assert "number 3" in facts[0][2] and "number 4" in facts[1][2]


# --- injection ----------------------------------------------------------------


def test_render_empty_facts_returns_none():
    assert render_facts_message([]) is None


def test_rendered_message_is_marked_system():
    msg = render_facts_message([("user", "name", "Sark")])
    assert msg["role"] == "system"
    assert "Session memory" in msg["content"]
    assert "user name: Sark" in msg["content"]


@pytest.mark.asyncio
async def test_memory_system_message_disabled_by_env(memory, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_MEMORY", "0")
    await memory.record("s1", [user("my name is Sark")])
    assert await memory_system_message(memory, "s1") is None


# --- retention -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_is_bounded_to_turn_cap(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "3")
    store = SessionStore(db_path=":memory:")
    await store.init()
    for i in range(6):
        await store.append("s1", [user(f"turn {i}"), assistant(f"reply {i}")])
    history = await store.load("s1")
    users = [m["content"] for m in history if m["role"] == "user"]
    assert users == ["turn 3", "turn 4", "turn 5"]
    await store.close()


@pytest.mark.asyncio
async def test_retention_disabled_when_off(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "off")
    store = SessionStore(db_path=":memory:")
    await store.init()
    for i in range(5):
        await store.append("s1", [user(f"turn {i}")])
    assert len(await store.load("s1")) == 5
    await store.close()


# --- end-to-end through the OpenAI endpoint ------------------------------------


@pytest.mark.asyncio
async def test_facts_injected_on_next_request(client, monkeypatch):
    monkeypatch.delenv("INVINCIBLE_MEMORY", raising=False)
    store = app.state.sessions
    memory = MemoryStore(shared=store)
    await memory.init()
    app.state.memory = memory

    received = {}

    def handler(request: httpx.Request):
        received["payload"] = json.loads(request.read())
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    app.state.router.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    headers = {
        "X-Session-Id": "mem-e2e",
        "Authorization": "Bearer test-gateway-key",
    }
    await client.post(
        "/v1/chat/completions",
        json={"messages": [user("my name is Sark and I'm working on Phase 10")]},
        headers=headers,
    )
    # Second request: the injected memory must reach the provider payload.
    await client.post(
        "/v1/chat/completions",
        json={"messages": [user("what was I doing?")]},
        headers=headers,
    )
    sent = received["payload"]["messages"]
    mem_msgs = [
        m for m in sent
        if m["role"] == "system" and "Session memory" in m["content"]
    ]
    assert len(mem_msgs) == 1
    assert "user name: Sark" in mem_msgs[0]["content"]
    assert "current_task" in mem_msgs[0]["content"]

    # Injected memory must never be persisted into stored history.
    stored = await store.load("mem-e2e")
    assert all("Session memory" not in (m.get("content") or "") for m in stored)

    app.state.memory = None
