"""Hermetic tests: approval-policy escape fix + escape detector.

No server, no network, no real provider. ``git`` is only touched in a
scratch temp repo (skipped when git is missing).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tools.eval import approval_policy
from tools.eval import runner as eval_runner

HAS_GIT = shutil.which("git") is not None


def _anchor(ws: Path, slash_d: bool = True) -> str:
    flag = "/d " if slash_d else ""
    return f'cd {flag}"{ws}" && '


# --- write_file: relative always denied --------------------------------------


def test_policy_relative_write_denied(tmp_path: Path):
    for rel in ["out.txt", "sub/x.txt", "probe.py", ".\\out.txt"]:
        ok, reason = approval_policy.decide(
            "write_file", {"path": rel}, tmp_path)
        assert ok is False, rel
        assert "relative" in reason, reason


def test_policy_absolute_write_inside_approved(tmp_path: Path):
    ok, _ = approval_policy.decide(
        "write_file", {"path": str(tmp_path / "sub" / "x.txt")}, tmp_path)
    assert ok is True


def test_policy_absolute_write_outside_denied(tmp_path: Path):
    outside = tmp_path.parent / "elsewhere.txt"
    ok, _ = approval_policy.decide(
        "write_file", {"path": str(outside)}, tmp_path)
    assert ok is False


# --- execute_bash: unanchored denies path-like relative operands -------------


def test_policy_unanchored_dangerous_denied(tmp_path: Path):
    for cmd in ["del *.*", "rm -f README.md", "python probe.py",
                "python tools/eval/run_eval.py", "type sub/x.txt"]:
        ok, reason = approval_policy.decide(
            "execute_bash", {"command": cmd}, tmp_path)
        assert ok is False, cmd
        assert reason


def test_policy_unanchored_safe_still_approved(tmp_path: Path):
    ok, _ = approval_policy.decide(
        "execute_bash", {"command": "echo hi"}, tmp_path)
    assert ok is True


# --- execute_bash: anchored allows relative, keeps every other check ---------


def test_policy_anchored_same_commands_approved(tmp_path: Path):
    for cmd in ["del *.*", "rm -f README.md", "python probe.py"]:
        ok, reason = approval_policy.decide(
            "execute_bash", {"command": _anchor(tmp_path) + cmd}, tmp_path)
        assert ok is True, (cmd, reason)


def test_policy_anchored_without_slash_d_approved(tmp_path: Path):
    ok, _ = approval_policy.decide(
        "execute_bash", {"command": _anchor(tmp_path, slash_d=False)
                         + "python probe.py"},
        tmp_path,
    )
    assert ok is True


def test_policy_anchored_keeps_risky_and_dotdot(tmp_path: Path):
    for cmd in ["rm -rf sub", "python ..\\other\\x.py"]:
        ok, _ = approval_policy.decide(
            "execute_bash", {"command": _anchor(tmp_path) + cmd}, tmp_path)
        assert ok is False, cmd


def test_policy_anchored_absolute_outside_denied(tmp_path: Path):
    outside = tmp_path.parent / "secret.txt"
    ok, _ = approval_policy.decide(
        "execute_bash",
        {"command": _anchor(tmp_path) + f"type {outside}"}, tmp_path)
    assert ok is False


def test_policy_different_cd_prefix_denied(tmp_path: Path):
    other = tmp_path.parent / "other"
    other.mkdir()
    ok, reason = approval_policy.decide(
        "execute_bash",
        {"command": f'cd /d "{other}" && python probe.py'}, tmp_path)
    assert ok is False
    assert reason


def test_policy_unquoted_cd_not_anchored(tmp_path: Path):
    ok, _ = approval_policy.decide(
        "execute_bash",
        {"command": f"cd /d {tmp_path} && python probe.py"}, tmp_path)
    assert ok is False


def test_policy_pushd_prefix_not_anchored(tmp_path: Path):
    ok, _ = approval_policy.decide(
        "execute_bash",
        {"command": f'pushd "{tmp_path}" && python probe.py'}, tmp_path)
    assert ok is False


def test_policy_reanchor_after_anchor_denied(tmp_path: Path):
    for tail in ["cd /d C:\\ && del x", "pushd C:\\ && dir",
                 "chdir C:\\ && dir", "D: && dir"]:
        ok, reason = approval_policy.decide(
            "execute_bash", {"command": _anchor(tmp_path) + tail}, tmp_path)
        assert ok is False, tail
        assert reason


def test_policy_mixed_slashes_and_drive_case(tmp_path: Path):
    alt = str(tmp_path).replace("\\", "/")
    if len(alt) > 1 and alt[1] == ":":
        alt = alt[0].swapcase() + alt[1:]
    ok, reason = approval_policy.decide(
        "execute_bash", {"command": f'CD /D "{alt}" && python probe.py'},
        tmp_path,
    )
    assert ok is True, reason


# --- escape detector ----------------------------------------------------------


def test_detect_escape_pure_dicts():
    before = {"tracked.txt": ("  ", 1, 10)}
    after = dict(before)
    assert eval_runner.detect_escape(before, after) == []
    changed = dict(after)
    changed["evil.txt"] = ("??", 2, 5)
    assert eval_runner.detect_escape(before, changed) == ["evil.txt"]
    # Allowed prefixes never flag.
    allowed = dict(before)
    allowed[".eval_workspace/abc/out.txt"] = ("??", 2, 5)
    allowed["eval_results/20240101-x.json"] = ("??", 2, 5)
    assert eval_runner.detect_escape(before, allowed) == []
    # Deleted repo files flag too.
    assert eval_runner.detect_escape(
        {"gone.txt": ("  ", 1, 2)}, {}) == ["gone.txt (deleted)"]


def test_detect_escape_none_when_git_missing():
    assert eval_runner.detect_escape(None, {"x": ("??", 1, 1)}) == []
    assert eval_runner.detect_escape({"x": ("??", 1, 1)}, None) == []


def test_escape_exit_code():
    assert eval_runner.escape_exit_code([{"passed": True}]) == 0
    assert eval_runner.escape_exit_code(
        [{"passed": False, "escaped_files": ["evil.txt"]}]) == 1
    assert eval_runner.escape_exit_code(
        [{"passed": True}], {"batch_escapes": ["evil.txt"]}) == 1
    assert eval_runner.escape_exit_code(
        [{"passed": True}], {"batch_escapes": []}) == 0


@pytest.mark.skipif(not HAS_GIT, reason="git not installed")
def test_detector_flags_repo_root_file_in_temp_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=str(repo), check=True,
        capture_output=True)
    before = eval_runner.snapshot_git_status(repo)
    assert before is not None
    (repo / "evil.txt").write_text("logged=true", encoding="utf-8")
    (repo / ".eval_workspace").mkdir()
    (repo / ".eval_workspace" / "run1").mkdir()
    (repo / ".eval_workspace" / "run1" / "out.txt").write_text(
        "ok", encoding="utf-8")
    after = eval_runner.snapshot_git_status(repo)
    assert after is not None
    escaped = eval_runner.detect_escape(before, after)
    assert escaped == ["evil.txt"]


def test_snapshot_none_outside_repo(tmp_path: Path):
    # A directory git does not know returns non-zero -> disabled path.
    plain = tmp_path / "plain"
    plain.mkdir()
    if not HAS_GIT:
        pytest.skip("git not installed")
    assert eval_runner.snapshot_git_status(plain) is None
