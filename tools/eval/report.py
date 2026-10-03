"""Summaries + compare deltas + compact table. Pure: stdlib only."""

from __future__ import annotations

COMPLETED_OUTCOMES = ("pass", "fail")
INFRA_OUTCOMES = ("timeout", "error")


def run_outcome(run: dict) -> str:
    """Classify one run: ``pass`` / ``fail`` / ``timeout`` / ``error``.

    Any run carrying an ``error`` (per-task ``task timeout`` or another
    infra failure such as a 503/cooldown) is NEVER ``pass``: a timeout
    reflects provider latency, not agent quality, and must not be scored
    against the agent even if the end-state checks happen to pass.
    ``timeout`` when the error message mentions a timeout, else ``error``.
    """
    error = run.get("error")
    if error:
        message = (
            str(error.get("message", "")) if isinstance(error, dict)
            else str(error)
        )
        return "timeout" if "timeout" in message.lower() else "error"
    return "pass" if run.get("passed") else "fail"


def summarize_runs(runs: list[dict]) -> tuple[dict[str, dict], float]:
    """Per-task rates/means + overall.

    ``pass_rate`` is over COMPLETED runs only (``pass`` or ``fail``);
    ``completed`` counts them and ``timeouts`` counts every infra failure
    (task timeout or other error), which never count toward the rate and
    are excluded from the per-task means (their wall-time/tool counts
    measure provider latency, not the agent). Per task: ``pass_rate,
    runs, completed, timeouts, passed, failed, mean_tool_calls,
    mean_seconds`` plus ``mean_denied`` (mean policy-denied approvals)
    and ``mean_blocked`` (mean server-blocked results). Overall = mean
    pass rate across tasks (each task weighted equally).
    """
    by_task: dict[str, list[dict]] = {}
    for run in runs:
        by_task.setdefault(run["task_id"], []).append(run)
    summary: dict[str, dict] = {}
    for task_id, items in by_task.items():
        completed = [r for r in items if run_outcome(r) in COMPLETED_OUTCOMES]
        passed = sum(1 for r in completed if r.get("passed"))
        tools = [r.get("tool_calls_total", 0) for r in completed]
        secs = [r.get("seconds", 0.0) for r in completed]
        denied = [r.get("approvals_denied", 0) for r in completed]
        blocked = [r.get("blocked_results", 0) for r in completed]
        summary[task_id] = {
            "runs": len(items),
            "completed": len(completed),
            "timeouts": len(items) - len(completed),
            "passed": passed,
            "failed": len(completed) - passed,
            "pass_rate": (passed / len(completed)) if completed else 0.0,
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
    """Per-task pass-rate/tool-call deltas (other minus base).

    ``status`` is ``IMPROVED`` / ``REGRESSED`` / ``same`` on pass-rate delta.
    ``timeouts_rose`` flags a rise in infra failures (timeouts/errors) for
    a task — provider latency drifting up, not the agent regressing.
    """
    rows: list[dict] = []
    for task_id in sorted(set(base) | set(other)):
        b = base.get(task_id, {})
        o = other.get(task_id, {})
        d_rate = o.get("pass_rate", 0.0) - b.get("pass_rate", 0.0)
        d_tools = o.get("mean_tool_calls", 0.0) - b.get("mean_tool_calls", 0.0)
        d_denied = o.get("mean_denied", 0.0) - b.get("mean_denied", 0.0)
        d_blocked = o.get("mean_blocked", 0.0) - b.get("mean_blocked", 0.0)
        b_timeouts = b.get("timeouts", 0)
        o_timeouts = o.get("timeouts", 0)
        d_timeouts = o_timeouts - b_timeouts
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
            "base_timeouts": b_timeouts,
            "other_timeouts": o_timeouts,
            "delta_timeouts": d_timeouts,
            "timeouts_rose": d_timeouts > 0,
            "status": status,
        })
    return rows


def render_table(summary: dict[str, dict], overall: float) -> str:
    """Compact human-readable table for stdout.

    ``k/N`` is passed/completed (timeouts excluded from N); ``to`` is the
    count of infra failures (timeouts/errors) for the task. Both degrade
    to the old shape when reading pre-outcome result files.
    """
    lines = [
        f"{'task':<28} {'k/N':>7} {'rate':>6} {'tools':>7} {'secs':>7} "
        f"{'deny':>6} {'block':>6} {'to':>3}",
        "-" * 77,
    ]
    for task_id in sorted(summary):
        s = summary[task_id]
        completed = s.get("completed", s.get("runs", 0))
        lines.append(
            f"{task_id:<28} {s['passed']:>2}/{completed:<4} "
            f"{s['pass_rate']:>6.2f} {s['mean_tool_calls']:>7.1f} "
            f"{s['mean_seconds']:>7.1f} "
            f"{s.get('mean_denied', 0.0):>6.1f} "
            f"{s.get('mean_blocked', 0.0):>6.1f} "
            f"{s.get('timeouts', 0):>3}"
        )
    lines.append("-" * 77)
    lines.append(f"overall (mean pass rate): {overall:.3f}")
    return "\n".join(lines)


def render_compare(rows: list[dict]) -> str:
    """Human-readable compare table with regression flags.

    ``dto`` is the timeout-count delta; a positive delta appends
    ``TIMEOUTS+`` to flag a rise in infra failures (provider latency).
    """
    lines = [
        f"{'task':<28} {'base':>6} {'other':>6} {'delta':>7} "
        f"{'dtools':>7} {'ddeny':>7} {'dblock':>7} {'dto':>4}  status",
        "-" * 92,
    ]
    for r in rows:
        flag = " TIMEOUTS+" if r.get("timeouts_rose") else ""
        lines.append(
            f"{r['task']:<28} {r['base_rate']:>6.2f} {r['other_rate']:>6.2f} "
            f"{r['delta_rate']:>+7.2f} {r['delta_tools']:>+7.1f} "
            f"{r.get('delta_denied', 0.0):>+7.1f} "
            f"{r.get('delta_blocked', 0.0):>+7.1f} "
            f"{r.get('delta_timeouts', 0):>+4}{flag}  {r['status']}"
        )
    return "\n".join(lines)
