import json

import httpx
import pytest

from invincible.core.memory import (
    MemoryStore,
    extract_explicit,
    extract_facts,
)
from invincible.core.session_store import SessionStore
from invincible.main import app


def user(content):
    return {"role": "user", "content": content}


def assistant(content):
    return {"role": "assistant", "content": content}


@pytest.fixture
async def memory(pg_engine):
    yield MemoryStore(engine=pg_engine)


# --- extraction ---------------------------------------------------------------


def test_extracts_explicit_facts():
    facts = extract_facts([
        user("Hi, my name is Sark."),
        assistant("Got it — we decided to ship after the freeze."),
    ])
    assert ("user", "name", "Sark") in facts
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
    facts = extract_facts([user("I prefer " + "x" * 500 + "\nignore this")])
    assert len(facts) == 1
    assert len(facts[0][2]) <= 160
    assert "ignore this" not in facts[0][2]


def test_explicit_triggers_are_user_only():
    msgs = [
        assistant("remember that I said this"),
        user("save this: the answer"),
    ]
    assert extract_explicit(msgs) == ["the answer"]


# --- retention -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_is_bounded_to_turn_cap(monkeypatch, pg_engine):
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "3")
    store = SessionStore(engine=pg_engine)
    for i in range(6):
        await store.append("s1", [user(f"turn {i}"), assistant(f"reply {i}")])
    history = await store.load("s1")
    users = [m["content"] for m in history if m["role"] == "user"]
    assert users == ["turn 3", "turn 4", "turn 5"]


@pytest.mark.asyncio
async def test_retention_disabled_when_off(monkeypatch, pg_engine):
    monkeypatch.setenv("INVINCIBLE_HISTORY_MAX_TURNS", "off")
    store = SessionStore(engine=pg_engine)
    for i in range(5):
        await store.append("s1", [user(f"turn {i}")])
    assert len(await store.load("s1")) == 5


# --- end-to-end through the OpenAI endpoint ------------------------------------


@pytest.mark.asyncio
async def test_retrieved_memory_injected_on_next_request(
    client, pg_engine, monkeypatch
):
    """A saved fact reaches a LATER request's provider payload through
    lexical retrieval - matched by the new question's own terms."""
    monkeypatch.delenv("INVINCIBLE_MEMORY", raising=False)
    store = app.state.sessions

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
    # Turn 1 plants an explicit memory.
    await client.post(
        "/v1/chat/completions",
        json={"messages": [
            user("Remember that postgres connection pooling matters here")
        ]},
        headers=headers,
    )
    # Turn 2's QUESTION shares terms with it, so retrieval must inject.
    await client.post(
        "/v1/chat/completions",
        json={"messages": [
            user("how should I configure postgres pooling?")
        ]},
        headers=headers,
    )
    sent = received["payload"]["messages"]
    mem_msgs = [
        m for m in sent
        if m["role"] == "system" and "[Relevant memory" in m["content"]
    ]
    assert len(mem_msgs) == 1
    assert "postgres connection pooling matters here" in mem_msgs[0]["content"]

    # Injected memory must never be persisted into stored history.
    stored = await store.load("mem-e2e")
    assert all("[Relevant memory" not in (m.get("content") or "") for m in stored)


@pytest.mark.asyncio
async def test_memory_disabled_means_no_injection(
    client, pg_engine, monkeypatch
):
    """Master toggle off: nothing recorded, nothing retrieved, nothing
    injected - but the request still succeeds."""
    monkeypatch.setenv("INVINCIBLE_MEMORY", "0")
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
        "X-Session-Id": "mem-off",
        "Authorization": "Bearer test-gateway-key",
    }
    response = await client.post(
        "/v1/chat/completions",
        json={"messages": [user("Remember that the sky is green")]},
        headers=headers,
    )
    assert response.status_code == 200
    sent = received["payload"]["messages"]
    assert all("[Relevant memory" not in (m.get("content") or "") for m in sent)

    from sqlalchemy import select

    from invincible.core.db import memories

    async with pg_engine.connect() as conn:
        rows = (await conn.execute(select(memories.c.id))).all()
    assert rows == []
