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


def _anchor(ws: Path, slash_d: bool = True, quoted: bool = True) -> str:
    flag = "/d " if slash_d else ""
    path = f'"{ws}"' if quoted else str(ws)
    return f"cd {flag}{path} && "


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


def test_policy_unquoted_cd_anchored(tmp_path: Path):
    # The model omits quotes (and sometimes /d): still anchored when the
    # path matches the workspace exactly (denying this was the bug that
    # failed run-and-report 3/3).
    for prefix in (_anchor(tmp_path, quoted=False),
                   _anchor(tmp_path, slash_d=False, quoted=False)):
        ok, reason = approval_policy.decide(
            "execute_bash", {"command": prefix + "python probe.py"},
            tmp_path,
        )
        assert ok is True, (prefix, reason)


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


def test_policy_unquoted_mixed_slashes_and_drive_case(tmp_path: Path):
    alt = str(tmp_path).replace("\\", "/")
    if len(alt) > 1 and alt[1] == ":":
        alt = alt[0].swapcase() + alt[1:]
    for prefix in (f"cd /d {alt} && ", f"cd {alt} && "):
        ok, reason = approval_policy.decide(
            "execute_bash", {"command": prefix + "python probe.py"},
            tmp_path,
        )
        assert ok is True, (prefix, reason)


def test_policy_unquoted_wrong_directory_denied(tmp_path: Path):
    other = tmp_path.parent / "other"
    other.mkdir(exist_ok=True)
    for cmd in [
        f"cd /d {other} && python probe.py",  # wrong directory
        f"cd {tmp_path.parent} && python probe.py",  # parent
        f"cd {tmp_path / 'sub'} && python probe.py",  # subdirectory
        # extra cd after an (unquoted) anchor re-anchors: deny.
        f"cd {tmp_path} && cd sub && python probe.py",
    ]:
        ok, reason = approval_policy.decide(
            "execute_bash", {"command": cmd}, tmp_path)
        assert ok is False, cmd
        assert reason


def test_policy_unbalanced_quotes_denied(tmp_path: Path):
    for cmd in [
        f'cd "{tmp_path} && python probe.py',  # unterminated
        f"cd {tmp_path}\" && python probe.py",  # trailing quote
        f'cd "{tmp_path}\' && python probe.py',  # mismatched
        # cmd.exe has no single-quote quoting: the cd would miss.
        f"cd '{tmp_path}' && python probe.py",
    ]:
        ok, reason = approval_policy.decide(
            "execute_bash", {"command": cmd}, tmp_path)
        assert ok is False, cmd
        assert reason


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


# --- unanchored mutating verbs + redirection ----------------------------------


def test_policy_unanchored_extensionless_verbs_denied(tmp_path: Path):
    for cmd in ["del LICENSE", "del Dockerfile", "mkdir stuff",
                "echo hi > out", "rd docs", "move Procfile x"]:
        ok, reason = approval_policy.decide(
            "execute_bash", {"command": cmd}, tmp_path)
        assert ok is False, cmd
        assert reason


def test_policy_unanchored_all_verbs_denied_with_relative(tmp_path: Path):
    for verb in ["del", "erase", "rm", "rmdir", "rd", "mkdir", "md", "mv",
                 "move", "ren", "rename", "copy", "cp", "xcopy", "robocopy",
                 "touch", "tee", "attrib", "icacls"]:
        ok, _ = approval_policy.decide(
            "execute_bash", {"command": f"{verb} target"}, tmp_path)
        assert ok is False, verb


def test_policy_unanchored_verbs_approved_anchored(tmp_path: Path):
    for cmd in ["del LICENSE", "del Dockerfile", "mkdir stuff",
                "echo hi > out", "rd docs", "move Procfile x"]:
        ok, reason = approval_policy.decide(
            "execute_bash", {"command": _anchor(tmp_path) + cmd}, tmp_path)
        assert ok is True, (cmd, reason)


def test_policy_unanchored_verbs_absolute_inside_approved(tmp_path: Path):
    ws = str(tmp_path)
    for cmd in [f"del {ws}/LICENSE", f"mkdir {ws}/stuff",
                f"rd {ws}/docs", f"move {ws}/Procfile {ws}/x",
                f"copy {ws}/a {ws}/b", "del /q " + f"{ws}/gone"]:
        ok, reason = approval_policy.decide(
            "execute_bash", {"command": cmd}, tmp_path)
        assert ok is True, (cmd, reason)


def test_policy_unanchored_redirection_always_denied(tmp_path: Path):
    # Even an absolute target does not save an unanchored redirect.
    ok, _ = approval_policy.decide(
        "execute_bash",
        {"command": f"echo hi > {tmp_path}/out"}, tmp_path)
    assert ok is False
    ok, _ = approval_policy.decide(
        "execute_bash",
        {"command": f"echo hi >> {tmp_path}/out"}, tmp_path)
    assert ok is False


def test_policy_verb_edge_cases(tmp_path: Path):
    # Bare verb proves nothing about its target: deny.
    ok, _ = approval_policy.decide(
        "execute_bash", {"command": "del"}, tmp_path)
    assert ok is False
    # Verb outside command position cannot be parsed reliably: deny.
    ok, _ = approval_policy.decide(
        "execute_bash", {"command": "echo del"}, tmp_path)
    assert ok is False
    # Mixed: one bad operand spoils the verb.
    ok, _ = approval_policy.decide(
        "execute_bash",
        {"command": f"move {tmp_path}/a b"}, tmp_path)
    assert ok is False
    # Absolute target outside the workspace: deny.
    outside = tmp_path.parent / "elsewhere.txt"
    ok, _ = approval_policy.decide(
        "execute_bash",
        {"command": f"del {outside}"}, tmp_path)
    assert ok is False


def test_policy_edit_file_matches_write_file(tmp_path: Path):
    from tools.eval import approval_policy

    ok, _ = approval_policy.decide(
        "edit_file", {"path": str(tmp_path / "sub" / "x.txt")}, tmp_path)
    assert ok is True
    for evil in ["out.txt", "../evil.txt", "/etc/passwd",
                 str(tmp_path.parent / "elsewhere.txt")]:
        ok, reason = approval_policy.decide(
            "edit_file", {"path": evil}, tmp_path)
        assert ok is False, evil
        assert reason


def test_eval_approval_args_edit_file_path_shape():
    from tools.eval.runner import _approval_args

    args = _approval_args(
        "edit_file",
        {"summary": "Edit x", "detail": "C:/ws/notes.txt\n---\n-old\n+new"},
    )
    assert args == {"path": "C:/ws/notes.txt"}
