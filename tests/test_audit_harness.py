# tests/test_audit_harness.py
"""Step B: harness audit — hermetic pins (tmp repos, no network, no DB)."""
from tools.audit_harness import audit_repo, main


def _write_clean_repo(path):
    path.joinpath("AGENTS.md").write_text(
        "# Demo system\n\nRun `ruff check .` then `pytest`.\n\n"
        "## Session protocol\n\nClock-in and clock-out daily.\n\n"
        "You MUST not skip tests. This must never ship broken.\n",
        encoding="utf-8",
    )
    path.joinpath("PROGRESS.md").write_text(
        "## Current State\n\ngreen\n\n## Next Steps\n\nship\n",
        encoding="utf-8",
    )
    path.joinpath("feature_list.json").write_text(
        '[{"id": "F1", "behavior": "b", "verification": "pytest", '
        '"state": "active"}]',
        encoding="utf-8",
    )
    path.joinpath("pyproject.toml").write_text("[project]\n", encoding="utf-8")
    path.joinpath("templates").mkdir()
    path.joinpath("templates", "clean-state-checklist.md").write_text(
        "tick all five\n", encoding="utf-8"
    )


def test_clean_repo_passes(tmp_path):
    _write_clean_repo(tmp_path)
    result = audit_repo(tmp_path)
    assert result.ok
    assert result.critical_failed == 0
    assert main([str(tmp_path)]) == 0


def test_missing_progress_fails_critical(tmp_path):
    _write_clean_repo(tmp_path)
    tmp_path.joinpath("PROGRESS.md").unlink()
    result = audit_repo(tmp_path)
    assert not result.ok
    assert main([str(tmp_path)]) == 1


def test_corrupt_feature_list_fails_critical(tmp_path):
    _write_clean_repo(tmp_path)
    tmp_path.joinpath("feature_list.json").write_text(
        "{not json", encoding="utf-8"
    )
    result = audit_repo(tmp_path)
    assert not result.ok
    assert any(
        f.name.startswith("feature_list") and not f.passed
        for f in result.findings
    )


def test_two_active_is_recommended_not_critical(tmp_path):
    _write_clean_repo(tmp_path)
    tmp_path.joinpath("feature_list.json").write_text(
        '[{"id": "F1", "behavior": "b", "verification": "v", '
        '"state": "active"}, {"id": "F2", "behavior": "b", '
        '"verification": "v", "state": "active"}]',
        encoding="utf-8",
    )
    result = audit_repo(tmp_path)
    assert result.ok  # WIP=1 is RECOMMENDED: warn, exit 0
    assert result.recommended_failed >= 1
    assert main([str(tmp_path)]) == 0


def test_empty_directory_fails(tmp_path):
    result = audit_repo(tmp_path)
    assert not result.ok
    assert result.critical_failed >= 3
