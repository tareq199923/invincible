# tests/test_harness_router.py
"""H4: agent routing + handoff interception — hermetic."""
import pytest

from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.harness_router import (
    AGENTS,
    BASE_PROMPT,
    OPERATOR_AGENT,
    TRIAGE_AGENT,
    UnknownAgent,
    build_system_prompt,
    classify_task,
    handle_tool_call,
    render_env_block,
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


def test_classify_task_kinds():
    assert classify_task("why is login slow? find the bug") == "read"
    assert classify_task("run the migration and verify") == "do"
    assert classify_task("fix the refund bug") == "do"
    assert classify_task("write a plan outline for SSO") == "plan"
    assert classify_task("plan the fix for refunds") == "plan"  # plan wins
    assert classify_task("") == "read"
    # Word boundaries: "outlines" in prose is not the planning keyword.
    assert classify_task("he outlines the facts") == "read"


def test_static_prompts_carry_base_and_matching_tools():
    for agent in (TRIAGE_AGENT, OPERATOR_AGENT):
        assert agent.system_prompt.startswith(BASE_PROMPT)
    assert "read_file" in TRIAGE_AGENT.system_prompt
    assert "handoff" in TRIAGE_AGENT.system_prompt
    assert "execute_bash" not in TRIAGE_AGENT.system_prompt
    assert "execute_bash" in OPERATOR_AGENT.system_prompt
    assert "verify" in OPERATOR_AGENT.system_prompt


def test_build_system_prompt_section_order_and_env_last():
    prompt = build_system_prompt(
        "triage", "why is checkout slow?",
        model="m1", cwd="/home/u", date_str="2026-09-26",
    )
    assert prompt.startswith(BASE_PROMPT)
    assert "read_file" in prompt  # read overlay for a read task
    env = "Environment: model=m1 | cwd=/home/u | date=2026-09-26."
    assert prompt.endswith(env)


def test_build_system_prompt_task_overlays_and_unknown_agent():
    do_prompt = build_system_prompt(
        "operator", "restart the worker", date_str="2026-09-26")
    assert "Inspect, then act, then verify" in do_prompt
    plan_prompt = build_system_prompt(
        "triage", "outline the migration steps", date_str="2026-09-26")
    assert "end with the plan" in plan_prompt
    generic = build_system_prompt(
        "ghost", "look around", date_str="2026-09-26")
    assert "ghost specialist" in generic  # degrades, never raises


def test_render_env_block_unknowns_and_defaults():
    assert render_env_block(date_str="2026-09-26") == (
        "Environment: model=unknown | cwd=unknown | date=2026-09-26."
    )
    # Empty date falls back to today without raising.
    assert "date=" in render_env_block()
