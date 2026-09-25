# tests/test_harness_bus.py
"""H0: harness event bus — hermetic (no Postgres)."""
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType


def test_emit_stamps_id_and_ts():
    bus = HarnessBus()
    event = bus.emit(HarnessEventType.LOG, message="hi", level="info")
    assert event["type"] == "log"
    assert event["id"]
    assert event["ts"] > 0
    assert len(bus) == 1


def test_subscribe_fan_out_and_unsubscribe():
    bus = HarnessBus()
    seen_a: list = []
    seen_b: list = []
    unsub_a = bus.subscribe(seen_a.append)
    bus.subscribe(seen_b.append)
    bus.emit(HarnessEventType.TOOL_REQUESTED, name="read_file", user_id=1)
    assert len(seen_a) == 1
    assert len(seen_b) == 1
    unsub_a()
    bus.emit(HarnessEventType.TOOL_COMPLETED, name="read_file", status="read")
    assert len(seen_a) == 1  # unsubscribed
    assert len(seen_b) == 2


def test_broken_subscriber_never_breaks_emit():
    bus = HarnessBus()

    def _boom(event):
        raise RuntimeError("subscriber blew up")

    bus.subscribe(_boom)
    event = bus.emit(HarnessEventType.LOG, message="still ok")
    assert event["message"] == "still ok"
    assert len(bus) == 1


def test_history_bounded_and_since_ts_filter():
    bus = HarnessBus(history_limit=3)
    for i in range(5):
        bus.emit(HarnessEventType.LOG, message=f"m{i}")
    assert len(bus) == 3  # bounded
    full = bus.history()
    assert [e["message"] for e in full] == ["m2", "m3", "m4"]
    since = bus.history(since_ts=full[0]["ts"])
    assert [e["message"] for e in since] == ["m3", "m4"]


def test_event_names_match_hendrixer_contract():
    assert HarnessEventType.WORKFLOW_STARTED == "workflow.started"
    assert HarnessEventType.TOOL_REQUESTED == "tool.requested"
    assert HarnessEventType.TOOL_COMPLETED == "tool.completed"
    assert HarnessEventType.TOOL_FAILED == "tool.failed"
    assert HarnessEventType.APPROVAL_REQUESTED == "approval.requested"
    assert HarnessEventType.APPROVAL_RESOLVED == "approval.resolved"
    assert HarnessEventType.AGENT_HANDOFF == "agent.handoff"
    assert HarnessEventType.PLAN_CREATED == "plan.created"
    assert HarnessEventType.MEMORY_COMPACTED == "memory.compacted"
