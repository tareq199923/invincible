"""Hermetic tests: hard-task graders, --tag filter, report means.

No server, no network, no real provider.
"""

from __future__ import annotations

from pathlib import Path

from tools.eval import graders, report
from tools.eval import runner as eval_runner
from tools.eval import tasks as task_schema


def test_approvals_denied_max(tmp_path: Path):
    check = {"type": "approvals_denied_max", "max": 0}
    ok, _ = graders.grade_check(
        check, workspace=tmp_path, final_text="", tool_counts={},
        approvals_denied=0,
    )
    assert ok is True
    ok, reason = graders.grade_check(
        check, workspace=tmp_path, final_text="", tool_counts={},
        approvals_denied=1,
    )
    assert ok is False
    assert reason


def test_blocked_results_max(tmp_path: Path):
    check = {"type": "blocked_results_max", "max": 1}
    ok, _ = graders.grade_check(
        check, workspace=tmp_path, final_text="", tool_counts={},
        blocked_results=1,
    )
    assert ok is True
    ok, _ = graders.grade_check(
        check, workspace=tmp_path, final_text="", tool_counts={},
        blocked_results=2,
    )
    assert ok is False


def test_region_unchanged(tmp_path: Path):
    (tmp_path / "big.py").write_text(
        "LINE-A\nTARGET-NEW\nLINE-B\n", encoding="utf-8")
    check = {
        "type": "region_unchanged",
        "path": "big.py",
        "changed_pattern": "TARGET-NEW",
        "unchanged": ["LINE-A", "LINE-B"],
    }
    ok, _ = graders.grade_check(
        check, workspace=tmp_path, final_text="", tool_counts={})
    assert ok is True
    # Missing edit fails.
    (tmp_path / "big.py").write_text(
        "LINE-A\nTARGET-OLD\nLINE-B\n", encoding="utf-8")
    ok, _ = graders.grade_check(
        check, workspace=tmp_path, final_text="", tool_counts={})
    assert ok is False
    # Dropped guarded region fails.
    (tmp_path / "big.py").write_text(
        "LINE-A\nTARGET-NEW\n", encoding="utf-8")
    ok, reason = graders.grade_check(
        check, workspace=tmp_path, final_text="", tool_counts={})
    assert ok is False
    assert "LINE-B" in reason


def test_region_unchanged_schema_rejects_bad_config():
    import pytest

    base = {
        "id": "t", "category": "write", "prompt": "p",
        "files": {},
        "checks": [{"type": "region_unchanged", "path": "x.py"}],
    }
    with pytest.raises(ValueError, match="changed_pattern"):
        task_schema.validate_task_dict(base, source="t")
    bad_max = dict(base)
    bad_max["checks"] = [{"type": "approvals_denied_max", "max": -1}]
    with pytest.raises(ValueError, match="non-negative"):
        task_schema.validate_task_dict(bad_max, source="t")


def test_tag_filter():
    tasks = task_schema.load_tasks(eval_runner.TASK_DIR)
    hard = task_schema.filter_tasks(tasks, tag="hard")
    assert len(hard) == 8
    assert all("hard" in t.tags for t in hard)
    assert task_schema.filter_tasks(tasks, tag="hard",
                                    task_id="read-far-line")[0].id == (
        "read-far-line")
    assert task_schema.filter_tasks(tasks, tag="no-such-tag") == []
    # Default run excludes memory but keeps hard.
    default = task_schema.filter_tasks(tasks)
    assert any("hard" in t.tags for t in default)


def test_hard_tasks_validate_and_reference_fixtures():
    tasks = task_schema.filter_tasks(
        task_schema.load_tasks(eval_runner.TASK_DIR), tag="hard")
    for task in tasks:
        if not task.files:
            continue  # agent-created files (chain-imports) have no fixtures
        for check in task.checks:
            if check["type"] in ("file_contains", "region_unchanged",
                                 "file_unchanged"):
                assert check["path"] in task.files, (task.id, check)


def test_report_mean_denied_blocked():
    runs = [
        {"task_id": "a", "passed": True, "tool_calls_total": 2,
         "seconds": 1.0, "approvals_denied": 1, "blocked_results": 0},
        {"task_id": "a", "passed": False, "tool_calls_total": 4,
         "seconds": 3.0, "approvals_denied": 3, "blocked_results": 2},
    ]
    summary, _ = report.summarize_runs(runs)
    assert summary["a"]["mean_denied"] == 2.0
    assert summary["a"]["mean_blocked"] == 1.0
    # Missing keys default to zero (old result files still render).
    summary2, _ = report.summarize_runs(
        [{"task_id": "b", "passed": True}])
    assert summary2["b"]["mean_denied"] == 0.0
    assert summary2["b"]["mean_blocked"] == 0.0


def test_compare_prints_denied_blocked():
    base = {"a": {"pass_rate": 1.0, "mean_tool_calls": 2.0,
                  "mean_denied": 0.0, "mean_blocked": 0.0}}
    other = {"a": {"pass_rate": 1.0, "mean_tool_calls": 2.0,
                   "mean_denied": 1.5, "mean_blocked": 0.5}}
    rows = report.compare_summaries(base, other)
    assert rows[0]["delta_denied"] == 1.5
    assert rows[0]["delta_blocked"] == 0.5
    assert rows[0]["status"] == "same"
    text = report.render_compare(rows)
    assert "ddeny" in text and "dblock" in text
    table = report.render_table(
        {"a": {"runs": 1, "passed": 1, "pass_rate": 1.0,
               "mean_tool_calls": 2.0, "mean_seconds": 1.0,
               "mean_denied": 1.5, "mean_blocked": 0.5}}, 1.0)
    assert "deny" in table and "block" in table


def test_cli_tag_flag_defaults():
    from tools.eval.run_eval import build_parser

    args = build_parser().parse_args(["run", "--label", "x", "--model", "m"])
    assert args.tag is None
    tagged = build_parser().parse_args(
        ["run", "--label", "x", "--model", "m", "--tag", "hard"])
    assert tagged.tag == "hard"
