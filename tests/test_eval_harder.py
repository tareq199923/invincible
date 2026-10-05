"""Hermetic tests for the 10 ``harder`` eval tasks.

No server, no network, no real provider, no model. ``shell_check``
commands execute FOR REAL (python only) inside a temp workspace copy via
:func:`tools.eval.graders.run_shell_check`.

Two-sided fairness (mandatory for every ``harder`` task):
(a) the unmodified fixture FAILS the checks, and
(b) applying the task's ``reference:`` solution PASSES all checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.eval import graders
from tools.eval import runner as eval_runner
from tools.eval import tasks as task_schema

HARDER_IDS = (
    "long-chain-feature",
    "many-files-read",
    "glob-nested",
    "run-tests-report",
    "two-bugs-two-files",
    "ambiguous-edit",
    "crlf-edit",
    "wrong-path-recovery",
    "big-output-command",
    "denial-handling",
)


def _raw_tasks() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(eval_runner.TASK_DIR.glob("*.yaml")):
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if "harder" in raw.get("tags", []):
            out[raw["id"]] = raw
    return out


def _materialize(workspace: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # Same as the runner: newline="" preserves CRLF escapes byte-for-byte.
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)


# --- task inventory -----------------------------------------------------------


def test_harder_tag_count_and_ids():
    tasks = task_schema.filter_tasks(
        task_schema.load_tasks(eval_runner.TASK_DIR), tag="harder")
    assert sorted(t.id for t in tasks) == sorted(HARDER_IDS)
    # The old `hard` set is untouched by this change.
    hard = task_schema.filter_tasks(
        task_schema.load_tasks(eval_runner.TASK_DIR), tag="hard")
    assert len(hard) == 8


def test_harder_yaml_sizes_under_limit():
    for task_id in HARDER_IDS:
        size = (eval_runner.TASK_DIR / f"{task_id}.yaml").stat().st_size
        assert size < 300 * 1024, (task_id, size)


def test_harder_tasks_have_reference_blocks():
    for task_id, raw in _raw_tasks().items():
        ref = raw.get("reference")
        assert isinstance(ref, dict), task_id
        assert isinstance(ref.get("files", {}), dict), task_id
        assert isinstance(ref.get("final_text", ""), str), task_id


def test_reference_block_ignored_by_loader():
    task = task_schema.validate_task_dict({
        "id": "ref-demo",
        "category": "read",
        "prompt": "p",
        "files": {},
        "checks": [{"type": "file_absent", "path": "x.txt"}],
        "reference": {
            "files": {"x.txt": "solved"},
            "final_text": "done",
            "delete": ["old.txt"],
        },
    })
    assert not hasattr(task, "reference")
    assert task.files == {}


def test_reference_validation_rejects_bad_shapes():
    base = {
        "id": "ref-bad",
        "category": "read",
        "prompt": "p",
        "files": {},
        "checks": [{"type": "file_absent", "path": "x.txt"}],
    }
    bad_refs = [
        {"files": {"../evil.txt": "x"}},
        {"files": {"ok.txt": "x"}, "delete": ["../evil.txt"]},
        {"files": {"ok.txt": 42}},
        {"files": {}, "final_text": 42},
        {"files": {}, "bogus_key": 1},
        "not-a-mapping",
    ]
    for bad in bad_refs:
        with pytest.raises(ValueError, match="reference"):
            task_schema.validate_task_dict({**base, "reference": bad})


def test_file_line_endings_schema():
    base = {
        "id": "eol-schema",
        "category": "write",
        "prompt": "p",
        "files": {},
    }
    good = {**base, "checks": [
        {"type": "file_line_endings", "path": "a.txt", "style": "crlf"}]}
    task_schema.validate_task_dict(good)
    bad = {**base, "checks": [
        {"type": "file_line_endings", "path": "a.txt", "style": "cr"}]}
    with pytest.raises(ValueError, match="file_line_endings"):
        task_schema.validate_task_dict(bad)
    missing = {**base, "checks": [
        {"type": "file_line_endings", "path": "a.txt"}]}
    with pytest.raises(ValueError, match="file_line_endings"):
        task_schema.validate_task_dict(missing)


# --- file_line_endings grader unit tests --------------------------------------


def test_file_line_endings_crlf_passes(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    ok, _ = graders.grade_check(
        {"type": "file_line_endings", "path": "a.txt", "style": "crlf"},
        workspace=tmp_path, final_text="", tool_counts={})
    assert ok is True


def test_file_line_endings_crlf_rejects_lf(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"one\ntwo\n")
    ok, _ = graders.grade_check(
        {"type": "file_line_endings", "path": "a.txt", "style": "crlf"},
        workspace=tmp_path, final_text="", tool_counts={})
    assert ok is False


def test_file_line_endings_crlf_rejects_mixed(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\nthree\r\n")
    ok, _ = graders.grade_check(
        {"type": "file_line_endings", "path": "a.txt", "style": "crlf"},
        workspace=tmp_path, final_text="", tool_counts={})
    assert ok is False


def test_file_line_endings_lf_passes_and_rejects_crlf(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"one\ntwo\n")
    ok, _ = graders.grade_check(
        {"type": "file_line_endings", "path": "a.txt", "style": "lf"},
        workspace=tmp_path, final_text="", tool_counts={})
    assert ok is True
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    ok, _ = graders.grade_check(
        {"type": "file_line_endings", "path": "a.txt", "style": "lf"},
        workspace=tmp_path, final_text="", tool_counts={})
    assert ok is False


def test_file_line_endings_empty_and_missing_fail(tmp_path: Path):
    (tmp_path / "empty.txt").write_bytes(b"")
    for style in ("crlf", "lf"):
        ok, _ = graders.grade_check(
            {"type": "file_line_endings", "path": "empty.txt", "style": style},
            workspace=tmp_path, final_text="", tool_counts={})
        assert ok is False
    ok, _ = graders.grade_check(
        {"type": "file_line_endings", "path": "ghost.txt", "style": "lf"},
        workspace=tmp_path, final_text="", tool_counts={})
    assert ok is False


def test_crlf_fixture_survives_yaml_round_trip():
    raw = _raw_tasks()["crlf-edit"]
    assert "\r\n" in raw["files"]["settings.ini"]
    assert "\r\n" in raw["reference"]["files"]["settings.ini"]


# --- two-sided fairness -------------------------------------------------------


@pytest.mark.parametrize("task_id", sorted(HARDER_IDS))
def test_fixture_fails_without_solution(task_id: str, tmp_path: Path):
    """Unmodified fixture + empty answer must NOT pass."""
    raw = _raw_tasks()[task_id]
    _materialize(tmp_path, raw.get("files", {}))
    passed, _ = graders.grade_all(
        raw["checks"], workspace=tmp_path, final_text="",
        tool_counts={})
    assert passed is False, f"{task_id}: fixture passes with no solution"


@pytest.mark.parametrize("task_id", sorted(HARDER_IDS))
def test_reference_solution_passes(task_id: str, tmp_path: Path):
    """Applying the reference solution (files + deletes + final text,
    shell_check run for real) must pass every check."""
    raw = _raw_tasks()[task_id]
    _materialize(tmp_path, raw.get("files", {}))
    ref = raw["reference"]
    _materialize(tmp_path, ref.get("files", {}))
    for rel in ref.get("delete", []):
        (tmp_path / rel).unlink(missing_ok=True)
    passed, results = graders.grade_all(
        raw["checks"], workspace=tmp_path,
        final_text=ref.get("final_text", ""), tool_counts={})
    assert passed is True, (
        f"{task_id}: reference fails: "
        + str([(r["type"], r["passed"], r["reason"]) for r in results])
    )
