# tests/test_harness_router.py
"""H4: agent routing + handoff interception — hermetic."""
import pytest

from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.harness_router import (
    AGENTS,
    OPERATOR_AGENT,
    TRIAGE_AGENT,
    UnknownAgent,
    handle_tool_call,
    resolve,
)


def test_builtin_agents_least_privilege():
    # Triage investigates but cannot change the machine; the operator can.
    assert not TRIAGE_AGENT.allows("execute_bash")
    assert not TRIAGE_AGENT.allows("write_file")
    assert TRIAGE_AGENT.allows("read_file")
    assert TRIAGE_AGENT.allows("handoff")
    assert OPERATOR_AGENT.allows("execute_bash")
    assert OPERATOR_AGENT.allows("write_file")


def test_resolve_unknown_lists_valid_names():
    with pytest.raises(UnknownAgent, match="triage"):
        resolve(AGENTS, "billing")
    assert resolve(AGENTS, "triage") is TRIAGE_AGENT


def test_non_handoff_passes_through_untouched():
    bus = HarnessBus()
    agent, result = handle_tool_call(
        AGENTS, TRIAGE_AGENT, "read_file", {"path": "x"},
        bus=bus, workflow_id="w",
    )
    assert agent is TRIAGE_AGENT
    assert result is None
    assert len(bus) == 0  # no event for pass-through


def test_handoff_switches_agent_laterally_and_emits():
    bus = HarnessBus()
    agent, result = handle_tool_call(
        AGENTS, TRIAGE_AGENT, "handoff",
        {"to": "operator", "reason": "needs a command run"},
        bus=bus, workflow_id="wf1",
    )
    assert agent is OPERATOR_AGENT
    assert result["ok"] is True
    assert "operator" in result["message"]
    events = bus.history()
    assert len(events) == 1
    assert events[0]["type"] == HarnessEventType.AGENT_HANDOFF
    assert events[0]["from_agent"] == "triage"
    assert events[0]["to_agent"] == "operator"


def test_handoff_unknown_target_stays_and_reports():
    bus = HarnessBus()
    agent, result = handle_tool_call(
        AGENTS, TRIAGE_AGENT, "handoff", {"to": "billing", "reason": "x"},
        bus=bus, workflow_id="wf2",
    )
    assert agent is TRIAGE_AGENT  # stays, never switches to nothing
    assert result["ok"] is False
    assert "billing" in result["message"]
