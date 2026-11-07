# tests/test_background_tasks_ui.py
"""Background-task dashboard UI: start form, status column, result page.

The HTTP surface is the real cookie-realm dashboard; workflow events and
sessions are seeded directly in Postgres so each assertion is about the
rendered result, not about the runner (covered by test_background_tasks).
"""
import time

from invincible.core.db import workflow_events
from tests.conftest import register_account


async def _login(client, email):
    registered, _ = await register_account(client, email)
    assert registered.status_code == 201, registered.text
    return registered.json()


async def _seed_events(engine, user_id, workflow_id, events):
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


async def test_workflows_page_has_start_form(client):
    await _login(client, "bt-ui-form@example.com")
    page = await client.get("/dashboard/workflows")
    assert page.status_code == 200
    assert 'id="background-task-form"' in page.text
    assert 'maxlength="8000"' in page.text
    assert "0 / 8000" in page.text
    assert "plan mode (read-only)" in page.text
    assert "/static/background-tasks.js" in page.text
    assert '<th>Status</th>' in page.text
    assert '<th>Last event</th>' not in page.text


async def test_workflows_page_task_status_and_non_task_summary(
    client, pg_engine
):
    body = await _login(client, "bt-ui-status@example.com")
    uid = body["id"]
    await _seed_events(pg_engine, uid, "task-done", [
        ("workflow.started", {"input_preview": "do it"}),
        ("workflow.completed", {"output_preview": "done"}),
    ])
    await _seed_events(pg_engine, uid, "task-late", [
        ("workflow.started", {"input_preview": "do it"}),
        ("workflow.failed", {"error": "timed out"}),
    ])
    await _seed_events(pg_engine, uid, "web-plan", [
        ("plan.created", {"steps": []}),
    ])

    page = await client.get("/dashboard/workflows")
    assert page.status_code == 200
    assert 'status-badge status-completed">completed' in page.text
    assert 'status-badge status-timed-out">timed out' in page.text
    assert "failed - timed out" in page.text
    assert "0 steps" in page.text
