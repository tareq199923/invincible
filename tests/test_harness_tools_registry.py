"""Drift guards: the registry and the hand-written surfaces agree.

``harness_policy`` logic and audit names stay hand-written ON PURPOSE
(amended scope): these tests are the tripwire. If a new tool lands in
the registry without its policy branch / runner case / dispatch branch,
exactly one test here fails loudly instead of failing silently.
"""

from __future__ import annotations

import os
import types

from invincible.core import harness_tools, tool_executor
from invincible.core.harness_policy import before_tool_call


def _names(tools: tuple) -> set[str]:
    return {tool.name for tool in tools}


def test_every_tool_exposed_somewhere():
    for tool in harness_tools.TOOLS:
        assert tool.mcp or tool.webchat or tool.router_agents, tool.name


def test_no_duplicate_names():
    names = [tool.name for tool in harness_tools.TOOLS]
    assert len(names) == len(set(names))


def test_all_names_resolve():
    for tool in harness_tools.TOOLS:
        assert harness_tools.get_tool(tool.name) is tool
    assert harness_tools.get_tool("frobnicate_xyz") is None


def test_webchat_order_matches_surface():
    assert set(harness_tools._WEBCHAT_ORDER) == {
        tool.name for tool in harness_tools.TOOLS if tool.webchat
    }


def test_registry_sets_cover_webchat_names():
    from invincible.core import webchat_agent

    webchat = {
        s["function"]["name"] for s in webchat_agent.WEBCHAT_TOOL_SCHEMAS
    }
    assert webchat == {
        tool.name for tool in harness_tools.TOOLS if tool.webchat
    }


def test_router_names_exist_in_registry():
    from invincible.core import harness_router

    for agent in harness_router.AGENTS.values():
        for name in agent.tools:
            assert harness_tools.get_tool(name) is not None, name


def test_fail_closed_default():
    assert harness_tools.HarnessTool(name="brand_new_xyz").needs_approval is True


def test_views_return_copies():
    first = harness_tools.mcp_descriptors()
    first[0]["name"] = "MUTATED"
    first[0]["inputSchema"]["properties"] = {}
    assert harness_tools.mcp_descriptors()[0]["name"] != "MUTATED"

    schemas = harness_tools.webchat_schemas()
    schemas[0]["function"]["parameters"]["properties"] = {}
    assert (
        harness_tools.webchat_schemas()[0]["function"]["parameters"][
            "properties"
        ]
        != {}
    )


# --- policy agreement (policy stays hand-written) ----------------------------

# Outside-the-sandbox probe path. Must be absolute AND outside the repo
# on BOTH platforms: a Windows drive path (C:/...) is relative on POSIX
# and resolves inside the repo root there, so it would NOT be blocked
# on Linux CI (it blocked nowhere — the failure mode is "pass").
_OUTSIDE = (
    "C:/definitely-outside-sandbox-xyz/probe.txt"
    if os.name == "nt"
    else "/definitely-outside-sandbox-xyz/probe.txt"
)


def _outcome(tool_name: str, args: dict, **kwargs) -> str:
    try:
        before_tool_call(tool_name, args, **kwargs)
    except tool_executor.ToolBlocked:
        return "blocked"
    return "pass"


def test_needs_approval_tools_are_policy_gated():
    gated = {
        tool.name for tool in harness_tools.TOOLS if tool.needs_approval
    }
    assert gated == {"execute_bash", "write_file", "edit_file"}
    assert _outcome("execute_bash", {"command": "rm -rf /"}) == "blocked"
    repo_env = os.path.join(tool_executor._REPO_ROOT, ".env")
    assert _outcome("write_file", {"path": repo_env}) == "blocked"
    assert _outcome("edit_file", {"path": repo_env}) == "blocked"
    assert _outcome("execute_bash", {"command": "echo hi"}) == "pass"


def test_read_gated_set_matches_runner_reads():
    runner_reads = {
        tool.agent_job
        for tool in harness_tools.TOOLS
        if tool.agent_job
        not in (None, "execute_bash", "write_file", "edit_file",
                "process_list", "screenshot")
    }
    assert runner_reads == {
        "read_file", "code_search", "list_dir",
        "git_status", "git_diff", "git_log",
    }
    repo = tool_executor._REPO_ROOT
    for name in runner_reads:
        assert _outcome(name, {"path": _OUTSIDE}) == "blocked", name
        assert _outcome(name, {"path": repo}) == "pass", name
        assert _outcome(name, {"path": _OUTSIDE}, agent_routed=True) == (
            "pass"), name


def test_policy_passthrough_set_is_exact():
    gated = {
        "execute_bash", "write_file", "edit_file",
        "read_file", "code_search", "list_dir",
        "git_status", "git_diff", "git_log",
    }
    expected_passthrough = _names(harness_tools.TOOLS) - gated
    assert expected_passthrough == {
        "process_list", "screenshot", "confirm_action",
        "task_state_set", "task_state_get", "checkpoint_create",
        "memory_save", "memory_search", "memory_list",
        "project_create", "project_list", "handoff",
    }
    for name in expected_passthrough:
        assert _outcome(name, {}) == "pass", name
    assert _outcome("frobnicate_xyz", {"x": 1}) == "pass"


# --- runner agreement (audit names stay hand-written) ------------------------

# Same safe-args contract as the golden runner probes: blocks and error
# results prove the job type is HANDLED (vs "Unknown job type").
_RUNNER_SAFE_ARGS = {
    "execute_bash": {"command": "rm -rf /"},
    "write_file": {"path": _OUTSIDE},
    "edit_file": {"path": _OUTSIDE},
    "read_file": {"path": _OUTSIDE},
    "code_search": {"path": "C:/definitely-outside-sandbox-xyz"},
    "list_dir": {"path": "C:/definitely-outside-sandbox-xyz"},
    "git_status": {"path": "C:/definitely-outside-sandbox-xyz"},
    "git_diff": {"path": "C:/definitely-outside-sandbox-xyz"},
    "git_log": {"path": "C:/definitely-outside-sandbox-xyz"},
    "process_list": {"limit": 1},
    "screenshot": {"url": "not-a-url"},
}


async def test_agent_job_tools_all_handled_by_runner():
    from invincible.agent import runner

    agent_jobs = {
        tool.agent_job for tool in harness_tools.TOOLS
        if tool.agent_job is not None
    }
    assert set(_RUNNER_SAFE_ARGS) == agent_jobs
    for job_type, args in _RUNNER_SAFE_ARGS.items():
        result = await runner.execute_job(
            {"type": job_type, "args": dict(args)})
        assert result.get("error", "") != f"Unknown job type: {job_type}"


# --- MCP dispatch agreement --------------------------------------------------

def _fake_request():
    """Minimal request double: no stores, so data-plane branches answer
    "not available" - still proving the branch exists (vs -32601)."""
    state = types.SimpleNamespace(
        pending_actions=tool_executor.PendingActionStore(),
        harness_bus=None,
        agent_registry=None,
        memory=None,
        retrieval=None,
        continuity=None,
        sessions=None,
        engine=None,
        audit_log=None,
        oauth_store=None,
    )
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state),
                                 state=types.SimpleNamespace())


async def test_mcp_dispatch_covers_every_mcp_tool():
    from invincible.endpoints import mcp as mcp_endpoint

    dispatched = [
        tool.name for tool in harness_tools.TOOLS if tool.mcp
    ]
    assert len(dispatched) == 20
    hostile = {
        "execute_bash": {"command": "rm -rf /"},
        "write_file": {"path": _OUTSIDE},
        "edit_file": {"path": _OUTSIDE},
        "read_file": {"path": _OUTSIDE},
        "code_search": {"pattern": "x", "path": _OUTSIDE},
        "list_dir": {"path": _OUTSIDE},
        "git_status": {"path": _OUTSIDE},
        "git_diff": {"path": _OUTSIDE},
        "git_log": {"path": _OUTSIDE},
    }
    for name in dispatched:
        args = hostile.get(name, {})
        response = await mcp_endpoint._dispatch(
            "tools/call", 1, {"name": name, "arguments": args},
            _fake_request(), principal=None,
        )
        # Every registered tool has a branch: a result payload (even a
        # "not available"/blocked one), never -32601 unknown-tool.
        assert "result" in response, name


async def test_mcp_unknown_tool_still_32601():
    from invincible.endpoints import mcp as mcp_endpoint

    response = await mcp_endpoint._dispatch(
        "tools/call", 1, {"name": "frobnicate_xyz", "arguments": {}},
        _fake_request(), principal=None,
    )
    assert response["error"]["code"] == -32601


def test_mode_coverage():
    for tool in harness_tools.TOOLS:
        if not tool.webchat:
            assert tool.webchat_modes == ()
            continue
        assert tool.webchat_modes, tool.name
        assert set(tool.webchat_modes) <= set(harness_tools.WEBCHAT_MODES)
    all_modes = harness_tools.webchat_for_mode("manual")
    assert {s["function"]["name"] for s in all_modes} == {
        tool.name for tool in harness_tools.TOOLS if tool.webchat
    }
