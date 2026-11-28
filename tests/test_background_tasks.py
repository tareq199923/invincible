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
from sqlalchemy.exc import SQLAlchemyError

from invincible.core import background_tasks as bt
from invincible.core import task_records as tr
from invincible.core import tool_executor
from invincible.core.background_tasks import (
    BackgroundTaskRegistry,
    new_workflow_id,
    run_background_task,
)
from invincible.core.chat_service import ChatError, PreparedChat
from invincible.core.credential_store import ByokCredentialStore
from invincible.core.db import ensure_local_owner, workflow_events
from invincible.core.harness_approvals import ApprovalStore
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.principal import Principal
from invincible.core.session_store import SessionStore
from invincible.core.task_records import TaskRecordStore
from invincible.core.tool_executor import PendingActionStore
from invincible.core.webchat_agent import ApprovalWaiter
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


def _patch_real_agent(monkeypatch, workflow_id):
    """Keep the real run_agent_turn; fake only preparation/persistence."""
    async def _prepare(*args, **kwargs):
        return PreparedChat(
            session_id=workflow_id, session_pk=1,
            full_messages=[{"role": "user", "content": "do it"}],
            to_persist=[{"role": "user", "content": "do it"}],
        )

    async def _no_persist(*args, **kwargs):
        return None

    monkeypatch.setattr(bt, "prepare_chat", _prepare)
    monkeypatch.setattr(
        "invincible.core.webchat_agent._persist_new_turns", _no_persist)


def _types(bus) -> list[str]:
    return [event["type"] for event in bus.history()]


class _ScriptedRouter:
    """Minimal ``route_request_detailed`` for a real run_agent_turn."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.calls: list[list] = []

    async def route_request_detailed(self, messages, **kwargs):
        self.calls.append(json.loads(json.dumps(messages)))
        message = self._messages.pop(0)
        return (
            {"choices": [{"message": message}]},
            {
                "request_id": f"req-{len(self.calls)}",
                "provider_name": "test",
                "model_id": "test-model",
                "attempts": 1,
            },
        )


def _manual_router(
    command: str = "echo hi", answer: str = "finished", **tool_args
):
    """A tool-call turn followed by a text answer.

    ``tool_args`` are merged into the call's arguments (e.g. an
    out-of-schema ``timeout`` to prove the runner scrubs it).
    """
    arguments = {"command": command, **tool_args}
    return _ScriptedRouter([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "execute_bash",
                    "arguments": json.dumps(arguments),
                },
            }],
        },
        {"role": "assistant", "content": answer},
    ])


def _last_tool_result(router: _ScriptedRouter) -> dict:
    tool_messages = [
        message for message in router.calls[1]
        if message.get("role") == "tool"
    ]
    assert tool_messages
    return json.loads(tool_messages[-1]["content"])


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
    # Let the fire-and-forget workflow_events inserts finish before
    # pg_engine teardown TRUNCATEs the schema.
    await bus.flush_persisted()


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


async def test_runner_rejects_unsupported_mode_before_preparation(
    pg_engine, monkeypatch
):
    """F35 D1: core accepts only plan/manual; auto fails before any turn."""
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()

    async def _boom(*args, **kwargs):
        raise AssertionError("mode guard must run before preparation")

    monkeypatch.setattr(bt, "prepare_chat", _boom)
    bus = HarnessBus()

    await asyncio.wait_for(run_background_task(
        **_runner_kwargs(
            principal, workflow_id, harness_bus=bus, mode="auto",
        )
    ), timeout=10)

    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_FAILED.value,
    ]
    assert bus.history()[-1]["error"] == (
        "unsupported background task mode: auto")


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


# ---------------------------------------------------------------------------
# Step 5: durable task records


async def _record_for(pg_engine, principal, workflow_id, *, mode="plan"):
    store = TaskRecordStore(pg_engine)
    await store.create_running(
        workflow_id, user_id=principal.user_id, mode=mode)
    return store


async def test_runner_closes_record_completed(pg_engine, monkeypatch):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = await _record_for(pg_engine, principal, workflow_id)
    _patch_runner(monkeypatch, events=[("done", {"text": "the answer"})])
    bus = HarnessBus()

    await run_background_task(
        **_runner_kwargs(
            principal, workflow_id, harness_bus=bus, task_records=store,
        )
    )

    row = await store.get(workflow_id, user_id=principal.user_id)
    assert row["status"] == tr.COMPLETED
    assert row["finished_at"] is not None
    assert _types(bus)[-1] == HarnessEventType.WORKFLOW_COMPLETED.value


async def test_runner_closes_record_failed(pg_engine, monkeypatch):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = await _record_for(pg_engine, principal, workflow_id)
    _patch_runner(
        monkeypatch, events=[("error", {"message": "provider down"})])
    bus = HarnessBus()

    await run_background_task(
        **_runner_kwargs(
            principal, workflow_id, harness_bus=bus, task_records=store,
        )
    )

    row = await store.get(workflow_id, user_id=principal.user_id)
    assert row["status"] == tr.FAILED
    assert row["finished_at"] is not None
    assert _types(bus)[-1] == HarnessEventType.WORKFLOW_FAILED.value


async def test_runner_cancellation_uses_cancel_status(pg_engine, monkeypatch):
    """A user Stop closes ``cancelled``; a graceful shutdown closes
    ``interrupted`` - both keep the existing event shape."""
    principal = await _principal(pg_engine)

    async def _slow_prepare(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(bt, "prepare_chat", _slow_prepare)

    for expected in (tr.CANCELLED, tr.INTERRUPTED):
        workflow_id = new_workflow_id()
        store = await _record_for(pg_engine, principal, workflow_id)
        bus = HarnessBus()
        task = asyncio.ensure_future(
            run_background_task(
                **_runner_kwargs(
                    principal, workflow_id, harness_bus=bus,
                    task_records=store,
                    cancel_status=lambda expected=expected: expected,
                )
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        row = await store.get(workflow_id, user_id=principal.user_id)
        assert row["status"] == expected
        assert bus.history()[-1]["error"] == "cancelled"


async def test_runner_record_write_failure_never_masks_result(
    pg_engine, monkeypatch, caplog
):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = await _record_for(pg_engine, principal, workflow_id)

    async def _boom(*args, **kwargs):
        raise SQLAlchemyError("record write failed")

    monkeypatch.setattr(store, "mark_terminal", _boom)
    _patch_runner(monkeypatch, events=[("done", {"text": "still ok"})])
    bus = HarnessBus()

    with caplog.at_level(
        logging.WARNING, logger="invincible.background_tasks"
    ):
        await run_background_task(
            **_runner_kwargs(
                principal, workflow_id, harness_bus=bus,
                task_records=store,
            )
        )

    assert _types(bus)[-1] == HarnessEventType.WORKFLOW_COMPLETED.value
    assert "Could not persist terminal status" in caplog.text
    # The row is untouched (still running) - the result was not masked.
    row = await store.get(workflow_id, user_id=principal.user_id)
    assert row["status"] == tr.RUNNING


async def test_runner_links_session_to_record(pg_engine, monkeypatch):
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = await _record_for(pg_engine, principal, workflow_id)
    sessions = SessionStore(engine=pg_engine)
    session_pk = await sessions.resolve_or_create(
        workflow_id,
        user_id=principal.user_id,
        project_id=principal.project_id,
    )

    class _Prepared:
        pass

    prepared = _Prepared()
    prepared.session_pk = session_pk

    async def _prepare(*args, **kwargs):
        return prepared

    monkeypatch.setattr(bt, "prepare_chat", _prepare)
    monkeypatch.setattr(
        bt, "run_agent_turn",
        lambda *args, **kwargs: _fake_stream([("done", {"text": "ok"})]),
    )

    await run_background_task(
        **_runner_kwargs(
            principal, workflow_id, task_records=store,
        )
    )

    row = await store.get(workflow_id, user_id=principal.user_id)
    assert row["session_pk"] == session_pk
    assert row["status"] == tr.COMPLETED


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


async def test_manual_task_suspends_then_approve_executes_once(
    pg_engine, monkeypatch
):
    """F35 B: stage -> wait -> approve -> execute exactly once."""
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = ApprovalStore(pg_engine)
    waiter = ApprovalWaiter()
    bus = HarnessBus()
    router = _manual_router()
    executed: list = []

    async def executor(action_type, args):
        executed.append((action_type, dict(args)))
        return {"status": "ok", "output": "hi"}

    audit_calls: list[tuple[str, dict | None]] = []

    async def audit(action, meta=None):
        audit_calls.append((action, meta))

    _patch_real_agent(monkeypatch, workflow_id)
    staged: dict = {}

    async def approver():
        for _ in range(500):
            await asyncio.sleep(0.01)
            rows = await store.pending_for_workflow(
                workflow_id, owner_subject=principal.user_id)
            if not rows:
                continue
            staged.update(rows[0])
            assert waiter.resolve(
                rows[0]["token"], principal.user_id, True, durable=True)
            return
        raise AssertionError("approval was never staged")

    task = asyncio.create_task(run_background_task(
        **_runner_kwargs(
            principal, workflow_id, router=router, waiter=waiter,
            executor=executor, harness_bus=bus, mode="manual",
            approval_store=store, audit=audit,
        )
    ))
    approver_task = asyncio.create_task(approver())
    await asyncio.wait_for(task, timeout=10)
    await asyncio.wait_for(approver_task, timeout=10)

    assert staged["type"] == "execute_bash"
    # D6: one hour, not the store's 24h default.
    remaining = staged["deadline"] - time.time()
    assert 3500 < remaining <= bt.BACKGROUND_APPROVAL_TTL_SECONDS
    # D2/D3: exactly one execution, through the paired-machine executor.
    assert executed == [
        ("execute_bash", {"command": "echo hi", "timeout": 30.0})]
    # The model got the real result, not a synthetic acknowledgement.
    assert _last_tool_result(router)["output"] == "hi"
    assert await store.pending_for_workflow(
        workflow_id, owner_subject=principal.user_id) == []
    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.APPROVAL_REQUESTED.value,
        HarnessEventType.APPROVAL_RESOLVED.value,
        HarnessEventType.WORKFLOW_COMPLETED.value,
    ]
    # D9: metadata only - the command never reaches the event stream.
    requested = bus.history()[1]
    assert requested["name"] == "execute_bash"
    assert "echo hi" not in json.dumps(requested)
    assert audit_calls == [(
        "background_task.approval_requested",
        {"workflow_id": workflow_id, "action": "execute_bash"},
    )]
    assert "echo hi" not in json.dumps(audit_calls)
    assert bus.history()[2]["approved"] is True
    # D2: replaying the approval finds no row (single execution).
    assert await store.resolve(
        staged["token"], approve=True,
        requester_subject=principal.user_id) is None
    assert len(executed) == 1


async def test_manual_task_ignores_out_of_schema_timeout(
    pg_engine, monkeypatch
):
    """F35 D2: the model cannot widen execute_bash's 30s timeout.

    ``execute_bash`` exposes only ``command``; both approval paths must
    scrub an out-of-schema ``timeout`` before staging.
    """
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = ApprovalStore(pg_engine)
    waiter = ApprovalWaiter()
    bus = HarnessBus()
    router = _manual_router(timeout=600)
    executed: list = []

    async def executor(action_type, args):
        executed.append((action_type, dict(args)))
        return {"status": "ok"}

    _patch_real_agent(monkeypatch, workflow_id)

    async def approver():
        for _ in range(500):
            await asyncio.sleep(0.01)
            rows = await store.pending_for_workflow(
                workflow_id, owner_subject=principal.user_id)
            if not rows:
                continue
            assert waiter.resolve(
                rows[0]["token"], principal.user_id, True, durable=True)
            return
        raise AssertionError("approval was never staged")

    task = asyncio.create_task(run_background_task(
        **_runner_kwargs(
            principal, workflow_id, router=router, waiter=waiter,
            executor=executor, harness_bus=bus, mode="manual",
            approval_store=store,
        )
    ))
    approver_task = asyncio.create_task(approver())
    await asyncio.wait_for(task, timeout=10)
    await asyncio.wait_for(approver_task, timeout=10)

    assert executed == [
        ("execute_bash", {"command": "echo hi", "timeout": 30.0})]


async def test_manual_task_deny_is_a_tool_result_not_a_failure(
    pg_engine, monkeypatch
):
    """F35 D5: deny feeds {"status": "declined"} and the turn continues."""
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = ApprovalStore(pg_engine)
    waiter = ApprovalWaiter()
    bus = HarnessBus()
    router = _manual_router(answer="done after deny")
    executed: list = []

    async def executor(action_type, args):
        executed.append((action_type, dict(args)))
        return {"status": "ok"}

    _patch_real_agent(monkeypatch, workflow_id)

    async def approver():
        for _ in range(500):
            await asyncio.sleep(0.01)
            rows = await store.pending_for_workflow(
                workflow_id, owner_subject=principal.user_id)
            if not rows:
                continue
            assert waiter.resolve(
                rows[0]["token"], principal.user_id, False, durable=True)
            return
        raise AssertionError("approval was never staged")

    task = asyncio.create_task(run_background_task(
        **_runner_kwargs(
            principal, workflow_id, router=router, waiter=waiter,
            executor=executor, harness_bus=bus, mode="manual",
            approval_store=store,
        )
    ))
    approver_task = asyncio.create_task(approver())
    await asyncio.wait_for(task, timeout=10)
    await asyncio.wait_for(approver_task, timeout=10)

    assert executed == []
    assert _last_tool_result(router) == {"status": "declined"}
    assert await store.pending_for_workflow(
        workflow_id, owner_subject=principal.user_id) == []
    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.APPROVAL_REQUESTED.value,
        HarnessEventType.APPROVAL_RESOLVED.value,
        HarnessEventType.WORKFLOW_COMPLETED.value,
    ]
    assert bus.history()[2]["approved"] is False


async def test_manual_task_denylisted_command_never_stages(
    pg_engine, monkeypatch
):
    """F35 D2: denylist runs first - no row, no approval, turn continues."""
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = ApprovalStore(pg_engine)
    waiter = ApprovalWaiter()
    bus = HarnessBus()
    router = _manual_router(
        command="sudo rm -rf /", answer="cannot do that")
    executed: list = []
    audit_calls: list = []

    async def executor(action_type, args):
        executed.append((action_type, dict(args)))
        return {"status": "ok"}

    async def audit(action, **meta):
        audit_calls.append((action, meta))

    _patch_real_agent(monkeypatch, workflow_id)

    await asyncio.wait_for(run_background_task(
        **_runner_kwargs(
            principal, workflow_id, router=router, waiter=waiter,
            executor=executor, harness_bus=bus, mode="manual",
            approval_store=store, audit=audit,
        )
    ), timeout=10)

    assert _last_tool_result(router)["status"] == "blocked"
    assert executed == []
    assert await store.pending_for_workflow(
        workflow_id, owner_subject=principal.user_id) == []
    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_COMPLETED.value,
    ]
    # D9: audit is metadata only - never the command.
    assert audit_calls[0][0] == "background_task.tool.blocked"
    assert audit_calls[0][1] == {"meta": {
        "action": "execute_bash", "call_id": "call-1"}}
    assert "sudo rm -rf /" not in json.dumps(audit_calls)


async def test_manual_task_without_executor_answers_unavailable(
    pg_engine, monkeypatch
):
    """F35 D3: no paired agent -> "unavailable" before staging.

    There is nowhere safe to run, so the action must not park and wait
    for an approval that could never execute. The model gets a normal
    tool result and the workflow completes.
    """
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = ApprovalStore(pg_engine)
    waiter = ApprovalWaiter()
    bus = HarnessBus()
    router = _manual_router(answer="no machine")
    audit_calls: list = []

    _patch_real_agent(monkeypatch, workflow_id)

    async def audit(action, **meta):
        audit_calls.append((action, meta))

    await asyncio.wait_for(run_background_task(
        **_runner_kwargs(
            principal, workflow_id, router=router, waiter=waiter,
            executor=None, harness_bus=bus, mode="manual",
            approval_store=store, audit=audit,
        )
    ), timeout=10)

    result = _last_tool_result(router)
    assert result["status"] == "unavailable"
    assert result["reason"] == tool_executor.MUTATING_UNAVAILABLE_REASON
    assert await store.pending_for_workflow(
        workflow_id, owner_subject=principal.user_id) == []
    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_COMPLETED.value,
    ]
    assert HarnessEventType.APPROVAL_REQUESTED.value not in _types(bus)
    assert HarnessEventType.APPROVAL_RESOLVED.value not in _types(bus)
    assert not [
        action for action, _meta in audit_calls
        if action.startswith("background_task.approval")
    ]
    assert "echo hi" not in json.dumps(audit_calls)


def test_effective_approval_deadline_uses_the_earlier_bound():
    started = 1_000_000.0
    task_cutoff = (
        started + bt.BACKGROUND_TASK_TIMEOUT_SECONDS
        - bt.APPROVAL_SAFETY_MARGIN_SECONDS
    )
    # Row deadline first.
    assert bt.effective_approval_deadline(
        task_cutoff - 100, started) == task_cutoff - 100
    # Task clock first.
    assert bt.effective_approval_deadline(
        task_cutoff + 100, started) == task_cutoff
    # Missing row deadline still gets the task cutoff.
    assert bt.effective_approval_deadline(None, started) == task_cutoff
    # Missing task start leaves the row deadline alone.
    assert bt.effective_approval_deadline(task_cutoff, None) == task_cutoff
    assert bt.effective_approval_deadline(None, None) is None
    assert bt.effective_approval_deadline(
        task_cutoff + 1_000, started,
        task_timeout_seconds=300) == started + 300 - 60


async def test_adapter_wait_is_bounded_by_the_task_clock():
    calls: list = []

    class FakeWaiter:
        async def wait_decision(
            self, token, user_id, *, timeout=None, durable=False
        ):
            calls.append((token, user_id, timeout, durable))
            return True

    adapter = bt.DurableApprovalAdapter(
        object(), FakeWaiter(), workflow_id="task-wait",
        owner_subject=7)
    adapter._task_deadline = time.time() + 5
    assert await adapter.wait("tok") is True
    _token, user_id, timeout, durable = calls[-1]
    assert (user_id, durable) == (7, True)
    assert 0 < timeout <= 5

    adapter._ttl_seconds = 2
    assert await adapter.wait("tok") is True
    assert 0 < calls[-1][2] <= 2

    adapter._task_deadline = time.time() - 1
    assert await adapter.wait("tok") is None
    assert len(calls) == 2


async def test_manual_task_window_closed_before_staging_fails(
    pg_engine, monkeypatch
):
    """F35 D6: inside the safety margin nothing stages and nothing runs."""
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = ApprovalStore(pg_engine)
    waiter = ApprovalWaiter()
    bus = HarnessBus()
    router = _manual_router()
    executed: list = []

    _patch_real_agent(monkeypatch, workflow_id)
    # Adapter construction computes now + 5 - 60: already closed.
    monkeypatch.setattr(bt, "BACKGROUND_TASK_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(bt, "APPROVAL_SAFETY_MARGIN_SECONDS", 60)

    async def executor(action_type, args):
        executed.append((action_type, dict(args)))
        return {"status": "ok"}

    await asyncio.wait_for(run_background_task(
        **_runner_kwargs(
            principal, workflow_id, router=router, waiter=waiter,
            executor=executor, harness_bus=bus, mode="manual",
            approval_store=store,
        )
    ), timeout=10)

    assert executed == []
    assert await store.pending_for_workflow(
        workflow_id, owner_subject=principal.user_id) == []
    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.WORKFLOW_FAILED.value,
    ]
    assert HarnessEventType.APPROVAL_REQUESTED.value not in _types(bus)
    assert bus.history()[-1]["error"] == "approval timed out"


async def test_manual_task_approval_timeout_fails_and_leaves_no_row(
    pg_engine, monkeypatch
):
    """F35 D6: a bounded wait fails the task and deletes the row."""
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = ApprovalStore(pg_engine)
    waiter = ApprovalWaiter()
    bus = HarnessBus()
    router = _manual_router()

    _patch_real_agent(monkeypatch, workflow_id)
    monkeypatch.setattr(bt, "BACKGROUND_APPROVAL_TTL_SECONDS", 0.05)

    async def executor(_action_type, _args):
        return {"status": "ok"}

    await asyncio.wait_for(run_background_task(
        **_runner_kwargs(
            principal, workflow_id, router=router, waiter=waiter,
            executor=executor, harness_bus=bus, mode="manual",
            approval_store=store,
        )
    ), timeout=10)

    assert _types(bus) == [
        HarnessEventType.WORKFLOW_STARTED.value,
        HarnessEventType.APPROVAL_REQUESTED.value,
        HarnessEventType.WORKFLOW_FAILED.value,
    ]
    assert bus.history()[-1]["error"] == "approval timed out"
    assert await store.pending_for_workflow(
        workflow_id, owner_subject=principal.user_id) == []


async def test_manual_task_cancel_purges_the_suspended_row(
    pg_engine, monkeypatch
):
    """F35 D7/D8: a suspended task is live for the cap and cleanup runs."""
    principal = await _principal(pg_engine)
    workflow_id = new_workflow_id()
    store = ApprovalStore(pg_engine)
    waiter = ApprovalWaiter()
    bus = HarnessBus()
    router = _manual_router()
    registry = BackgroundTaskRegistry()

    _patch_real_agent(monkeypatch, workflow_id)

    async def executor(_action_type, _args):
        return {"status": "ok"}

    task = registry.start(
        principal.user_id,
        run_background_task(
            **_runner_kwargs(
                principal, workflow_id, router=router, waiter=waiter,
                executor=executor, harness_bus=bus, mode="manual",
                approval_store=store,
            )
        ),
        workflow_id=workflow_id,
    )

    token = None
    for _ in range(500):
        await asyncio.sleep(0.01)
        rows = await store.pending_for_workflow(
            workflow_id, owner_subject=principal.user_id)
        if (rows and HarnessEventType.APPROVAL_REQUESTED.value
                in _types(bus)):
            token = rows[0]["token"]
            break
    assert token is not None
    # D8: suspended is still running, so it still counts toward the cap.
    assert registry.running_count(principal.user_id) == 1

    assert registry.cancel(principal.user_id, workflow_id) is True
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await store.resolve(
        token, approve=True, requester_subject=principal.user_id) is None
    assert await store.pending_for_workflow(
        workflow_id, owner_subject=principal.user_id) == []
    assert _types(bus)[-1] == HarnessEventType.WORKFLOW_FAILED.value
    assert bus.history()[-1]["error"] == "cancelled"


async def test_registry_cancel_is_owner_and_workflow_scoped():
    registry = BackgroundTaskRegistry()
    started = asyncio.Event()

    async def _wait():
        started.set()
        await asyncio.sleep(3600)

    task = registry.start(1, _wait(), workflow_id="task-a")
    await started.wait()
    assert registry.is_running(1, "task-a")
    assert not registry.is_running(2, "task-a")
    assert not registry.is_running(1, "task-b")

    # Another user (and an unknown workflow id) never reaches this task.
    assert registry.cancel(2, "task-a") is False
    assert registry.is_running(1, "task-a")
    assert registry.cancel(1, "task-b") is False
    assert registry.is_running(1, "task-a")

    assert registry.cancel(1, "task-a") is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not registry.is_running(1, "task-a")
    assert registry.running_count(1) == 0
    assert registry.cancel(1, "task-a") is False  # finished


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
        workflow_id=workflow_id,
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
        "/dashboard/background-tasks", json={"task": "hello"})
    assert response.status_code == 401


async def test_start_validates_task_and_mode(client):
    await register_account(client, "tasks-validation@example.com")

    empty = await client.post("/dashboard/background-tasks", json={"task": "   "})
    assert empty.status_code == 400
    assert "task is required" in empty.json()["error"]["message"]

    too_long = await client.post(
        "/dashboard/background-tasks", json={"task": "x" * 8001})
    assert too_long.status_code == 400
    assert "at most 8000" in too_long.json()["error"]["message"]

    wrong_mode = await client.post(
        "/dashboard/background-tasks", json={"task": "hello", "mode": "auto"})
    assert wrong_mode.status_code == 400
    assert "mode must be plan or manual" in \
        wrong_mode.json()["error"]["message"]

    empty_mode = await client.post(
        "/dashboard/background-tasks", json={"task": "hello", "mode": ""})
    assert empty_mode.status_code == 400
    assert "mode must be plan or manual" in \
        empty_mode.json()["error"]["message"]

    not_json = await client.post(
        "/dashboard/background-tasks", content=b"not-json",
        headers={"content-type": "application/json"})
    assert not_json.status_code == 400


async def test_start_without_provider_is_400_before_start(client):
    uid, _pid = await _login_with_credential(client, "tasks-noprov@example.com",
                                             count=0)
    response = await client.post(
        "/dashboard/background-tasks", json={"task": "hello"})
    assert response.status_code == 400
    assert "No AI provider is connected" in response.json()["error"]["message"]
    assert app.state.background_tasks.running_count(uid) == 0
    async with app.state.engine.connect() as conn:
        count = (await conn.execute(
            select(func.count()).select_from(workflow_events)
            .where(workflow_events.c.user_id == uid)
        )).scalar_one()
    assert count == 0


async def test_start_manual_requires_store_but_plan_does_not(
    client, byok_env, monkeypatch
):
    uid, _pid = await _login_with_credential(
        client, "tasks-store@example.com")
    monkeypatch.setattr(app.state, "approval_store", None)

    manual = await client.post(
        "/dashboard/background-tasks",
        json={"task": "do it", "mode": "manual"})
    assert manual.status_code == 503, manual.text
    error = manual.json()["detail"]["error"]
    assert error["type"] == "config_error"
    assert "approval_store" in error["message"]

    plan = await client.post(
        "/dashboard/background-tasks",
        json={"task": "plan it", "mode": "plan"})
    assert plan.status_code == 202, plan.text
    await _drain(app.state.background_tasks, uid)


async def test_start_success_persists_events_and_answer(
    client, pg_engine, byok_env, router_setter
):
    _bodies, handler = _scripted([provider_body("w1", content="the answer")])
    router_setter({"w1.example.com": handler})
    uid, pid = await _login_with_credential(
        client, "tasks-ok@example.com")

    response = await client.post(
        "/dashboard/background-tasks", json={"task": "plan a trip"})
    assert response.status_code == 202, response.text
    workflow_id = response.json()["workflow_id"]
    assert workflow_id.startswith("task-")
    assert len(workflow_id) == len("task-") + 12

    await _drain(app.state.background_tasks, uid)
    await app.state.harness_bus.flush_persisted()

    # Plan mode is read-only by construction: the provider request must
    # offer read tools but never a mutating tool. Step 4 will relax this
    # deliberately, so pin the promise now.
    offered = {
        tool["function"]["name"]
        for body in _bodies
        for tool in body.get("tools") or []
    }
    assert "read_file" in offered
    assert offered.isdisjoint({"execute_bash", "write_file", "edit_file"})

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

    # Step 5: the durable row is linked to that session and closed.
    record = await app.state.task_records.get(workflow_id, user_id=uid)
    assert record["status"] == tr.COMPLETED
    assert record["mode"] == "plan"
    assert record["finished_at"] is not None
    session_pk = await app.state.sessions.resolve_or_create(
        workflow_id, user_id=uid, project_id=pid)
    assert record["session_pk"] == session_pk

    # Amendment 3: exactly one metadata-only audit row on start.
    audit_rows = await app.state.audit_log.recent(limit=50)
    started_rows = [
        row for row in audit_rows
        if row["action"] == "background_task.started"
    ]
    assert len(started_rows) == 1
    started = started_rows[0]
    assert started["resource_id"] == workflow_id
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
    first = await client.post("/dashboard/background-tasks", json={"task": "one"})
    second = await client.post("/dashboard/background-tasks", json={"task": "two"})
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert app.state.background_tasks.running_count(uid_a) == 2

    third = await client.post("/dashboard/background-tasks", json={"task": "three"})
    assert third.status_code == 429
    assert third.json()["error"]["type"] == "rate_limit_error"
    assert app.state.background_tasks.running_count(uid_a) == 2

    uid_b, _pid_b = await _login_with_credential(
        client, "tasks-cap-b@example.com")
    assert app.state.background_tasks.running_count(uid_b) == 0
    other = await client.post("/dashboard/background-tasks", json={"task": "mine"})
    assert other.status_code == 202, other.text
    assert app.state.background_tasks.running_count(uid_b) == 1


@pytest.mark.usefixtures("byok_env")
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


@pytest.mark.usefixtures("byok_env")
async def test_cancel_endpoint_persists_cancelled_and_one_audit(
    client, pg_engine, monkeypatch
):
    uid, pid = await _login_with_credential(
        client, "tasks-cancel@example.com")
    principal = Principal(user_id=uid, project_id=pid, kind="session")
    workflow_id = new_workflow_id()

    async def _slow_prepare(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(bt, "prepare_chat", _slow_prepare)
    bus = HarnessBus()
    bus.attach_engine(pg_engine)
    records = TaskRecordStore(pg_engine)
    await records.create_running(workflow_id, user_id=uid, mode="plan")
    task = app.state.background_tasks.start(
        uid,
        run_background_task(
            **_runner_kwargs(
                principal, workflow_id, harness_bus=bus,
                task_records=records,
                cancel_status=app.state.background_tasks.cancel_status,
            )
        ),
        workflow_id=workflow_id,
    )
    await asyncio.sleep(0.05)
    assert app.state.background_tasks.is_running(uid, workflow_id)
    assert (await records.get(
        workflow_id, user_id=uid))["status"] == tr.RUNNING

    response = await client.post(
        f"/dashboard/background-tasks/{workflow_id}/cancel")
    assert response.status_code == 202, response.text
    assert response.json() == {"status": "cancelling"}

    with pytest.raises(asyncio.CancelledError):
        await task
    await bus.flush_persisted()

    # A user Stop closes the durable row ``cancelled`` (only a graceful
    # shutdown writes ``interrupted``).
    row = await records.get(workflow_id, user_id=uid)
    assert row["status"] == tr.CANCELLED
    assert row["finished_at"] is not None

    # Exactly one terminal event from the existing CancelledError path -
    # the endpoint adds no second event.
    rows = await _persisted(pg_engine, workflow_id)
    assert [row["type"] for row in rows] == [
        "workflow.started", "workflow.failed"]
    assert rows[-1]["payload"]["error"] == "cancelled"

    audit_rows = [
        row for row in await app.state.audit_log.recent(limit=50)
        if row["action"] == "background_task.cancelled"
    ]
    assert len(audit_rows) == 1
    assert audit_rows[0]["resource_id"] == workflow_id
    assert audit_rows[0]["actor_user_id"] == uid
    assert audit_rows[0]["meta"] == {"workflow_id": workflow_id}


@pytest.mark.usefixtures("byok_env")
async def test_cancel_endpoint_404_is_identical_and_leaves_foreign_running(
    client,
):
    uid_a, _pid_a = await _login_with_credential(
        client, "tasks-cancel-owner@example.com")

    async def _finish():
        return None

    finished_id = new_workflow_id()
    finished = app.state.background_tasks.start(
        uid_a, _finish(), workflow_id=finished_id)
    await finished

    held = asyncio.Event()

    async def _hold():
        held.set()
        await asyncio.sleep(3600)

    foreign_id = new_workflow_id()
    foreign = app.state.background_tasks.start(
        uid_a, _hold(), workflow_id=foreign_id)
    await held.wait()

    await _login_with_credential(client, "tasks-cancel-other@example.com")
    unknown = await client.post(
        "/dashboard/background-tasks/task-unknown/cancel")
    done = await client.post(
        f"/dashboard/background-tasks/{finished_id}/cancel")
    other = await client.post(
        f"/dashboard/background-tasks/{foreign_id}/cancel")

    assert unknown.status_code == done.status_code == other.status_code == 404
    assert unknown.json() == done.json() == other.json()
    # User B's request must not touch user A's still-running task.
    assert app.state.background_tasks.is_running(uid_a, foreign_id)
    assert not [
        row for row in await app.state.audit_log.recent(limit=50)
        if row["action"] == "background_task.cancelled"
    ]

    app.state.background_tasks.cancel(uid_a, foreign_id)
    with pytest.raises(asyncio.CancelledError):
        await foreign


# ---------------------------------------------------------------------------
# Step 5: start-path records and Retry


async def _seed_interrupted(
    engine, workflow_id, uid, pid, *, mode="plan", text="original task",
):
    store = TaskRecordStore(engine)
    await store.create_running(workflow_id, user_id=uid, mode=mode)
    await store.mark_terminal(workflow_id, tr.INTERRUPTED)
    if text is not None:
        await app.state.sessions.save(
            workflow_id,
            [{"role": "user", "content": text}],
            user_id=uid,
            project_id=pid,
        )
    return store


async def test_start_inserts_running_row_and_shutdown_interrupts(
    client, byok_env, monkeypatch
):
    uid, _pid = await _login_with_credential(
        client, "tasks-row-live@example.com")

    async def _slow_prepare(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(bt, "prepare_chat", _slow_prepare)
    response = await client.post(
        "/dashboard/background-tasks", json={"task": "slow task"})
    assert response.status_code == 202, response.text
    workflow_id = response.json()["workflow_id"]

    row = await app.state.task_records.get(workflow_id, user_id=uid)
    assert row["status"] == tr.RUNNING
    assert row["mode"] == "plan"
    assert row["finished_at"] is None

    await app.state.background_tasks.shutdown()
    row = await app.state.task_records.get(workflow_id, user_id=uid)
    assert row["status"] == tr.INTERRUPTED
    assert row["finished_at"] is not None


async def test_start_record_failure_fails_closed(
    client, byok_env, monkeypatch
):
    uid, _pid = await _login_with_credential(
        client, "tasks-row-down@example.com")

    async def _boom(*args, **kwargs):
        raise SQLAlchemyError("record store down")

    monkeypatch.setattr(
        app.state.task_records, "create_running", _boom)
    response = await client.post(
        "/dashboard/background-tasks", json={"task": "hello"})
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["type"] == "config_error"
    assert "storage is unavailable" in error["message"]
    assert app.state.background_tasks.running_count(uid) == 0


async def test_start_scheduling_failure_marks_row_failed(
    client, byok_env, monkeypatch
):
    uid, _pid = await _login_with_credential(
        client, "tasks-schedule-fail@example.com")

    def _boom(*args, **kwargs):
        raise RuntimeError("no event loop")

    monkeypatch.setattr(app.state.background_tasks, "start", _boom)
    response = await client.post(
        "/dashboard/background-tasks", json={"task": "hello"})
    assert response.status_code == 503, response.text
    assert response.json()["error"]["type"] == "config_error"

    rows = await app.state.task_records.list_for_user(uid)
    assert len(rows) == 1
    assert rows[0]["status"] == tr.FAILED
    assert rows[0]["finished_at"] is not None


async def test_start_cap_is_rechecked_after_record_insert(
    client, byok_env, monkeypatch
):
    uid, _pid = await _login_with_credential(
        client, "tasks-cap-recheck@example.com")
    original = app.state.task_records.create_running

    async def _insert_then_fill(*args, **kwargs):
        await original(*args, **kwargs)

        async def _hold():
            await asyncio.sleep(3600)

        for _ in range(2):
            app.state.background_tasks.start(
                uid, _hold(), workflow_id=new_workflow_id())

    monkeypatch.setattr(
        app.state.task_records, "create_running", _insert_then_fill)
    response = await client.post(
        "/dashboard/background-tasks", json={"task": "hello"})
    assert response.status_code == 429, response.text
    assert response.json()["error"]["type"] == "rate_limit_error"

    rows = await app.state.task_records.list_for_user(uid)
    assert len(rows) == 1
    assert rows[0]["status"] == tr.FAILED
    await app.state.background_tasks.shutdown()


async def test_retry_requires_json_content_type(client, byok_env):
    uid, pid = await _login_with_credential(
        client, "tasks-retry-ct@example.com")
    workflow_id = "task-retry-ct"
    await _seed_interrupted(
        app.state.engine, workflow_id, uid, pid)

    response = await client.post(
        f"/dashboard/background-tasks/{workflow_id}/retry",
        content=b"{}",
        headers={"content-type": "text/plain"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_retry_unknown_and_foreign_are_identical_404(
    client, byok_env
):
    uid, pid = await _login_with_credential(
        client, "tasks-retry-owner@example.com")
    workflow_id = "task-retry-foreign"
    await _seed_interrupted(
        app.state.engine, workflow_id, uid, pid)

    other_id, _other_pid = await _login_with_credential(
        client, "tasks-retry-other@example.com")
    foreign = await client.post(
        f"/dashboard/background-tasks/{workflow_id}/retry", json={})
    unknown = await client.post(
        "/dashboard/background-tasks/task-retry-missing/retry", json={})
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    assert foreign.json() == {
        "error": {
            "message": "No such background task.",
            "type": "not_found_error",
        }
    }
    assert app.state.background_tasks.running_count(other_id) == 0
    # The owner's row is untouched by the foreign attempt.
    row = await app.state.task_records.get(workflow_id, user_id=uid)
    assert row["status"] == tr.INTERRUPTED


@pytest.mark.parametrize("status", [None, tr.COMPLETED, tr.CANCELLED])
async def test_retry_only_interrupted_is_retryable(
    client, byok_env, status
):
    uid, _pid = await _login_with_credential(
        client, f"tasks-retry-{status or 'running'}@example.com")
    workflow_id = f"task-retry-{status or 'running'}"
    store = TaskRecordStore(app.state.engine)
    await store.create_running(workflow_id, user_id=uid, mode="plan")
    if status is not None:
        await store.mark_terminal(workflow_id, status)

    response = await client.post(
        f"/dashboard/background-tasks/{workflow_id}/retry", json={})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert (await store.get(
        workflow_id, user_id=uid))["status"] == (status or tr.RUNNING)
    assert app.state.background_tasks.running_count(uid) == 0


async def test_retry_without_task_text_is_409_and_starts_nothing(
    client, byok_env
):
    uid, pid = await _login_with_credential(
        client, "tasks-retry-notext@example.com")
    workflow_id = "task-retry-notext"
    store = await _seed_interrupted(
        app.state.engine, workflow_id, uid, pid, text=None)

    response = await client.post(
        f"/dashboard/background-tasks/{workflow_id}/retry", json={})
    assert response.status_code == 409, response.text
    assert "no longer available" in response.json()["error"]["message"]
    assert app.state.background_tasks.running_count(uid) == 0
    rows = await store.list_for_user(uid)
    assert [row["workflow_id"] for row in rows] == [workflow_id]
    assert rows[0]["status"] == tr.INTERRUPTED


async def test_retry_oversized_task_text_is_409(client, byok_env):
    uid, pid = await _login_with_credential(
        client, "tasks-retry-long@example.com")
    workflow_id = "task-retry-long"
    store = await _seed_interrupted(
        app.state.engine, workflow_id, uid, pid, text="x" * 8001)

    response = await client.post(
        f"/dashboard/background-tasks/{workflow_id}/retry", json={})
    assert response.status_code == 409, response.text
    assert "8000" in response.json()["error"]["message"]
    assert app.state.background_tasks.running_count(uid) == 0
    assert (await store.get(
        workflow_id, user_id=uid))["status"] == tr.INTERRUPTED


async def test_retry_without_provider_is_400_and_leaves_row(client):
    uid, pid = await _login_with_credential(
        client, "tasks-retry-noprov@example.com", count=0)
    workflow_id = "task-retry-noprov"
    store = await _seed_interrupted(
        app.state.engine, workflow_id, uid, pid)

    response = await client.post(
        f"/dashboard/background-tasks/{workflow_id}/retry", json={})
    assert response.status_code == 400, response.text
    assert "No AI provider is connected" in response.json()["error"]["message"]
    assert app.state.background_tasks.running_count(uid) == 0
    rows = await store.list_for_user(uid)
    assert [row["workflow_id"] for row in rows] == [workflow_id]
    assert rows[0]["status"] == tr.INTERRUPTED


async def test_retry_starts_new_task_and_leaves_old_untouched(
    client, byok_env, router_setter
):
    _bodies, handler = _scripted([
        provider_body("w1", content="retried answer")])
    router_setter({"w1.example.com": handler})
    uid, pid = await _login_with_credential(
        client, "tasks-retry-ok@example.com")
    old_id = "task-retry-old"
    store = await _seed_interrupted(
        app.state.engine, old_id, uid, pid,
        mode="manual", text="original task")
    old_before = await store.get(old_id, user_id=uid)

    # A pending approval belongs to the OLD workflow only. Retry must not
    # reuse it, move it, or expose it on the new task.
    old_token = await app.state.approval_store.suspend(
        workflow_id=old_id,
        action_type="execute_bash",
        args={"command": "echo old"},
        owner_subject=uid,
    )

    response = await client.post(
        f"/dashboard/background-tasks/{old_id}/retry", json={})
    assert response.status_code == 202, response.text
    new_id = response.json()["workflow_id"]
    assert new_id != old_id
    assert new_id.startswith("task-")
    assert len(new_id) == len("task-") + 12

    await _drain(app.state.background_tasks, uid)
    await app.state.harness_bus.flush_persisted()

    # The old row and its timeline are untouched.
    assert await store.get(old_id, user_id=uid) == old_before
    assert await _persisted(app.state.engine, old_id) == []
    assert await app.state.approval_store.pending_for_workflow(
        old_id, owner_subject=uid) != []
    assert await app.state.approval_store.pending_for_workflow(
        new_id, owner_subject=uid) == []

    new_row = await store.get(new_id, user_id=uid)
    assert new_row["status"] == tr.COMPLETED
    assert new_row["mode"] == "manual"
    assert new_row["finished_at"] is not None

    result = await app.state.sessions.task_result(new_id, user_id=uid)
    assert result["task_text"] == "original task"
    assert result["answer"] == "retried answer"

    audit = [
        row for row in await app.state.audit_log.recent(limit=200)
        if row["action"] in (
            "background_task.started", "background_task.retried")
    ]
    started = [
        row for row in audit
        if row["action"] == "background_task.started"
        and row["resource_id"] == new_id
    ]
    retried = [
        row for row in audit if row["action"] == "background_task.retried"
    ]
    assert len(started) == 1
    assert len(retried) == 1
    assert retried[0]["resource_id"] == old_id
    assert retried[0]["meta"] == {
        "workflow_id": old_id,
        "new_workflow_id": new_id,
        "mode": "manual",
    }
    assert old_token not in json.dumps(audit, default=str)
