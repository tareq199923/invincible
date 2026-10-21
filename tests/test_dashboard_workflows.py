# tests/test_dashboard_workflows.py
"""H6c: per-account workflow timelines (dashboard)."""
import time

from sqlalchemy import select

from invincible.core.db import workflow_events
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from tests.conftest import register_account


async def logged_in(client, seq):
    registered, _ = await register_account(
        client, f"workflows-{seq}@example.com")
    assert registered.status_code == 201, registered.text
    return registered.json()["id"]


async def _seed(engine, user_id, workflow_id, events):
    now = time.time()
    async with engine.begin() as conn:
        for offset, (etype, payload) in enumerate(events):
            await conn.execute(workflow_events.insert().values(
                workflow_id=workflow_id,
                user_id=user_id,
                type=etype,
                payload=payload,
                created_at=now + offset,
            ))


async def test_page_requires_session(client):
    assert (await client.get("/dashboard/workflows")).status_code == 401


async def test_page_empty_state(client):
    await logged_in(client, 1)
    page = await client.get("/dashboard/workflows")
    assert page.status_code == 200
    assert "No workflows yet" in page.text


async def test_page_lists_own_workflows(client, pg_engine):
    uid = await logged_in(client, 2)
    await _seed(pg_engine, uid, "web-mine", [
        ("plan.created", {"steps": [
            {"id": "a", "agent": "scout", "objective": "secret task"},
        ]}),
        ("subagent.completed", {
            "step_id": "a", "agent": "scout",
            "findings_preview": "secret findings",
        }),
    ])
    page = await client.get("/dashboard/workflows")
    assert page.status_code == 200
    assert "web-mine" in page.text
    assert "step a" in page.text
    assert "scout" in page.text
    assert "completed" in page.text
    assert "secret task" not in page.text
    assert "secret findings" not in page.text


async def test_page_isolated_per_user(client, pg_engine):
    uid_a = await logged_in(client, 3)
    await _seed(pg_engine, uid_a, "web-alice", [
        ("plan.created", {"steps": []}),
    ])
    mine = await client.get("/dashboard/workflows")
    assert "web-alice" in mine.text
    await logged_in(client, 4)  # replaces the session cookie
    theirs = await client.get("/dashboard/workflows")
    assert "web-alice" not in theirs.text
    assert "No workflows yet" in theirs.text


async def test_detail_page_shows_timeline(client, pg_engine):
    uid = await logged_in(client, 5)
    await _seed(pg_engine, uid, "web-detail", [
        ("plan.created", {"steps": [
            {"id": "a", "agent": "scout"},
            {"id": "b", "agent": "librarian"},
        ]}),
        ("subagent.started", {"step_id": "a", "agent": "scout"}),
        ("subagent.completed", {"step_id": "a", "agent": "scout"}),
    ])
    page = await client.get("/dashboard/workflows/web-detail")
    assert page.status_code == 200
    assert "web-detail" in page.text
    assert "plan.created" in page.text
    assert "2 steps" in page.text
    assert "subagent.started" in page.text
    assert "step a" in page.text


async def test_detail_page_foreign_is_404(client, pg_engine):
    uid_a = await logged_in(client, 6)
    await _seed(pg_engine, uid_a, "web-foreign", [
        ("plan.created", {"steps": []}),
    ])
    await logged_in(client, 7)  # user B
    page = await client.get("/dashboard/workflows/web-foreign")
    assert page.status_code == 404
    assert "plan.created" not in page.text


async def test_bus_persists_user_id(client, pg_engine):
    uid = await logged_in(client, 8)
    bus = HarnessBus()
    bus.attach_engine(pg_engine)
    bus.emit(
        HarnessEventType.PLAN_CREATED,
        workflow_id="web-bus",
        user_id=uid,
        steps=[],
    )
    await bus.flush_persisted()
    async with pg_engine.connect() as conn:
        row = (await conn.execute(
            select(workflow_events.c.user_id).where(
                workflow_events.c.workflow_id == "web-bus")
        )).first()
    assert row is not None
    assert row[0] == uid
