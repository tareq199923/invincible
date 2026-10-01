"""Merge command: combine result files, drop infra failures, guard model."""

from __future__ import annotations

import json

from tools.eval import report, run_eval


def _run(task: str, *, passed: bool, error: dict | None = None) -> dict:
    r = {"task_id": task, "passed": passed, "tool_calls_total": 1.0,
         "seconds": 5.0}
    if error is not None:
        r["error"] = error
    return r


def _file(tmp_path, name: str, runs: list[dict], model: str = "m1") -> str:
    path = tmp_path / name
    path.write_text(json.dumps({
        "meta": {"label": name, "model": model},
        "summary": {}, "overall": 0.0, "runs": runs,
    }), encoding="utf-8")
    return str(path)


def test_merge_drops_infra_and_recomputes(tmp_path, capsys, monkeypatch):
    import tools.eval.runner as runner
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    base = _file(tmp_path, "a.json", [
        _run("t1", passed=True),
        _run("t1", passed=False,
             error={"message": "All providers failed", "status": 503}),
    ])
    topup = _file(tmp_path, "b.json", [_run("t1", passed=True)])
    rc = run_eval.main(["merge", "--label", "clean", base, topup])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 infra failures dropped" in out
    files = list((tmp_path / "results").glob("*-clean.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert len(payload["runs"]) == 2
    assert payload["summary"]["t1"]["pass_rate"] == 1.0


def test_merge_output_file_and_summary(tmp_path, capsys, monkeypatch):
    import tools.eval.runner as runner
    outdir = tmp_path / "results"
    monkeypatch.setattr(runner, "RESULTS_DIR", outdir)
    base = _file(tmp_path, "a.json", [
        _run("t1", passed=True),
        _run("t1", passed=False,
             error={"message": "cooldown", "status": 503}),
        _run("t2", passed=False),
    ])
    topup = _file(tmp_path, "b.json", [_run("t1", passed=True)])
    rc = run_eval.main(["merge", "--label", "clean", base, topup])
    out = capsys.readouterr().out
    assert rc == 0
    files = list(outdir.glob("*-clean.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["meta"]["model"] == "m1"
    assert len(payload["runs"]) == 3  # infra run dropped
    assert payload["summary"]["t1"]["passed"] == 2
    assert payload["summary"]["t1"]["runs"] == 2
    assert payload["summary"]["t2"]["runs"] == 1
    assert "note: uneven coverage" in out
    # same-model guarantee is recorded for later compares
    assert payload["meta"]["merged_from"]


def test_merge_refuses_cross_model(tmp_path, capsys):
    a = _file(tmp_path, "a.json", [_run("t1", passed=True)], model="m1")
    b = _file(tmp_path, "b.json", [_run("t1", passed=True)], model="m2")
    rc = run_eval.main(["merge", "--label", "x", a, b])
    assert rc == 2
    assert "disagree on model" in capsys.readouterr().out


def test_merge_refuses_task_with_zero_genuine_runs(tmp_path, capsys):
    a = _file(tmp_path, "a.json", [
        _run("t1", passed=True),
        _run("t2", passed=False,
             error={"message": "cooldown", "status": 503}),
    ])
    rc = run_eval.main(["merge", "--label", "x", a])
    assert rc == 2
    assert "zero genuine runs remain for: t2" in capsys.readouterr().out


def test_merge_refuses_all_infra(tmp_path, capsys):
    a = _file(tmp_path, "a.json", [
        _run("t1", passed=False, error={"status": 503}),
    ])
    rc = run_eval.main(["merge", "--label", "x", a])
    assert rc == 2
    assert "every run is an infra failure" in capsys.readouterr().out


def test_merged_summary_matches_summarize_runs():
    runs = [_run("t1", passed=True), _run("t1", passed=True),
            _run("t1", passed=False)]
    summary, overall = report.summarize_runs(runs)
    assert summary["t1"]["passed"] == 2
    assert summary["t1"]["runs"] == 3
    assert abs(overall - 2 / 3) < 1e-9
