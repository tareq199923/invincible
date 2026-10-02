"""Hermetic tests: report summaries/compare + workspace denylist proof."""

from __future__ import annotations

import os

import pytest

from invincible.core import tool_executor
from tools.eval import report
from tools.eval import runner as eval_runner


def _run(task_id: str, passed: bool, tools: int, secs: float) -> dict:
    return {"task_id": task_id, "passed": passed,
            "tool_calls_total": tools, "seconds": secs}


def test_summarize_math():
    runs = [_run("a", True, 2, 10.0), _run("a", False, 4, 20.0),
            _run("b", True, 1, 5.0)]
    summary, overall = report.summarize_runs(runs)
    assert summary["a"]["pass_rate"] == pytest.approx(0.5)
    assert summary["a"]["mean_tool_calls"] == pytest.approx(3.0)
    assert summary["a"]["mean_seconds"] == pytest.approx(15.0)
    assert summary["b"]["pass_rate"] == pytest.approx(1.0)
    assert overall == pytest.approx(0.75)  # mean of 0.5 and 1.0


def test_compare_flags_regression():
    base = {"a": {"pass_rate": 1.0, "mean_tool_calls": 2.0},
            "b": {"pass_rate": 0.0, "mean_tool_calls": 5.0}}
    other = {"a": {"pass_rate": 0.5, "mean_tool_calls": 3.0},
             "b": {"pass_rate": 1.0, "mean_tool_calls": 4.0}}
    rows = report.compare_summaries(base, other)
    by_task = {r["task"]: r for r in rows}
    assert by_task["a"]["status"] == "REGRESSED"
    assert by_task["a"]["delta_rate"] == pytest.approx(-0.5)
    assert by_task["b"]["status"] == "IMPROVED"
    assert "REGRESSED" in report.render_compare(rows)
    assert "overall" in report.render_table(
        {"a": {"runs": 2, "passed": 1, "pass_rate": 0.5,
               "mean_tool_calls": 3.0, "mean_seconds": 1.0}}, 0.5)


def test_no_secrets_guard():
    with pytest.raises(eval_runner.EvalError):
        eval_runner.check_no_secrets({"x": "EVAL_PASSWORD=secret"})
    eval_runner.check_no_secrets({"runs": [{"final_text": "hello"}]})


# --- workspace vs the real server denylists ----------------------------------


def test_workspace_inside_repo_passes_denylists(tmp_path):
    """The run dir must be readable/writable under the real sandbox rules."""
    workspace = eval_runner.WORKSPACE_ROOT / "probe-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        target = workspace / "calc.py"
        target.write_text("x = 1", encoding="utf-8")
        # No raise = allowed under the real server gates.
        tool_executor.check_read_denylist(str(target))
        tool_executor.check_write_denylist(str(target))
        tool_executor.check_read_denylist(str(workspace))
    finally:
        import shutil

        shutil.rmtree(workspace, ignore_errors=True)


def test_protected_paths_still_blocked_from_workspace():
    """Write denylist (invincible/, tests/, .git/, .env, ...) is intact."""
    root = tool_executor._REPO_ROOT
    for rel in ("invincible/x.py", "tests/x.py", ".git/x", ".env",
                "providers.yaml", "sessions.db"):
        with pytest.raises(tool_executor.ToolBlocked):
            tool_executor.check_write_denylist(os.path.join(root, rel))
    # Reads: secrets blocked, source allowed.
    with pytest.raises(tool_executor.ToolBlocked):
        tool_executor.check_read_denylist(os.path.join(root, ".env"))
    tool_executor.check_read_denylist(os.path.join(root, "invincible"))
