# tests/test_harness_supervisor.py
"""H4: hierarchical supervision — hermetic (no Postgres, no Router).

The planner, investigators, and synthesizer are always fakes: these
tests pin fan-out/fan-in, degradation, and validation — never a real
provider call.
"""
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.harness_router import AGENTS
from invincible.core.harness_supervisor import (
    PLAN_SYSTEM,
    build_subagent_prompt,
    make_plan,
    run_supervisor,
    validate_plan,
)


def test_validate_plan_drops_bad_steps():
    plan = {"steps": [
        {"id": "a", "agent": "triage", "objective": "look"},
        {"id": "", "agent": "triage", "objective": "no id"},
        {"id": "b", "agent": "billing", "objective": "hallucinated agent"},
        {"id": "c", "agent": "triage", "objective": ""},
        "not a dict",
    ]}
    assert validate_plan(plan, AGENTS) == [
        {"id": "a", "agent": "triage", "objective": "look"}]
    assert validate_plan({"nope": 1}, AGENTS) == []
    assert validate_plan(None, AGENTS) == []


async def test_make_plan_validates_planner_output():
    async def complete(system, user):
        assert "triage" in user  # valid agents advertised
        return {"steps": [
            {"id": "x", "agent": "operator", "objective": "run it"},
            {"id": "y", "agent": "ghost", "objective": "dropped"},
        ]}

    steps = await make_plan("task", complete=complete)
    assert steps == [{"id": "x", "agent": "operator", "objective": "run it"}]


async def test_supervisor_fans_out_and_synthesizes():
    bus = HarnessBus()
    ran = []

    async def complete(system, user):
        return {"steps": [
            {"id": "a", "agent": "triage", "objective": "check X"},
            {"id": "b", "agent": "operator", "objective": "run Y"},
        ]}

    async def investigate(agent, objective):
        ran.append((agent, objective))
        return f"{agent} found {objective}"

    async def synthesize(task, findings):
        assert task == "fix it"
        assert len(findings) == 2
        return "one clear reply"

    out = await run_supervisor(
        "fix it", investigate=investigate, synthesize=synthesize,
        complete=complete, bus=bus, workflow_id="wf1",
    )
    assert out == "one clear reply"
    assert sorted(a for a, _ in ran) == ["operator", "triage"]
    types = [e["type"] for e in bus.history()]
    assert types[0] == HarnessEventType.WORKFLOW_STARTED
    assert HarnessEventType.PLAN_CREATED in types
    assert types.count(HarnessEventType.SUBAGENT_STARTED) == 2
    assert types.count(HarnessEventType.SUBAGENT_COMPLETED) == 2
    assert types[-1] == HarnessEventType.WORKFLOW_COMPLETED


async def test_supervisor_degrades_on_partial_failure():
    """One sub-agent blows up: recorded, the rest still synthesize."""
    bus = HarnessBus()

    async def complete(system, user):
        return {"steps": [
            {"id": "a", "agent": "triage", "objective": "ok"},
            {"id": "b", "agent": "operator", "objective": "boom"},
        ]}

    async def investigate(agent, objective):
        if objective == "boom":
            raise RuntimeError("sub-agent died")
        return "fine"

    async def synthesize(task, findings):
        assert findings == [{"agent": "triage", "findings": "fine"}]
        return "degraded reply"

    out = await run_supervisor(
        "t", investigate=investigate, synthesize=synthesize,
        complete=complete, bus=bus, workflow_id="wf2",
    )
    assert out == "degraded reply"
    types = [e["type"] for e in bus.history()]
    assert HarnessEventType.SUBAGENT_FAILED in types
    assert HarnessEventType.WORKFLOW_COMPLETED in types


async def test_supervisor_empty_plan_still_synthesizes():
    async def complete(system, user):
        return {"steps": []}

    async def investigate(agent, objective):  # pragma: no cover
        raise AssertionError("no steps, no investigators")

    async def synthesize(task, findings):
        assert findings == []
        return "nothing to do"

    out = await run_supervisor(
        "t", investigate=investigate, synthesize=synthesize,
        complete=complete, bus=None,
    )
    assert out == "nothing to do"


def test_plan_system_demands_json_only():
    assert "JSON" in PLAN_SYSTEM
    assert "no other text" in PLAN_SYSTEM


def test_build_subagent_prompt_is_minimal_and_bounded():
    triage = build_subagent_prompt("triage", "check the refund query")
    assert "triage investigator sub-agent" in triage
    assert "Objective: check the refund query" in triage
    assert "findings only" in triage
    # Minimal: no full-prompt sections leak in.
    assert "Environment:" not in triage
    assert "least-privilege" not in triage

    operator = build_subagent_prompt("operator", "restart the worker")
    assert "operator sub-agent" in operator
    assert "Objective: restart the worker" in operator

    generic = build_subagent_prompt("ghost", "look around")
    assert "ghost specialist sub-agent" in generic  # degrades, never raises

    empty = build_subagent_prompt("triage", "   ")
    assert "(none given)" in empty
