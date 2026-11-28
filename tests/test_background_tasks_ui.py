# tests/test_background_tasks_ui.py
"""Background-task dashboard UI: start form, status column, result page.

The HTTP surface is the real cookie-realm dashboard; workflow events and
sessions are seeded directly in Postgres so each assertion is about the
rendered result, not about the runner (covered by test_background_tasks).
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from invincible.core import background_tasks as bt
from invincible.core.background_tasks import (
    new_workflow_id,
    run_background_task,
)
from invincible.core.db import workflow_events
from invincible.core.harness_approvals import ApprovalStore
from invincible.core.harness_bus import HarnessBus
from invincible.core.principal import Principal
from invincible.core.task_records import COMPLETED, INTERRUPTED
from invincible.main import app
from tests.conftest import provider_body, register_account
from tests.test_background_tasks import (
    _drain,
    _login_with_credential,
    _runner_kwargs,
    _scripted,
)
from tests.test_webchat_agent import fn_call, tool_body

HX = {"HX-Request": "true"}


async def _login(client, email):
    registered, _ = await register_account(client, email)
    assert registered.status_code == 201, registered.text
    return registered.json()


async def _seed_events(engine, user_id, workflow_id, events, *, base=None):
    now = time.time() if base is None else base
    async with engine.begin() as conn:
        for offset, (etype, payload) in enumerate(events):
            await conn.execute(workflow_events.insert().values(
                workflow_id=workflow_id,
                user_id=user_id,
                type=etype,
                payload=payload,
                created_at=now + offset,
            ))


def _running_for(monkeypatch, *workflow_ids):
    wanted = set(workflow_ids)
    monkeypatch.setattr(
        app.state.background_tasks, "is_running",
        lambda user_id, workflow_id: workflow_id in wanted)


async def _save_task_session(workflow_id, uid, pid, text="do it"):
    await app.state.sessions.save(
        workflow_id,
        [{"role": "user", "content": text}],
        user_id=uid,
        project_id=pid,
    )


async def _suspend(store, workflow_id, uid, action_type, args, **kwargs):
    return await store.suspend(
        workflow_id=workflow_id,
        action_type=action_type,
        args=args,
        owner_subject=uid,
        **kwargs,
    )


async def test_workflows_page_has_start_form(client):
    await _login(client, "bt-ui-form@example.com")
    page = await client.get("/dashboard/workflows")
    assert page.status_code == 200
    assert 'id="background-task-form"' in page.text
    assert 'maxlength="8000"' in page.text
    assert "0 / 8000" in page.text
    assert 'id="background-task-mode"' in page.text
    assert '<option value="plan" selected>' in page.text
    assert '<option value="manual">' in page.text
    assert "Plan runs read-only" in page.text
    assert "INVINCIBLE_AGENT_ROUTING=1" in page.text
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


async def test_owner_detail_renders_full_args_and_escapes_controls(
    client, pg_engine, monkeypatch
):
    """Owner-only detail: full command/edit, safe write preview, no-store."""
    body = await _login(client, "bt-ui-detail@example.com")
    uid, pid = body["id"], body["project_id"]
    workflow_id = "task-detail-args"
    await _seed_events(pg_engine, uid, workflow_id, [
        ("workflow.started", {"input_preview": "inspect"}),
    ])
    await _save_task_session(workflow_id, uid, pid)
    _running_for(monkeypatch, workflow_id)
    store = ApprovalStore(pg_engine)

    command = "echo \u202e" + "x" * 600 + "\u200b\t\nnext\u00a0line"
    write_content = "w" * 600
    old = "old-" + "a" * 400
    new = "new-" + "b" * 400
    bash_token = await _suspend(
        store, workflow_id, uid, "execute_bash", {"command": command})
    write_token = await _suspend(
        store, workflow_id, uid, "write_file",
        {"path": "notes/out.txt", "content": write_content})
    edit_token = await _suspend(
        store, workflow_id, uid, "edit_file",
        {"path": "notes/edit.txt", "old_string": old,
         "new_string": new})
    foreign_token = await _suspend(
        store, workflow_id, uid + 1, "execute_bash",
        {"command": "FOREIGN_SECRET_COMMAND"})

    page = await client.get(f"/dashboard/workflows/{workflow_id}")
    assert page.status_code == 200
    assert page.headers.get("Cache-Control") == "no-store"
    assert (
        'status-badge status-awaiting-approval">awaiting approval'
        in page.text
    )
    assert "Pending approvals" in page.text
    for token in (bash_token, write_token, edit_token):
        assert f'data-approval-token="{token}"' in page.text
    assert foreign_token not in page.text
    assert "FOREIGN_SECRET_COMMAND" not in page.text
    assert "_owner_subject" not in page.text

    # Full command (not the 200-char summary), with controls escaped.
    assert (
        "echo &lt;U+202E&gt;" + "x" * 600
        + "&lt;U+200B&gt;\\t\nnext&lt;U+00A0&gt;line"
        in page.text
    )
    assert "\u202e" not in page.text
    assert "Contains non-ASCII characters; check the" in page.text
    assert page.text.count("Contains non-ASCII") == 1

    # write_file keeps path + byte count + the 500-char preview.
    assert "notes/out.txt" in page.text
    assert "w" * 500 in page.text
    assert "w" * 501 not in page.text
    assert "(600 bytes total)" in page.text

    # edit_file shows the whole old/new pair on the owner detail page.
    assert f"-{old}\n+{new}" in page.text


async def test_owner_detail_hides_cards_past_the_task_cutoff(
    client, pg_engine, monkeypatch
):
    """The effective deadline (task clock minus margin) is enforced in UI."""
    body = await _login(client, "bt-ui-detail-late@example.com")
    uid, pid = body["id"], body["project_id"]
    workflow_id = "task-detail-late"
    await _seed_events(
        pg_engine, uid, workflow_id,
        [("workflow.started", {"input_preview": "inspect"})],
        base=time.time() - 7200,
    )
    await _save_task_session(workflow_id, uid, pid)
    _running_for(monkeypatch, workflow_id)
    store = ApprovalStore(pg_engine)
    token = await _suspend(
        store, workflow_id, uid, "execute_bash",
        {"command": "STILL_PENDING_COMMAND"})
    rows = await store.pending_for_workflow(
        workflow_id, owner_subject=uid)
    assert [row["token"] for row in rows] == [token]

    page = await client.get(f"/dashboard/workflows/{workflow_id}")
    assert page.status_code == 200
    assert page.headers.get("Cache-Control") == "no-store"
    assert token not in page.text
    assert "STILL_PENDING_COMMAND" not in page.text
    assert "Pending approvals" not in page.text
    assert "The approval window has closed" in page.text
    assert 'status-badge status-running">running' in page.text


async def test_workflows_list_counts_only_visible_approvals(
    client, pg_engine, monkeypatch
):
    body = await _login(client, "bt-ui-list-approvals@example.com")
    uid = body["id"]
    now = time.time()
    visible = "task-list-visible"
    hidden = "task-list-hidden"
    await _seed_events(
        pg_engine, uid, visible,
        [("workflow.started", {"input_preview": "visible"})], base=now)
    await _seed_events(
        pg_engine, uid, hidden,
        [("workflow.started", {"input_preview": "hidden"})],
        base=now - 7200)
    _running_for(monkeypatch, visible, hidden)
    store = ApprovalStore(pg_engine)
    await _suspend(
        store, visible, uid, "execute_bash",
        {"command": "SECRET_VISIBLE_COMMAND"})
    await _suspend(
        store, hidden, uid, "execute_bash",
        {"command": "SECRET_HIDDEN_COMMAND"})

    page = await client.get("/dashboard/workflows")
    assert page.status_code == 200
    assert page.headers.get("Cache-Control") == "no-store"
    assert page.text.count("1 awaiting approval") == 1
    assert 'status-badge status-awaiting-approval">awaiting approval' \
        in page.text
    assert 'status-badge status-running">running' in page.text
    assert visible in page.text and hidden in page.text
    assert "SECRET_VISIBLE_COMMAND" not in page.text
    assert "SECRET_HIDDEN_COMMAND" not in page.text
    assert "_owner_subject" not in page.text
    assert '<pre class="approval-detail">' not in page.text


async def test_approvals_page_is_metadata_only_and_owner_scoped(
    client, pg_engine
):
    owner = await _login(client, "bt-ui-approvals-a@example.com")
    uid, _pid = owner["id"], owner["project_id"]
    store = ApprovalStore(pg_engine)
    now = time.time()
    wf_bash = "task-appr-bash"
    wf_write = "task-appr-write"
    wf_expired = "task-appr-expired"
    for workflow_id in (wf_bash, wf_write):
        await _seed_events(
            pg_engine, uid, workflow_id,
            [("workflow.started", {"input_preview": "x"})], base=now)
    await _seed_events(
        pg_engine, uid, wf_expired,
        [("workflow.started", {"input_preview": "x"})],
        base=now - 7200)

    bash_token = await _suspend(
        store, wf_bash, uid, "execute_bash",
        {"command": "TOP_SECRET_BASH"})
    write_token = await _suspend(
        store, wf_write, uid, "write_file",
        {"path": "top/secret.txt", "content": "TOP_SECRET_CONTENT"})
    expired_token = await _suspend(
        store, wf_expired, uid, "execute_bash",
        {"command": "EXPIRED_SECRET"})

    other = await _login(client, "bt-ui-approvals-b@example.com")
    other_wf = "task-appr-other"
    await _seed_events(
        pg_engine, other["id"], other_wf,
        [("workflow.started", {"input_preview": "x"})], base=now)
    other_token = await _suspend(
        store, other_wf, other["id"], "execute_bash",
        {"command": "OTHER_USER_SECRET"})

    other_page = await client.get("/dashboard/approvals")
    assert other_page.status_code == 200
    assert other_page.headers.get("Cache-Control") == "no-store"
    assert other_wf in other_page.text
    assert wf_bash not in other_page.text
    assert "OTHER_USER_SECRET" not in other_page.text

    relogin = await client.post(
        "/auth/login",
        json={"email": "bt-ui-approvals-a@example.com",
              "password": "longenough1"})
    assert relogin.status_code == 200, relogin.text
    page = await client.get("/dashboard/approvals")
    assert page.status_code == 200
    assert page.headers.get("Cache-Control") == "no-store"
    assert wf_bash in page.text and wf_write in page.text
    assert wf_expired not in page.text
    assert other_wf not in page.text
    assert "execute_bash" in page.text and "write_file" in page.text
    for secret in (
        "TOP_SECRET_BASH", "TOP_SECRET_CONTENT", "top/secret.txt",
        "EXPIRED_SECRET", "OTHER_USER_SECRET",
        bash_token, write_token, expired_token, other_token,
    ):
        assert secret not in page.text
    assert "_owner_subject" not in page.text
    assert "data-approval-decision" not in page.text
    assert "data-approval-token" not in page.text

    await _login(client, "bt-ui-approvals-c@example.com")
    empty = await client.get("/dashboard/approvals")
    assert empty.status_code == 200
    assert empty.headers.get("Cache-Control") == "no-store"
    assert "No approvals are waiting" in empty.text


async def test_owner_detail_with_pending_row_still_404s_foreign(
    client, pg_engine
):
    owner = await _login(client, "bt-ui-pending-owner@example.com")
    workflow_id = "task-pending-foreign"
    await _seed_events(pg_engine, owner["id"], workflow_id, [
        ("workflow.started", {"input_preview": "private"}),
    ])
    await _save_task_session(
        workflow_id, owner["id"], owner["project_id"])
    store = ApprovalStore(pg_engine)
    token = await _suspend(
        store, workflow_id, owner["id"], "execute_bash",
        {"command": "FOREIGN_DETAIL_SECRET"})

    await _login(client, "bt-ui-pending-other@example.com")
    url = f"/dashboard/workflows/{workflow_id}"
    theirs = await client.get(url)
    unknown = await client.get("/dashboard/workflows/task-doesnotexist")
    assert theirs.status_code == unknown.status_code == 404
    assert theirs.text == unknown.text
    assert token not in theirs.text
    assert "FOREIGN_DETAIL_SECRET" not in theirs.text
    fragment = await client.get(url, headers=HX)
    assert fragment.status_code == 404
    assert token not in fragment.text
    assert "FOREIGN_DETAIL_SECRET" not in fragment.text


async def test_background_approval_route_owner_matrix_and_audit(
    client, pg_engine
):
    body = await _login(client, "bt-ui-decide@example.com")
    uid = body["id"]
    workflow_id = "task-decide"
    await _seed_events(pg_engine, uid, workflow_id, [
        ("workflow.started", {"input_preview": "x"}),
    ])
    store = ApprovalStore(pg_engine)
    token = await _suspend(
        store, workflow_id, uid, "execute_bash",
        {"command": "ROUTE_SECRET_COMMAND"})
    assert app.state.webchat_approvals.register(
        token, uid, durable=True)
    url = f"/dashboard/background-tasks/{workflow_id}/approvals/{token}"

    unknown = await client.post(
        f"/dashboard/background-tasks/{workflow_id}"
        "/approvals/not-a-token",
        json={"approve": True})
    assert unknown.status_code == 404
    not_found = unknown.json()

    for payload in ({}, {"approve": "yes"}, {"approve": 1},
                    {"approve": None}):
        bad = await client.post(url, json=payload)
        assert bad.status_code == 400, payload
        assert bad.json()["error"]["type"] == "invalid_request_error"
    not_json = await client.post(
        url, content=b"not-json",
        headers={"content-type": "application/json"})
    assert not_json.status_code == 400

    ok = await client.post(url, json={"approve": True})
    assert ok.status_code == 202, ok.text
    assert ok.json() == {"ok": True, "approved": True}
    again = await client.post(url, json={"approve": True})
    assert again.status_code == 404
    assert again.json() == not_found

    deny_token = await _suspend(
        store, workflow_id, uid, "write_file",
        {"path": "deny.txt", "content": "nope"})
    assert app.state.webchat_approvals.register(
        deny_token, uid, durable=True)
    deny = await client.post(
        f"/dashboard/background-tasks/{workflow_id}"
        f"/approvals/{deny_token}",
        json={"approve": False})
    assert deny.status_code == 202, deny.text
    assert deny.json() == {"ok": True, "approved": False}

    audit = [
        row for row in await app.state.audit_log.recent(limit=200)
        if row["action"] == "background_task.approval_decided"
    ]
    assert len(audit) == 2
    by_choice = {row["meta"]["approved"]: row for row in audit}
    assert by_choice[True]["resource_id"] == workflow_id
    assert by_choice[True]["meta"] == {
        "workflow_id": workflow_id,
        "action": "execute_bash",
        "approved": True,
    }
    assert by_choice[False]["meta"] == {
        "workflow_id": workflow_id,
        "action": "write_file",
        "approved": False,
    }
    flattened = json.dumps(audit, default=str)
    assert "ROUTE_SECRET_COMMAND" not in flattened
    assert token not in flattened
    assert deny_token not in flattened

    await _login(client, "bt-ui-decide-other@example.com")
    foreign_token = await _suspend(
        store, workflow_id, uid, "execute_bash",
        {"command": "FOREIGN_ROW_COMMAND"})
    foreign = await client.post(
        f"/dashboard/background-tasks/{workflow_id}"
        f"/approvals/{foreign_token}",
        json={"approve": True})
    assert foreign.status_code == 404
    assert foreign.json() == not_found
    assert "FOREIGN_ROW_COMMAND" not in foreign.text

    client.cookies.clear()
    anonymous = await client.post(
        f"/dashboard/background-tasks/{workflow_id}"
        f"/approvals/{foreign_token}",
        json={"approve": True})
    assert anonymous.status_code == 401


async def test_background_approval_route_rejects_expired_and_door_mismatch(
    client, pg_engine
):
    body = await _login(client, "bt-ui-decide-doors@example.com")
    uid = body["id"]
    store = ApprovalStore(pg_engine)
    now = time.time()

    late_wf = "task-decide-late"
    await _seed_events(
        pg_engine, uid, late_wf,
        [("workflow.started", {"input_preview": "x"})],
        base=now - 7200)
    late_token = await _suspend(
        store, late_wf, uid, "execute_bash",
        {"command": "LATE_COMMAND"})
    assert app.state.webchat_approvals.register(
        late_token, uid, durable=True)
    unknown = await client.post(
        f"/dashboard/background-tasks/{late_wf}"
        "/approvals/not-a-token",
        json={"approve": True})
    not_found = unknown.json()
    late = await client.post(
        f"/dashboard/background-tasks/{late_wf}"
        f"/approvals/{late_token}",
        json={"approve": True})
    assert late.status_code == 404
    assert late.json() == not_found
    assert await store.pending_for_workflow(
        late_wf, owner_subject=uid) != []

    door_wf = "task-decide-door"
    await _seed_events(
        pg_engine, uid, door_wf,
        [("workflow.started", {"input_preview": "x"})], base=now)
    door_token = await _suspend(
        store, door_wf, uid, "execute_bash",
        {"command": "DOOR_COMMAND"})
    assert app.state.webchat_approvals.register(
        door_token, uid, durable=False)
    wrong_door = await client.post(
        f"/dashboard/background-tasks/{door_wf}"
        f"/approvals/{door_token}",
        json={"approve": True})
    assert wrong_door.status_code == 404
    assert wrong_door.json() == not_found
    assert await store.pending_for_workflow(
        door_wf, owner_subject=uid) != []
    assert app.state.webchat_approvals.resolve(
        door_token, uid, True, durable=False) is True

    row_wf = "task-decide-row-expired"
    await _seed_events(
        pg_engine, uid, row_wf,
        [("workflow.started", {"input_preview": "x"})], base=now)
    row_token = await _suspend(
        store, row_wf, uid, "execute_bash",
        {"command": "ROW_EXPIRED_COMMAND"},
        now=now - 7200, ttl_seconds=60)
    assert app.state.webchat_approvals.register(
        row_token, uid, durable=True)
    row_expired = await client.post(
        f"/dashboard/background-tasks/{row_wf}"
        f"/approvals/{row_token}",
        json={"approve": True})
    assert row_expired.status_code == 404
    assert row_expired.json() == not_found

    fast_wf = "task-decide-fast"
    await _seed_events(
        pg_engine, uid, fast_wf,
        [("workflow.started", {"input_preview": "x"})], base=now)
    fast_token = await _suspend(
        store, fast_wf, uid, "execute_bash",
        {"command": "FAST_DOOR_COMMAND"})
    assert app.state.webchat_approvals.register(
        fast_token, uid, durable=True)
    fast_route = await client.post(
        "/dashboard/chat/approve",
        json={"token": fast_token, "approve": True})
    assert fast_route.status_code == 404
    assert await store.pending_for_workflow(
        fast_wf, owner_subject=uid) != []


async def test_manual_task_route_start_approve_and_finish(
    client, byok_env, router_setter, monkeypatch
):
    executed: list = []

    async def executor(action_type, args):
        executed.append((action_type, dict(args)))
        return {"status": "ok", "output": "hi"}

    monkeypatch.setattr(
        "invincible.endpoints.tasks.build_agent_executor",
        lambda *args, **kwargs: executor,
    )
    _bodies, handler = _scripted([
        tool_body("w1", [
            fn_call("call-1", "execute_bash", {"command": "echo hi"}),
        ]),
        provider_body("w1", content="the final answer"),
    ])
    router_setter({"w1.example.com": handler})
    uid, _pid = await _login_with_credential(
        client, "bt-ui-manual-e2e@example.com")

    started = await client.post(
        "/dashboard/background-tasks",
        json={"task": "run echo", "mode": "manual"})
    assert started.status_code == 202, started.text
    workflow_id = started.json()["workflow_id"]

    rows: list[dict] = []
    for _ in range(500):
        await asyncio.sleep(0.01)
        rows = await app.state.approval_store.pending_for_owner(uid)
        rows = [
            row for row in rows
            if row["workflow_id"] == workflow_id
        ]
        if rows:
            break
    assert rows, "the manual task never staged an approval"
    token = rows[0]["token"]

    requested_audit: list[dict] = []
    for _ in range(500):
        requested_audit = [
            row for row in await app.state.audit_log.recent(limit=200)
            if row["action"] == "background_task.approval_requested"
        ]
        if requested_audit:
            break
        await asyncio.sleep(0.01)
    assert len(requested_audit) == 1
    assert requested_audit[0]["resource_type"] == "background_task"
    assert requested_audit[0]["resource_id"] == workflow_id
    assert requested_audit[0]["meta"] == {
        "workflow_id": workflow_id,
        "action": "execute_bash",
    }
    assert "echo hi" not in json.dumps(requested_audit, default=str)

    pending_page = await client.get(
        f"/dashboard/workflows/{workflow_id}")
    assert pending_page.status_code == 200
    assert 'status-badge status-awaiting-approval">awaiting approval' \
        in pending_page.text
    assert f'data-approval-token="{token}"' in pending_page.text
    assert "echo hi" in pending_page.text
    assert pending_page.headers.get("Cache-Control") == "no-store"

    decision = await client.post(
        f"/dashboard/background-tasks/{workflow_id}"
        f"/approvals/{token}",
        json={"approve": True})
    assert decision.status_code == 202, decision.text

    await _drain(app.state.background_tasks, uid)
    await app.state.harness_bus.flush_persisted()
    assert len(executed) == 1
    action, args = executed[0]
    assert action == "execute_bash"
    assert args["command"] == "echo hi"
    assert await app.state.approval_store.pending_for_workflow(
        workflow_id, owner_subject=uid) == []

    final_page = await client.get(
        f"/dashboard/workflows/{workflow_id}")
    assert final_page.status_code == 200
    assert 'status-badge status-completed">completed' in final_page.text
    assert "the final answer" in final_page.text
    assert "Pending approvals" not in final_page.text
    assert token not in final_page.text

    audit = [
        row for row in await app.state.audit_log.recent(limit=200)
        if row["action"] == "background_task.approval_decided"
    ]
    assert len(audit) == 1
    assert audit[0]["meta"] == {
        "workflow_id": workflow_id,
        "action": "execute_bash",
        "approved": True,
    }
    assert "echo hi" not in json.dumps(audit, default=str)


async def test_interrupted_task_shows_badge_retry_and_no_stop(
    client, pg_engine
):
    body = await _login(client, "bt-ui-interrupted@example.com")
    uid, pid = body["id"], body["project_id"]
    workflow_id = "task-ui-interrupted"
    await _seed_events(pg_engine, uid, workflow_id, [
        ("workflow.started", {"input_preview": "original task"}),
        ("workflow.failed", {"error": "cancelled"}),
    ])
    await _save_task_session(workflow_id, uid, pid, text="original task")
    await app.state.task_records.create_running(
        workflow_id, user_id=uid, mode="plan")
    await app.state.task_records.mark_terminal(workflow_id, INTERRUPTED)

    page = await client.get(f"/dashboard/workflows/{workflow_id}")
    assert page.status_code == 200
    assert 'status-badge status-interrupted">interrupted' in page.text
    assert "interrupted by a" in page.text
    assert "server restart" in page.text
    assert f'data-retry-workflow="{workflow_id}"' in page.text
    assert "data-cancel-workflow" not in page.text
    assert "hx-get=" not in page.text
    assert "Pending approvals" not in page.text


async def test_interrupted_task_without_text_hides_retry(
    client, pg_engine
):
    body = await _login(client, "bt-ui-interrupted-notext@example.com")
    uid = body["id"]
    workflow_id = "task-ui-interrupted-notext"
    await app.state.task_records.create_running(
        workflow_id, user_id=uid, mode="plan")
    await app.state.task_records.mark_terminal(workflow_id, INTERRUPTED)

    page = await client.get(f"/dashboard/workflows/{workflow_id}")
    assert page.status_code == 200
    assert 'status-badge status-interrupted">interrupted' in page.text
    assert "original task text is no longer available" in page.text
    assert "data-retry-workflow" not in page.text
    assert "data-cancel-workflow" not in page.text


async def test_workflows_list_merges_durable_rows_and_events(
    client, pg_engine
):
    body = await _login(client, "bt-ui-merge@example.com")
    uid = body["id"]
    store = app.state.task_records

    await _seed_events(pg_engine, uid, "task-merge-events", [
        ("workflow.started", {"input_preview": "x"}),
        ("workflow.completed", {"output_preview": "y"}),
    ])
    await store.create_running(
        "task-merge-events", user_id=uid, mode="plan")
    await store.mark_terminal("task-merge-events", COMPLETED)
    await store.create_running(
        "task-merge-durable", user_id=uid, mode="plan")
    await store.mark_terminal("task-merge-durable", INTERRUPTED)

    page = await client.get("/dashboard/workflows")
    assert page.status_code == 200
    # One row per workflow id: the durable row and the event row merge.
    assert page.text.count(
        'href="/dashboard/workflows/task-merge-events"') == 1
    assert "task-merge-durable" in page.text
    assert 'status-badge status-completed">completed' in page.text
    assert 'status-badge status-interrupted">interrupted' in page.text

    other = await _login(client, "bt-ui-merge-other@example.com")
    await store.create_running(
        "task-merge-foreign", user_id=other["id"], mode="plan")
    theirs = await client.get("/dashboard/workflows")
    assert theirs.status_code == 200
    assert "task-merge-foreign" in theirs.text
    assert "task-merge-events" not in theirs.text
    assert "task-merge-durable" not in theirs.text


async def test_approvals_page_skips_terminal_durable_tasks(
    client, pg_engine
):
    body = await _login(client, "bt-ui-appr-interrupted@example.com")
    uid = body["id"]
    workflow_id = "task-appr-interrupted"
    await _seed_events(pg_engine, uid, workflow_id, [
        ("workflow.started", {"input_preview": "x"}),
    ])
    token = await _suspend(
        app.state.approval_store, workflow_id, uid, "execute_bash",
        {"command": "INTERRUPTED_SECRET_COMMAND"})
    await app.state.task_records.create_running(
        workflow_id, user_id=uid, mode="manual")
    await app.state.task_records.mark_terminal(workflow_id, INTERRUPTED)

    page = await client.get("/dashboard/approvals")
    assert page.status_code == 200
    assert "No approvals are waiting" in page.text
    assert workflow_id not in page.text
    assert token not in page.text
    assert "INTERRUPTED_SECRET_COMMAND" not in page.text

    detail = await client.get(f"/dashboard/workflows/{workflow_id}")
    assert detail.status_code == 200
    assert 'status-badge status-interrupted">interrupted' in detail.text
    assert "Pending approvals" not in detail.text
    assert token not in detail.text
    assert "INTERRUPTED_SECRET_COMMAND" not in detail.text


def test_background_task_static_js_contract():
    script = (
        Path(__file__).resolve().parent.parent
        / "invincible/templates/static/background-tasks.js"
    ).read_text(encoding="utf-8")
    assert 'mode: mode ? mode.value : "plan"' in script
    assert "var resolvedTokens = {};" in script
    assert 'document.addEventListener("htmx:afterSwap", ' \
        "applyResolvedState);" in script
    assert '"/approvals/"' in script
    assert 'body: JSON.stringify({approve: decision === "approve"})' \
        in script
    assert 'credentials: "same-origin"' in script
    # Step 5: Retry is a delegated handler that starts a NEW task.
    assert 'closest("[data-retry-workflow]")' in script
    assert '"/retry"' in script
    assert '"Content-Type": "application/json"' in script
    assert 'body: "{}"' in script
    assert "Could not retry the task" in script
