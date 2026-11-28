# tests/test_task_records.py
"""Durable background-task records (Step 5): store acceptance.

Real PostgreSQL through the shared ``pg_engine``; ownership, terminal
writes, and the startup sweep are asserted at the store boundary.
"""
import pytest

from invincible.core import task_records as tr
from invincible.main import app
from tests.conftest import register_account


async def _account(client, email):
    response, _ = await register_account(client, email)
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_get_and_owner_scope(client):
    owner = await _account(client, "records-owner@example.com")
    other = await _account(client, "records-other@example.com")
    store = app.state.task_records

    await store.create_running(
        "task-one", user_id=owner["id"], mode="plan", now=100.0)

    row = await store.get("task-one", user_id=owner["id"])
    assert row["workflow_id"] == "task-one"
    assert row["user_id"] == owner["id"]
    assert row["session_pk"] is None
    assert row["mode"] == "plan"
    assert row["status"] == tr.RUNNING
    assert row["created_at"] == row["started_at"] == 100.0
    assert row["finished_at"] is None

    # A workflow id is never a capability.
    assert await store.get("task-one", user_id=other["id"]) is None
    assert await store.get("task-missing", user_id=owner["id"]) is None
    assert [
        item["workflow_id"]
        for item in await store.list_for_user(owner["id"])
    ] == ["task-one"]
    assert await store.list_for_user(other["id"]) == []


async def test_create_rejects_unknown_mode(client):
    owner = await _account(client, "records-mode@example.com")
    with pytest.raises(ValueError):
        await app.state.task_records.create_running(
            "task-auto", user_id=owner["id"], mode="auto")


async def test_attach_session_is_owner_scoped(client):
    owner = await _account(client, "records-link-owner@example.com")
    other = await _account(client, "records-link-other@example.com")
    store = app.state.task_records
    await store.create_running(
        "task-link", user_id=owner["id"], mode="plan")
    session_pk = await app.state.sessions.resolve_or_create(
        "task-link",
        user_id=owner["id"],
        project_id=owner["project_id"],
    )

    assert await store.attach_session(
        "task-link", session_pk, user_id=other["id"]) is False
    assert (await store.get(
        "task-link", user_id=owner["id"]))["session_pk"] is None

    assert await store.attach_session(
        "task-link", session_pk, user_id=owner["id"]) is True
    assert (await store.get(
        "task-link", user_id=owner["id"]))["session_pk"] == session_pk


@pytest.mark.parametrize(
    "status",
    [tr.COMPLETED, tr.FAILED, tr.CANCELLED, tr.INTERRUPTED],
)
async def test_mark_terminal_all_statuses_and_idempotent(client, status):
    owner = await _account(client, f"records-{status}@example.com")
    store = app.state.task_records
    workflow_id = f"task-{status}"
    await store.create_running(
        workflow_id, user_id=owner["id"], mode="manual")

    assert await store.mark_terminal(
        workflow_id, status, finished_at=200.0) is True
    row = await store.get(workflow_id, user_id=owner["id"])
    assert row["status"] == status
    assert row["finished_at"] == 200.0

    # The running predicate makes a second close a no-op, even when the
    # requested status is the same one.
    assert await store.mark_terminal(
        workflow_id, tr.FAILED, finished_at=300.0) is False
    assert (await store.get(
        workflow_id, user_id=owner["id"]))["status"] == status


async def test_mark_terminal_rejects_non_terminal_status(client):
    owner = await _account(client, "records-terminal-guard@example.com")
    store = app.state.task_records
    await store.create_running(
        "task-guard", user_id=owner["id"], mode="plan")

    with pytest.raises(ValueError):
        await store.mark_terminal("task-guard", tr.RUNNING)
    with pytest.raises(ValueError):
        await store.mark_terminal("task-guard", "awaiting approval")


async def test_sweep_marks_only_running_rows_interrupted(client):
    owner = await _account(client, "records-sweep-a@example.com")
    other = await _account(client, "records-sweep-b@example.com")
    store = app.state.task_records

    await store.create_running(
        "task-a-running", user_id=owner["id"], mode="plan")
    await store.create_running(
        "task-b-running", user_id=other["id"], mode="manual")
    await store.create_running(
        "task-a-done", user_id=owner["id"], mode="plan")
    await store.mark_terminal("task-a-done", tr.COMPLETED)
    await store.create_running(
        "task-a-failed", user_id=owner["id"], mode="plan")
    await store.mark_terminal("task-a-failed", tr.FAILED)

    assert await store.mark_running_interrupted(now=500.0) == 2

    for workflow_id, account in (
        ("task-a-running", owner),
        ("task-b-running", other),
    ):
        row = await store.get(workflow_id, user_id=account["id"])
        assert row["status"] == tr.INTERRUPTED
        assert row["finished_at"] == 500.0

    done = await store.get("task-a-done", user_id=owner["id"])
    assert done["status"] == tr.COMPLETED
    assert done["finished_at"] != 500.0
    failed = await store.get("task-a-failed", user_id=owner["id"])
    assert failed["status"] == tr.FAILED
    assert failed["finished_at"] != 500.0

    # A second sweep has nothing left to do.
    assert await store.mark_running_interrupted(now=600.0) == 0


async def test_list_for_user_newest_first_and_bounded(client):
    owner = await _account(client, "records-list@example.com")
    store = app.state.task_records
    for offset, workflow_id in enumerate(
        ("task-old", "task-mid", "task-new")
    ):
        await store.create_running(
            workflow_id,
            user_id=owner["id"],
            mode="plan",
            now=100.0 + offset,
        )

    assert [
        item["workflow_id"]
        for item in await store.list_for_user(owner["id"])
    ] == ["task-new", "task-mid", "task-old"]
    assert [
        item["workflow_id"]
        for item in await store.list_for_user(owner["id"], limit=2)
    ] == ["task-new", "task-mid"]
