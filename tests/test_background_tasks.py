# tests/test_background_tasks.py
"""Background agent tasks: runner, registry, and the HTTP start surface.

Runner tests fake ``prepare_chat`` / ``run_agent_turn`` and assert the
workflow event contract against a real HarnessBus + Postgres. Endpoint
tests exercise the cookie-realm gate, validation, the no-provider and
per-user cap checks, the persisted audit row, and per-user workflow
isolation through the existing dashboard detail page.
"""
import asyncio
import json
import logging
import time

import httpx
import pytest
from sqlalchemy import func, select

from invincible.core import background_tasks as bt
from invincible.core import tool_executor
from invincible.core.background_tasks import (
    BackgroundTaskRegistry,
    new_workflow_id,
    run_background_task,
)
from invincible.core.chat_service import ChatError
from invincible.core.credential_store import ByokCredentialStore
from invincible.core.db import ensure_local_owner, workflow_events
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.principal import Principal
from invincible.core.tool_executor import PendingActionStore
from invincible.main import app
from tests.conftest import provider_body, register_account

# ---------------------------------------------------------------------------
# Helpers


async def _principal(pg_engine) -> Principal:
    uid, pid = await ensure_local_owner(pg_engine)
    return Principal(user_id=uid, project_id=pid, kind="session")


def _fake_stream(events):
    async def _gen():
        for event in events:
            yield event

    return _gen()


def _patch_runner(monkeypatch, *, prepare=None, events=()):
    async def _default_prepare(*args, **kwargs):
        return object()

    monkeypatch.setattr(bt, "prepare_chat", prepare or _default_prepare)
    monkeypatch.setattr(
        bt, "run_agent_turn",
        lambda *args, **kwargs: _fake_stream(events),
    )


def _types(bus) -> list[str]:
    return [event["type"] for event in bus.history()]


def _runner_kwargs(
    principal: Principal, workflow_id: str, **overrides
) -> dict:
    kwargs = {
        "task": "do the thing",
        "model": None,
        "principal": principal,
        "workflow_id": workflow_id,
        "router": object(),
        "sessions": object(),
        "memory": object(),
        "runs_store": object(),
        "pending_store": PendingActionStore(),
        "executor": None,
        "waiter": object(),
    }
    kwargs.update(overrides)
    return kwargs


async def _persisted(engine, workflow_id: str) -> list[dict]:
    async with engine.connect() as conn:
        rows = (await conn.execute(
            workflow_events.select()
            .where(workflow_events.c.workflow_id == workflow_id)
            .order_by(workflow_events.c.id)
        )).mappings().all()
    return [dict(row) for row in rows]


async def _login_with_credential(client, email, count=1):
    registered, _ = await register_account(client, email)
    assert registered.status_code == 201, registered.text
    body = registered.json()
    store = ByokCredentialStore(app.state.engine)
    for index in range(count):
        await store.create(
            user_id=body["id"],
            provider_name=f"Web{index + 1}",
            model_id=f"w{index + 1}-model",
            base_url=f"https://w{index + 1}.example.com/v1",
            api_key=f"web-key-{index + 1}",
        )
    return body["id"], body["project_id"]


def _scripted(responses):
    bodies: list[dict] = []
    state = {"index": 0}

    def handler(request):
        bodies.append(json.loads(request.content))
        response = responses[min(state["index"], len(responses) - 1)]
        state["index"] += 1
        return httpx.Response(200, json=response)

    return bodies, handler


async def _drain(registry, user_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while registry.running_count(user_id):
        if time.monotonic() > deadline:
            raise AssertionError("background task did not finish")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Runner: workflow event contract


async def test_runner_done_emits_started_and_completed(
    pg_engine, monkeypatch
):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    assert workflow_id.startswith("task-")
    assert len(workflow_id) == len("task-") + 12
    task = "t" * 500
    answer = "a" * 500
    _patch_runner(
        monkeypatch, events=[("done", {"text": answer})])
    bus = HarnessBus()
    bus.attach_engine(pg_engine)

    await run_background_task(
        **_runner_kwargs(
            principal, workflow_id,
            task=task, harness_bus=bus,
        )
    )

    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_COMPLETED.value,
    ]
    started, completed = bus.history()
    assert started["workflow_id"] == workflow_id
    assert started["user_id"] == principal.user_id
    assert started["input_preview"] == task[:200]
    assert completed["output_preview"] == answer[:200]

    await bus.flush_persisted()
    rows = await _persisted(pg_engine, workflow_id)
    assert [row["type"] for row in rows] == [
        "workflow.started", "workflow.completed"]
    assert all(row["user_id"] == principal.user_id for row in rows)


async def test_runner_error_event_emits_failed(pg_engine, monkeypatch):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    _patch_runner(
        monkeypatch, events=[("error", {"message": "upstream down"})])
    bus = HarnessBus()
    bus.attach_engine(pg_engine)

    await run_background_task(
        **_runner_kwargs(principal, workflow_id, harness_bus=bus)
    )

    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_FAILED.value,
    ]
    assert bus.history()[-1]["error"] == "upstream down"
    assert bus.history()[-1]["user_id"] == principal.user_id


async def test_runner_exception_is_logged_and_failed(
    pg_engine, monkeypatch, caplog
):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()

    async def _boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(bt, "prepare_chat", _boom)
    bus = HarnessBus()
    caplog.set_level(logging.ERROR, logger="invincible.background_tasks")

    await run_background_task(
        **_runner_kwargs(principal, workflow_id, harness_bus=bus)
    )

    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_FAILED.value,
    ]
    assert bus.history()[-1]["error"] == "kaboom"
    records = [
        record for record in caplog.records
        if record.name == "invincible.background_tasks"
    ]
    assert any(record.exc_info for record in records)


async def test_runner_timeout_emits_distinct_failed(
    pg_engine, monkeypatch
):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    monkeypatch.setattr(bt, "BACKGROUND_TASK_TIMEOUT_SECONDS", 0.05)

    async def _slow_prepare(*args, **kwargs):
        await asyncio.sleep(5)
        return object()

    monkeypatch.setattr(bt, "prepare_chat", _slow_prepare)
    bus = HarnessBus()

    await run_background_task(
        **_runner_kwargs(principal, workflow_id, harness_bus=bus)
    )

    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_FAILED.value,
    ]
    assert bus.history()[-1]["error"] == "timed out"


async def test_runner_cancellation_emits_cancelled(pg_engine, monkeypatch):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()

    async def _slow_prepare(*args, **kwargs):
        await asyncio.sleep(60)
        return object()

    monkeypatch.setattr(bt, "prepare_chat", _slow_prepare)
    bus = HarnessBus()
    bus.attach_engine(pg_engine)

    task = asyncio.ensure_future(
        run_background_task(
            **_runner_kwargs(principal, workflow_id, harness_bus=bus)
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_FAILED.value,
    ]
    assert bus.history()[-1]["error"] == "cancelled"
    await bus.flush_persisted()
    rows = await _persisted(pg_engine, workflow_id)
    assert rows[-1]["type"] == "workflow.failed"
    assert rows[-1]["payload"]["error"] == "cancelled"


async def test_runner_chat_error_has_no_traceback(
    pg_engine, monkeypatch, caplog
):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    error = ChatError(
        400,
        {"error": {"message": "no providers connected",
                   "type": "invalid_request_error"}},
    )

    async def _reject(*args, **kwargs):
        raise error

    monkeypatch.setattr(bt, "prepare_chat", _reject)
    bus = HarnessBus()
    caplog.set_level(logging.INFO, logger="invincible.background_tasks")

    await run_background_task(
        **_runner_kwargs(principal, workflow_id, harness_bus=bus)
    )

    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_FAILED.value,
    ]
    assert bus.history()[-1]["error"] == "no providers connected"
    records = [
        record for record in caplog.records
        if record.name == "invincible.background_tasks"
    ]
    assert records
    assert all(record.exc_info is None for record in records)


async def test_runner_approval_denies_token_and_fails(
    pg_engine, monkeypatch
):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    pending = PendingActionStore()
    token = pending.put(
        "execute_bash", {"command": "echo hi"},
        owner_subject=principal.user_id,
    )
    _patch_runner(monkeypatch, events=[("approval", {"token": token})])
    bus = HarnessBus()

    await run_background_task(
        **_runner_kwargs(
            principal, workflow_id, pending_store=pending,
            harness_bus=bus,
        )
    )

    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_FAILED.value,
    ]
    assert bus.history()[-1]["error"] == "approval not supported yet"
    # The staged token was declined and popped by the runner: a later
    # approval sees an unknown token, and no window is left open.
    assert await tool_executor.confirm_action(
        pending, token, True, requester_subject=principal.user_id,
    ) == {"status": "not_found"}
    assert pending.take(token, requester_subject=principal.user_id) is None


async def test_shutdown_cancels_task_and_persists_failed(
    pg_engine, monkeypatch
):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()

    async def _slow_prepare(*args, **kwargs):
        await asyncio.sleep(60)
        return object()

    monkeypatch.setattr(bt, "prepare_chat", _slow_prepare)
    bus = HarnessBus()
    bus.attach_engine(pg_engine)
    registry = BackgroundTaskRegistry()
    registry.start(
        principal.user_id,
        run_background_task(
            **_runner_kwargs(principal, workflow_id, harness_bus=bus)
        ),
    )
    await asyncio.sleep(0.05)
    assert registry.running_count(principal.user_id) == 1

    await registry.shutdown()

    assert registry.running_count(principal.user_id) == 0
    assert bus.history()[-1]["type"] == "workflow.failed"
    assert bus.history()[-1]["error"] == "cancelled"
    await bus.flush_persisted()
    rows = await _persisted(pg_engine, workflow_id)
    assert rows[-1]["type"] == "workflow.failed"
    assert rows[-1]["payload"]["error"] == "cancelled"


# ---------------------------------------------------------------------------
# Endpoint: auth, validation, provider/cap gates, success, isolation


async def test_start_requires_session(client):
    response = await client.post(
        "/dashboard/tasks", json={"task": "hello"})
    assert response.status_code == 401


async def test_start_validates_task_and_mode(client):
    await register_account(client, "tasks-validation@example.com")

    empty = await client.post("/dashboard/tasks", json={"task": "   "})
    assert empty.status_code == 400
    assert "task is required" in empty.json()["error"]["message"]

    too_long = await client.post(
        "/dashboard/tasks", json={"task": "x" * 8001})
    assert too_long.status_code == 400
    assert "at most 8000" in too_long.json()["error"]["message"]

    wrong_mode = await client.post(
        "/dashboard/tasks", json={"task": "hello", "mode": "manual"})
    assert wrong_mode.status_code == 400
    assert "mode must be plan" in wrong_mode.json()["error"]["message"]

    not_json = await client.post(
        "/dashboard/tasks", content=b"not-json",
        headers={"content-type": "application/json"})
    assert not_json.status_code == 400


async def test_start_without_provider_is_400_before_start(client):
    uid, _pid = await _login_with_credential(client, "tasks-noprov@example.com",
                                             count=0)
    response = await client.post(
        "/dashboard/tasks", json={"task": "hello"})
    assert response.status_code == 400
    assert "No AI provider is connected" in response.json()["error"]["message"]
    assert app.state.background_tasks.running_count(uid) == 0
    async with app.state.engine.connect() as conn:
        count = (await conn.execute(
            select(func.count()).select_from(workflow_events)
            .where(workflow_events.c.user_id == uid)
        )).scalar_one()
    assert count == 0


async def test_start_success_persists_events_and_answer(
    client, pg_engine, byok_env, router_setter
):
    _bodies, handler = _scripted([provider_body("w1", content="the answer")])
    router_setter({"w1.example.com": handler})
    uid, pid = await _login_with_credential(
        client, "tasks-ok@example.com")

    response = await client.post(
        "/dashboard/tasks", json={"task": "plan a trip"})
    assert response.status_code == 202, response.text
    workflow_id = response.json()["workflow_id"]
    assert workflow_id.startswith("task-")
    assert len(workflow_id) == len("task-") + 12

    await _drain(app.state.background_tasks, uid)
    await app.state.harness_bus.flush_persisted()

    rows = await _persisted(pg_engine, workflow_id)
    assert [row["type"] for row in rows] == [
        "workflow.started", "workflow.completed"]
    assert all(row["user_id"] == uid for row in rows)
    assert rows[0]["payload"]["input_preview"] == "plan a trip"
    assert rows[1]["payload"]["output_preview"] == "the answer"

    # The workflow id IS the chat session id: the full answer is readable
    # in that session under the same owner.
    history = await app.state.sessions.load(
        workflow_id, user_id=uid, project_id=pid)
    assert any(
        message.get("role") == "assistant"
        and message.get("content") == "the answer"
        for message in history
    )

    # Amendment 3: exactly one metadata-only audit row on start.
    audit_rows = await app.state.audit_log.recent(limit=50)
    started = next(
        row for row in audit_rows
        if row["action"] == "background_task.started"
        and row["resource_id"] == workflow_id
    )
    assert started["actor_user_id"] == uid
    assert started["actor_kind"] == "user"
    assert started["resource_type"] == "background_task"
    assert started["meta"] == {"workflow_id": workflow_id, "mode": "plan"}
    assert "plan a trip" not in json.dumps(audit_rows, default=str)


async def test_per_user_cap_and_other_user_unaffected(
    client, byok_env, monkeypatch
):
    import invincible.endpoints.tasks as tasks_endpoint

    async def _slow_runner(**kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(tasks_endpoint, "run_background_task", _slow_runner)
    uid_a, _pid_a = await _login_with_credential(
        client, "tasks-cap-a@example.com")
    first = await client.post("/dashboard/tasks", json={"task": "one"})
    second = await client.post("/dashboard/tasks", json={"task": "two"})
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert app.state.background_tasks.running_count(uid_a) == 2

    third = await client.post("/dashboard/tasks", json={"task": "three"})
    assert third.status_code == 429
    assert third.json()["error"]["type"] == "rate_limit_error"
    assert app.state.background_tasks.running_count(uid_a) == 2

    uid_b, _pid_b = await _login_with_credential(
        client, "tasks-cap-b@example.com")
    assert app.state.background_tasks.running_count(uid_b) == 0
    other = await client.post("/dashboard/tasks", json={"task": "mine"})
    assert other.status_code == 202, other.text
    assert app.state.background_tasks.running_count(uid_b) == 1


async def test_foreign_workflow_detail_is_404(client, pg_engine):
    uid_a, _pid_a = await _login_with_credential(
        client, "tasks-owner@example.com")
    now = time.time()
    async with pg_engine.begin() as conn:
        await conn.execute(workflow_events.insert().values(
            workflow_id="task-foreign",
            user_id=uid_a,
            type="workflow.started",
            payload={"workflow_id": "task-foreign"},
            created_at=now,
        ))
    mine = await client.get("/dashboard/workflows/task-foreign")
    assert mine.status_code == 200

    await _login_with_credential(client, "tasks-other@example.com")
    theirs = await client.get("/dashboard/workflows/task-foreign")
    assert theirs.status_code == 404
