"""Summaries + compare deltas + compact table. Pure: stdlib only."""

from __future__ import annotations


def summarize_runs(runs: list[dict]) -> tuple[dict[str, dict], float]:
    """Per-task rates/means + overall.

    Per task: ``pass_rate, runs, mean_tool_calls, mean_seconds`` plus
    ``mean_denied`` (mean policy-denied approvals) and ``mean_blocked``
    (mean server-blocked results). Overall = mean pass rate across tasks
    (each task weighted equally).
    """
    by_task: dict[str, list[dict]] = {}
    for run in runs:
        by_task.setdefault(run["task_id"], []).append(run)
    summary: dict[str, dict] = {}
    for task_id, items in by_task.items():
        passed = sum(1 for r in items if r.get("passed"))
        tools = [r.get("tool_calls_total", 0) for r in items]
        secs = [r.get("seconds", 0.0) for r in items]
        denied = [r.get("approvals_denied", 0) for r in items]
        blocked = [r.get("blocked_results", 0) for r in items]
        summary[task_id] = {
            "runs": len(items),
            "passed": passed,
            "pass_rate": (passed / len(items)) if items else 0.0,
            "mean_tool_calls": (sum(tools) / len(tools)) if tools else 0.0,
            "mean_seconds": (sum(secs) / len(secs)) if secs else 0.0,
            "mean_denied": (sum(denied) / len(denied)) if denied else 0.0,
            "mean_blocked": (sum(blocked) / len(blocked)) if blocked else 0.0,
        }
    overall = (
        sum(s["pass_rate"] for s in summary.values()) / len(summary)
        if summary else 0.0
    )
    return summary, overall


def compare_summaries(
    base: dict[str, dict], other: dict[str, dict]
) -> list[dict]:
    """Per-task pass-rate and tool-call deltas (other minus base).

    ``status`` is ``IMPROVED`` / ``REGRESSED`` / ``same`` on pass-rate delta.
    """
    rows: list[dict] = []
    for task_id in sorted(set(base) | set(other)):
        b = base.get(task_id, {})
        o = other.get(task_id, {})
        d_rate = o.get("pass_rate", 0.0) - b.get("pass_rate", 0.0)
        d_tools = o.get("mean_tool_calls", 0.0) - b.get("mean_tool_calls", 0.0)
        d_denied = o.get("mean_denied", 0.0) - b.get("mean_denied", 0.0)
        d_blocked = o.get("mean_blocked", 0.0) - b.get("mean_blocked", 0.0)
        status = (
            "IMPROVED" if d_rate > 0 else ("REGRESSED" if d_rate < 0 else "same")
        )
        rows.append({
            "task": task_id,
            "base_rate": round(b.get("pass_rate", 0.0), 3),
            "other_rate": round(o.get("pass_rate", 0.0), 3),
            "delta_rate": round(d_rate, 3),
            "base_tools": round(b.get("mean_tool_calls", 0.0), 2),
            "other_tools": round(o.get("mean_tool_calls", 0.0), 2),
            "delta_tools": round(d_tools, 2),
            "base_denied": round(b.get("mean_denied", 0.0), 2),
            "other_denied": round(o.get("mean_denied", 0.0), 2),
            "delta_denied": round(d_denied, 2),
            "base_blocked": round(b.get("mean_blocked", 0.0), 2),
            "other_blocked": round(o.get("mean_blocked", 0.0), 2),
            "delta_blocked": round(d_blocked, 2),
            "status": status,
        })
    return rows


def render_table(summary: dict[str, dict], overall: float) -> str:
    """Compact human-readable table for stdout."""
    lines = [
        f"{'task':<28} {'k/N':>7} {'rate':>6} {'tools':>7} {'secs':>7} "
        f"{'deny':>6} {'block':>6}",
        "-" * 73,
    ]
    for task_id in sorted(summary):
        s = summary[task_id]
        lines.append(
            f"{task_id:<28} {s['passed']:>2}/{s['runs']:<4} "
            f"{s['pass_rate']:>6.2f} {s['mean_tool_calls']:>7.1f} "
            f"{s['mean_seconds']:>7.1f} "
            f"{s.get('mean_denied', 0.0):>6.1f} "
            f"{s.get('mean_blocked', 0.0):>6.1f}"
        )
    lines.append("-" * 73)
    lines.append(f"overall (mean pass rate): {overall:.3f}")
    return "\n".join(lines)


def render_compare(rows: list[dict]) -> str:
    """Human-readable compare table with regression flags."""
    lines = [
        f"{'task':<28} {'base':>6} {'other':>6} {'delta':>7} "
        f"{'dtools':>7} {'ddeny':>7} {'dblock':>7}  status",
        "-" * 86,
    ]
    for r in rows:
        lines.append(
            f"{r['task']:<28} {r['base_rate']:>6.2f} {r['other_rate']:>6.2f} "
            f"{r['delta_rate']:>+7.2f} {r['delta_tools']:>+7.1f} "
            f"{r.get('delta_denied', 0.0):>+7.1f} "
            f"{r.get('delta_blocked', 0.0):>+7.1f}  {r['status']}"
        )
    return "\n".join(lines)
