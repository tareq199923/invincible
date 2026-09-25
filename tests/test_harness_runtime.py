# tests/test_harness_runtime.py
"""H2: runtime spine — hermetic (no Postgres, no Router).

Fake agent_next / execute_step / checkpoint callables drive the loop;
a real HarnessBus collects events for assertion.
"""
from invincible.core import tool_executor
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.harness_runtime import run_workflow


def _types(bus):
    return [e["type"] for e in bus.history()]


async def test_happy_path_single_tool_then_done():
    bus = HarnessBus()
    calls = []

    async def agent_next(context):
        assert context["task"] == "do the thing"
        if not calls:
            calls.append(1)
            return {"text": "", "tool_calls": [{
                "tool_call_id": "c1", "tool_name": "search",
                "input": {"q": "x"},
            }]}
        return {"text": "finished", "tool_calls": []}

    async def execute_step(name, args):
        assert (name, args) == ("search", {"q": "x"})
        return {"status": "ok", "hits": 2}

    out = await run_workflow(
        "wf1", "do the thing", agent_next=agent_next,
        execute_step=execute_step, bus=bus,
    )
    assert out == "finished"
    types = _types(bus)
    assert types[0] == HarnessEventType.WORKFLOW_STARTED
    assert HarnessEventType.TOOL_REQUESTED in types
    assert HarnessEventType.TOOL_COMPLETED in types
    assert types[-1] == HarnessEventType.WORKFLOW_COMPLETED


async def test_policy_block_becomes_structured_result_and_continues():
    """ToolBlocked from the policy gate is self-correctable: the loop
    continues and the agent sees the block as a result."""
    bus = HarnessBus()
    observed = {}

    async def agent_wrapped(context):
        if len(context["turns"]) == 0:
            return {"text": "", "tool_calls": [{
                "tool_call_id": "c1", "tool_name": "execute_bash",
                "input": {"command": "rm"},
            }]}
        observed.update(context["turns"][0][0])
        return {"text": "gave up safely", "tool_calls": []}

    async def execute_step(name, args):  # pragma: no cover
        raise AssertionError("blocked calls never execute")

    def policy(name, args):
        raise tool_executor.ToolBlocked("nope")

    out = await run_workflow(
        "wf2", "t", agent_next=agent_wrapped, execute_step=execute_step,
        policy=policy, bus=bus,
    )
    assert out == "gave up safely"
    assert observed["result"]["status"] == "blocked"
    assert HarnessEventType.TOOL_FAILED in _types(bus)
    assert HarnessEventType.TOOL_COMPLETED not in _types(bus)


async def test_executor_exception_becomes_error_result():
    bus = HarnessBus()
    n = 0

    async def agent_next(context):
        nonlocal n
        n += 1
        if n == 1:
            return {"text": "", "tool_calls": [{
                "tool_call_id": "c1", "tool_name": "x", "input": {}}]}
        # second turn: error result visible, finish
        assert context["turns"][0][0]["result"]["status"] == "error"
        return {"text": "done", "tool_calls": []}

    async def execute_step(name, args):
        raise RuntimeError("boom")

    out = await run_workflow(
        "wf3", "t", agent_next=agent_next, execute_step=execute_step,
        bus=bus,
    )
    assert out == "done"
    assert HarnessEventType.TOOL_FAILED in _types(bus)


async def test_step_limit_fails_workflow():
    bus = HarnessBus()

    async def agent_next(context):
        return {"text": "", "tool_calls": [{
            "tool_call_id": "c", "tool_name": "x", "input": {}}]}

    async def execute_step(name, args):
        return {"status": "ok"}

    out = await run_workflow(
        "wf4", "t", agent_next=agent_next, execute_step=execute_step,
        bus=bus, max_steps=2,
    )
    assert out == ""
    assert _types(bus)[-1] == HarnessEventType.WORKFLOW_FAILED


async def test_checkpoint_called_per_tool_result():
    seen = []

    async def agent_next(context):
        if not context["turns"]:
            return {"text": "", "tool_calls": [{
                "tool_call_id": "c1", "tool_name": "x", "input": {}}]}
        return {"text": "done", "tool_calls": []}

    async def execute_step(name, args):
        return {"status": "ok"}

    async def checkpoint(workflow_id, output):
        seen.append((workflow_id, output))

    out = await run_workflow(
        "wf5", "t", agent_next=agent_next, execute_step=execute_step,
        checkpoint=checkpoint, bus=None,
    )
    assert out == "done"
    assert seen == [("wf5", {"status": "ok"})]
