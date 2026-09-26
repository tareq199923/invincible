# invincible/core/harness_policy.py
"""Unified pre-execution policy gate (H2) — port of Hendrixer's
`policy.check(step)` / `ToolPolicy.beforeToolCall`.

This module adds NO new block patterns. It orchestrates the checks that
already exist, in one place, so every machine-plane entry point (MCP
dispatch today, supervisor fan-out in H4) enforces the same order:

- ``execute_bash`` → ``tool_executor.check_denylist`` (Wall 1; the agent
  re-runs it locally as Wall 2 in ``agent/runner.py``).
- ``write_file`` → ``tool_executor.check_write_denylist`` (server
  repo-root-relative; the agent's home sandbox re-gates locally).
- ``read_file`` → ``tool_executor.check_read_denylist`` — server roots
  only when executing locally. When agent-routed, the server skips its
  own roots check (meaningless for another machine's home) and the
  agent's home sandbox (``agent/sandbox.py``) is the gate.

Raises ``tool_executor.ToolBlocked`` on a hit — the caller maps it to
the wire shape (``Blocked: <reason>``, ``isError: true``). Anything else
passes through untouched, including unknown tool names (the dispatcher
owns those) and ``confirm_action`` (token resolution owns that).
"""
from __future__ import annotations

from invincible.core import tool_executor


def before_tool_call(
    tool_name: str, args: dict | None, *, agent_routed: bool = False
) -> None:
    """Run the pre-execution checks for one staged tool call.

    Pure orchestrator — every pattern lives in ``tool_executor`` /
    ``agent/sandbox``. Raises ``ToolBlocked``; returns None on pass.
    """
    argv = args or {}
    if tool_name == "execute_bash":
        tool_executor.check_denylist(str(argv.get("command", "")))
        return
    if tool_name == "write_file":
        tool_executor.check_write_denylist(str(argv.get("path", "")))
        return
    if tool_name == "read_file":
        if agent_routed:
            # The agent's home sandbox gates this read locally; the
            # server's repo-root-relative roots would check the wrong
            # machine's filesystem.
            return
        tool_executor.check_read_denylist(str(argv.get("path", "")))
        return
    if tool_name in ("code_search", "list_dir", "git_status",
                       "git_diff", "git_log"):
        # Same sandbox as read_file (H6a): path-shaped, non-destructive.
        # Directory/git inspection runs under it with no confirm step.
        if agent_routed:
            return
        tool_executor.check_read_denylist(str(argv.get("path", "") or "."))
        return
    # confirm_action, process_list, screenshot, memory_*, project_*,
    # task_state_*, unknown: no pre-check here — owned by token
    # resolution / dispatch / data-plane (screenshot is agent-only and
    # process_list carries no path).
    return
