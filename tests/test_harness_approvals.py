# tests/test_harness_approvals.py
"""H5: durable approvals + workflow-event persistence (needs Postgres).

- ApprovalStore suspend/resolve round-trips, indistinguishability,
  single-use, expiry, crash-resume (a fresh store instance resolves),
  and fast/slow-path separation.
- PendingActionStore.load_persisted skips slow-path rows (regression
  pin for the H5 column fix).
- HarnessBus persists workflow-scoped events only.
"""
from sqlalchemy import text

from invincible.core.harness_approvals import ApprovalStore
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.tool_executor import PendingActionStore


async def test_suspend_resolve_round_trip(pg_engine):
    store = ApprovalStore(pg_engine)
    token = await store.suspend(
        workflow_id="wf1", action_type="execute_bash",
        args={"command": "echo hi", "timeout": 30.0},
        owner_subject=42, now=1000.0,
    )
    assert token
    record = await store.resolve(
        token, approve=True, requester_subject=42, now=1000.0)
    assert record["type"] == "execute_bash"
    assert record["args"] == {"command": "echo hi", "timeout": 30.0}
    assert record["workflow_id"] == "wf1"
    assert "_owner_subject" not in record["args"]
    # Single-use: second resolve looks exactly like unknown.
    assert await store.resolve(
        token, approve=True, requester_subject=42) is None


async def test_deny_discards_without_record(pg_engine):
    store = ApprovalStore(pg_engine)
    token = await store.suspend(
        workflow_id="wf1", action_type="write_file",
        args={"path": "a", "content": "b"}, owner_subject=7, now=1000.0)
    out = await store.resolve(
        token, approve=False, requester_subject=7, now=1000.0)
    assert out == {"status": "declined", "workflow_id": "wf1"}
    assert await store.resolve(
        token, approve=False, requester_subject=7) is None


async def test_indistinguishable_rejections(pg_engine):
    """Unknown, wrong-subject, subject-less-vs-subject, and fast-path
    tokens all answer None — existence never leaks."""
    store = ApprovalStore(pg_engine)
    assert await store.resolve(
        "nope", approve=True, requester_subject=1) is None

    token = await store.suspend(
        workflow_id="w", action_type="execute_bash", args={"command": "x"},
        owner_subject=42, now=1000.0)
    assert await store.resolve(
        token, approve=True, requester_subject=43, now=1000.0) is None
    # ... while the owner still resolves afterwards (no consumption).
    assert (await store.resolve(
        token, approve=True, requester_subject=42,
        now=1000.0))["workflow_id"] == "w"

    # Subject-less row invisible to subjects (fail closed).
    token2 = await store.suspend(
        workflow_id="w", action_type="execute_bash", args={"command": "y"},
        owner_subject=None, now=1000.0)
    assert await store.resolve(
        token2, approve=True, requester_subject=1, now=1000.0) is None


async def test_expired_resolves_as_unknown_and_sweeps(pg_engine):
    store = ApprovalStore(pg_engine)
    token = await store.suspend(
        workflow_id="w", action_type="execute_bash", args={"command": "x"},
        owner_subject=1, now=1000.0, ttl_seconds=60.0)
    assert await store.resolve(
        token, approve=True, requester_subject=1, now=2000.0) is None
    # ... and a second resolve is still None (row was deleted).
    assert await store.resolve(
        token, approve=True, requester_subject=1, now=2000.0) is None

    token2 = await store.suspend(
        workflow_id="w", action_type="execute_bash", args={"command": "y"},
        owner_subject=1, now=1000.0, ttl_seconds=60.0)
    assert await store.sweep_expired(now=1000.0) == 0
    assert await store.sweep_expired(now=2000.0) == 1
    assert await store.resolve(
        token2, approve=True, requester_subject=1, now=2000.0) is None


async def test_crash_resume_fresh_instance_resolves(pg_engine):
    """Suspend, drop the store (simulated crash), resolve from a new one —
    the slow path survives restarts, unlike the in-memory fast path."""
    token = await ApprovalStore(pg_engine).suspend(
        workflow_id="wf9", action_type="execute_bash",
        args={"command": "echo hi"}, owner_subject=5, now=1000.0)
    record = await ApprovalStore(pg_engine).resolve(
        token, approve=True, requester_subject=5, now=9000.0)
    assert record["workflow_id"] == "wf9"
    assert record["args"] == {"command": "echo hi"}


async def test_slow_path_refuses_fast_path_tokens(pg_engine):
    """A fast-path confirm_action token persisted to the same table is
    invisible to the slow path — and still loads into fast-path memory."""
    fast = PendingActionStore()
    fast.attach_engine(pg_engine)
    token = fast.put(
        "execute_bash", {"command": "echo hi"}, owner_subject=11)
    await fast.flush_persisted()

    slow = ApprovalStore(pg_engine)
    assert await slow.resolve(
        token, approve=True, requester_subject=11) is None

    # ... and the fast path still owns it (H5 unpack/WHERE regression).
    fresh = PendingActionStore()
    fresh.attach_engine(pg_engine)
    await fresh.load_persisted()
    record = fresh.take(token, requester_subject=11)
    assert record is not None and record["type"] == "execute_bash"
    # Slow-path rows never leak into fast-path memory.
    slow_token = await slow.suspend(
        workflow_id="w", action_type="execute_bash", args={"command": "z"},
        owner_subject=11, now=1000.0)
    await fresh.load_persisted()
    assert fresh.take(slow_token, requester_subject=11) is None
    await fast.flush_persisted()


async def test_pending_for_workflow_lists_metadata(pg_engine):
    store = ApprovalStore(pg_engine)
    await store.suspend(
        workflow_id="wf-list", action_type="execute_bash",
        args={"command": "secret-cmd"}, owner_subject=3, now=1000.0)
    rows = await store.pending_for_workflow("wf-list")
    assert len(rows) == 1
    assert rows[0]["type"] == "execute_bash"
    assert rows[0]["workflow_id"] == "wf-list"
    assert "secret-cmd" not in str(rows[0])  # metadata only
    assert await store.pending_for_workflow("other") == []


async def test_bus_persists_only_workflow_scoped_events(pg_engine):
    from invincible.core.db import workflow_events

    bus = HarnessBus()
    bus.attach_engine(pg_engine)
    bus.emit(HarnessEventType.TOOL_REQUESTED, name="read_file", user_id=1)
    bus.emit(
        HarnessEventType.WORKFLOW_STARTED, workflow_id="wf-db",
        input_preview="hello",
    )
    await bus.flush_persisted()

    async with pg_engine.connect() as conn:
        rows = (await conn.execute(workflow_events.select())).all()
    assert len(rows) == 1  # ambient chatter stayed in-memory
    assert rows[0]._mapping["workflow_id"] == "wf-db"
    assert rows[0]._mapping["type"] == "workflow.started"
    assert rows[0]._mapping["payload"]["input_preview"] == "hello"

    # Memory truth holds regardless of persistence.
    assert len(bus) == 2


async def test_metadata_tables_match_migrated_schema(pg_engine):
    """create_all metadata includes the H5 tables/columns (the scratch-DB
    upgrade test pins migration convergence end-to-end)."""
    async with pg_engine.connect() as conn:
        tables = (await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        ))).scalars().all()
        cols = (await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'pending_actions'"
        ))).scalars().all()
    assert "workflow_events" in tables
    assert "suspended_workflow_id" in cols
    assert "deadline" in cols
