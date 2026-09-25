# tests/test_harness_policy.py
"""H2: policy gate — hermetic (no Postgres).

The gate adds no patterns; it orchestrates the existing checks. These
tests pin the routing: which check runs for which tool, and the
agent-routed read_file exemption (server roots are meaningless for
another machine's home — the agent sandbox gates there).
"""
import pytest

from invincible.core import tool_executor
from invincible.core.harness_policy import before_tool_call


def test_bash_denylist_hit_raises():
    with pytest.raises(tool_executor.ToolBlocked):
        before_tool_call("execute_bash", {"command": "sudo rm -rf /"})


def test_bash_safe_passes():
    before_tool_call("execute_bash", {"command": "git status"})  # no raise


def test_write_outside_repo_passes_gate():
    # Outside the server repo is the approval step's risk class, not the
    # denylist's — the gate must not refuse it.
    before_tool_call("write_file", {"path": "/tmp/scratch/out.txt"})


def test_read_roots_checked_locally(tmp_path, monkeypatch):
    import os

    monkeypatch.chdir(tmp_path)
    before_tool_call("read_file", {"path": str(tmp_path / "ok.txt")})
    outside = os.path.abspath(os.path.join(str(tmp_path), "..", "x.txt"))
    with pytest.raises(tool_executor.ToolBlocked):
        before_tool_call("read_file", {"path": outside})


def test_read_agent_routed_skips_server_roots():
    # Same outside-roots path passes when agent-routed: the agent's home
    # sandbox (Wall 3) gates it locally instead.
    before_tool_call(
        "read_file", {"path": "/definitely/not/the/server/repo/x.txt"},
        agent_routed=True,
    )


def test_code_search_shares_read_roots(tmp_path, monkeypatch):
    import os

    monkeypatch.chdir(tmp_path)
    before_tool_call(
        "code_search", {"pattern": "x", "path": str(tmp_path)})
    outside = os.path.abspath(os.path.join(str(tmp_path), "..", "x"))
    with pytest.raises(tool_executor.ToolBlocked):
        before_tool_call("code_search", {"pattern": "x", "path": outside})
    before_tool_call(
        "code_search", {"pattern": "x", "path": outside},
        agent_routed=True,
    )  # agent home sandbox gates instead


def test_non_machine_plane_tools_pass_through():
    for name in (
        "confirm_action", "process_list", "screenshot",
        "memory_save", "project_list", "task_state_get",
        "some_unknown_tool",
    ):
        before_tool_call(name, {})  # no raise, no check
