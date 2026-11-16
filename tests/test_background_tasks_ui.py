# tests/test_background_tasks_ui.py
"""Background-task dashboard UI: start form, status column, result page.

The HTTP surface is the real cookie-realm dashboard; workflow events and
sessions are seeded directly in Postgres so each assertion is about the
rendered result, not about the runner (covered by test_background_tasks).
"""
import asyncio
import time

import pytest

from invincible.core import background_tasks as bt
from invincible.core.background_tasks import (
    new_workflow_id,
    run_background_task,
)
from invincible.core.db import workflow_events
from invincible.core.harness_bus import HarnessBus
from invincible.core.principal import Principal
from invincible.main import app
from tests.conftest import provider_body, register_account
from tests.test_background_tasks import (
    _drain,
    _login_with_credential,
    _runner_kwargs,
    _scripted,
)

HX = {"HX-Request": "true"}


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


async def _finished_task(client, router_setter, email, *, task, answer):
    """Run one real task end to end through the endpoint (fake provider)."""
    _bodies, handler = _scripted([provider_body("w1", content=answer)])
    router_setter({"w1.example.com": handler})
    uid, pid = await _login_with_credential(client, email)
    started = await client.post(
        "/dashboard/background-tasks", json={"task": task})
    assert started.status_code == 202, started.text
    workflow_id = started.json()["workflow_id"]
    await _drain(app.state.background_tasks, uid)
    await app.state.harness_bus.flush_persisted()
    return uid, pid, workflow_id


async def test_result_page_shows_task_and_answer_for_owner(
    client, byok_env, router_setter
):
    _uid, _pid, workflow_id = await _finished_task(
        client, router_setter, "bt-ui-result@example.com",
        task="plan a trip", answer="the **final** answer")

    page = await client.get(f"/dashboard/workflows/{workflow_id}")
    assert page.status_code == 200
    assert 'status-badge status-completed">completed' in page.text
    assert "plan a trip" in page.text
    assert "<strong>final</strong>" in page.text
    assert "/dashboard/sessions/" in page.text
    # Terminal render: no polling attributes and no Stop button.
    assert "hx-get=" not in page.text
    assert "data-cancel-workflow" not in page.text
    # Task runs get the owner-only note; the generic one is replaced.
    assert "shown to the owning" in page.text
    assert "Task text, tool output, and file contents are not shown" \
        not in page.text


async def test_result_page_foreign_user_gets_identical_404(
    client, byok_env, router_setter
):
    _uid, _pid, workflow_id = await _finished_task(
        client, router_setter, "bt-ui-owner@example.com",
        task="secret plan text", answer="secret final answer")
    assert (
        await client.get(f"/dashboard/workflows/{workflow_id}")
    ).status_code == 200

    await _login(client, "bt-ui-intruder@example.com")
    theirs = await client.get(f"/dashboard/workflows/{workflow_id}")
    unknown = await client.get("/dashboard/workflows/task-doesnotexist")
    assert theirs.status_code == unknown.status_code == 404
    assert theirs.text == unknown.text
    assert "secret plan text" not in theirs.text
    assert "secret final answer" not in theirs.text


async def test_result_page_escapes_script_in_task_and_answer(
    client, byok_env, router_setter
):
    _uid, _pid, workflow_id = await _finished_task(
        client, router_setter, "bt-ui-xss@example.com",
        task="<b>bold</b> task",
        answer="hi <script>alert(1)</script> <img src=x onerror=alert(1)>")

    page = await client.get(f"/dashboard/workflows/{workflow_id}")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "<img src=x" not in page.text
    assert "<b>bold</b>" not in page.text
    assert "&lt;b&gt;bold&lt;/b&gt;" in page.text


async def test_result_fragment_foreign_user_404_leaks_nothing(
    client, byok_env, router_setter
):
    _uid, _pid, workflow_id = await _finished_task(
        client, router_setter, "bt-ui-frag-owner@example.com",
        task="private task words", answer="private answer words")
    url = f"/dashboard/workflows/{workflow_id}"

    own = await client.get(url, headers=HX)
    assert own.status_code == 200
    assert 'id="task-live"' in own.text
    assert "<html" not in own.text  # fragment only, no nested shell
    assert "private answer words" in own.text

    await _login(client, "bt-ui-frag-other@example.com")
    theirs = await client.get(url, headers=HX)
    assert theirs.status_code == 404
    assert 'id="task-live"' not in theirs.text
    assert "private task words" not in theirs.text
    assert "private answer words" not in theirs.text


async def test_stop_polls_until_cancelled_is_persisted_then_stops(
    client, pg_engine, monkeypatch
):
    body = await _login(client, "bt-ui-stop@example.com")
    uid, pid = body["id"], body["project_id"]
    principal = Principal(user_id=uid, project_id=pid, kind="session")
    workflow_id = new_workflow_id()
    url = f"/dashboard/workflows/{workflow_id}"

    async def _slow_prepare(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(bt, "prepare_chat", _slow_prepare)
    # No engine on this bus: the runner's events stay in memory, which is
    # exactly the "terminal event not persisted yet" window. Rows are
    # seeded by hand to control when persistence "lands".
    bus = HarnessBus()
    await _seed_events(pg_engine, uid, workflow_id, [
        ("workflow.started", {"input_preview": "slow task"}),
    ])
    await app.state.sessions.save(
        workflow_id,
        [{"role": "user", "content": "slow task"}],
        user_id=uid, project_id=pid)
    task = app.state.background_tasks.start(
        uid,
        run_background_task(
            **_runner_kwargs(principal, workflow_id, harness_bus=bus)),
        workflow_id=workflow_id,
    )
    await asyncio.sleep(0.05)

    live = await client.get(url, headers=HX)
    assert live.status_code == 200
    assert "status-running" in live.text
    assert 'hx-get="' in live.text
    assert f'data-cancel-workflow="{workflow_id}"' in live.text

    stop = await client.post(
        f"/dashboard/background-tasks/{workflow_id}/cancel")
    assert stop.status_code == 202, stop.text
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus.history()[-1]["error"] == "cancelled"

    # Cancelled in memory, nothing persisted yet: still inside the grace
    # window, so the badge stays "running" and the poll keeps going.
    lagging = await client.get(url, headers=HX)
    assert lagging.status_code == 200
    assert "status-running" in lagging.text
    assert 'hx-get="' in lagging.text

    # The terminal event lands: polling stops, Stop button is gone.
    await _seed_events(pg_engine, uid, workflow_id, [
        ("workflow.failed", {"error": "cancelled"}),
    ])
    done = await client.get(url, headers=HX)
    assert done.status_code == 200
    assert 'status-badge status-cancelled">cancelled' in done.text
    assert "hx-get=" not in done.text
    assert "data-cancel-workflow" not in done.text


async def test_task_without_session_renders_timeline_and_poll_refreshes(
    client, pg_engine
):
    body = await _login(client, "bt-ui-nosession@example.com")
    uid = body["id"]
    url = "/dashboard/workflows/task-nosession"
    await _seed_events(pg_engine, uid, "task-nosession", [
        ("workflow.started", {"input_preview": "x"}),
        ("workflow.failed", {"error": "cancelled"}),
    ])

    page = await client.get(url)
    assert page.status_code == 200
    assert "workflow.started" in page.text  # normal timeline
    assert "Background task" not in page.text
    assert "Task text, tool output, and file contents are not shown" \
        in page.text

    # A late poll must reload the page, never swap a full shell in.
    poll = await client.get(url, headers=HX)
    assert poll.status_code == 200
    assert poll.headers.get("HX-Refresh") == "true"
    assert "<html" not in poll.text
