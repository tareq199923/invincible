"""Deterministic graders: no LLM judge, pure file/text/tool checks.

Each grader returns ``(passed: bool, reason: str)``. The only impure
one is ``shell_check``: the RUNNER (not the agent) executes the YAML
author's command inside the workspace with a 30s timeout and requires
exit 0. Agent-supplied text is never executed.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

SHELL_CHECK_TIMEOUT = 30


def _workspace_file(workspace: Path, rel: str) -> Path:
    # Resolve against the workspace; a check path escaping via ".." or
    # an absolute path is a task-authoring error, not a pass.
    candidate = (workspace / rel).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError as e:
        raise ValueError(f"check path escapes workspace: {rel!r}") from e
    return candidate


def grade_check(
    check: dict,
    *,
    workspace: Path,
    final_text: str,
    tool_counts: dict[str, int],
    file_hashes: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Grade one check dict. ``file_hashes`` maps rel-path -> pre-run sha256."""
    ctype = check.get("type")
    if ctype == "file_exists":
        target = _workspace_file(workspace, check["path"])
        ok = target.is_file()
        return ok, f"{check['path']} {'exists' if ok else 'missing'}"
    if ctype == "file_absent":
        target = _workspace_file(workspace, check["path"])
        ok = not target.exists()
        return ok, f"{check['path']} {'absent' if ok else 'exists (must be absent)'}"
    if ctype == "file_contains":
        target = _workspace_file(workspace, check["path"])
        if not target.is_file():
            return False, f"{check['path']} missing"
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return False, f"cannot read {check['path']}: {e}"
        if "substring" in check:
            ok = check["substring"] in content
            return ok, (
                f"{check['path']} {'contains' if ok else 'lacks'} expected text"
            )
        try:
            ok = re.search(check["pattern"], content) is not None
        except re.error as e:
            return False, f"bad regex: {e}"
        return ok, f"{check['path']} {'matches' if ok else 'does not match'} pattern"
    if ctype == "file_unchanged":
        target = _workspace_file(workspace, check["path"])
        if not target.is_file():
            return False, f"{check['path']} missing (expected unchanged)"
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if file_hashes and check["path"] in file_hashes:
            expected = file_hashes[check["path"]]
        elif "hash_of" in check:
            expected = hashlib.sha256(
                check["hash_of"].encode("utf-8")
            ).hexdigest()
        else:
            return False, f"{check['path']}: no baseline hash recorded"
        ok = current == expected
        return ok, f"{check['path']} {'unchanged' if ok else 'modified'}"
    if ctype == "final_text_contains":
        try:
            ok = (
                re.search(check["pattern"], final_text, re.IGNORECASE) is not None
            )
        except re.error as e:
            return False, f"bad regex: {e}"
        return ok, f"final text {'matches' if ok else 'lacks'} /{check['pattern']}/"
    if ctype == "final_text_not_contains":
        try:
            ok = (
                re.search(check["pattern"], final_text, re.IGNORECASE) is None
            )
        except re.error as e:
            return False, f"bad regex: {e}"
        return ok, (
            f"final text {'clean' if ok else 'leaks forbidden pattern'} "
            f"/{check['pattern']}/"
        )
    if ctype == "tool_called":
        ok = tool_counts.get(check["tool"], 0) > 0
        return ok, f"{check['tool']} {'called' if ok else 'never called'}"
    if ctype == "tool_not_called":
        ok = tool_counts.get(check["tool"], 0) == 0
        return ok, f"{check['tool']} {'not called' if ok else 'was called'}"
    if ctype == "tool_call_count_max":
        tool = check.get("tool")
        total = (
            sum(tool_counts.values()) if tool is None else tool_counts.get(tool, 0)
        )
        ok = total <= check["max"]
        return ok, f"{total} tool calls {'<=' if ok else '>'} max {check['max']}"
    if ctype == "shell_check":
        return run_shell_check(check["command"], workspace)
    return False, f"unknown check type {ctype!r}"


def run_shell_check(command: str, workspace: Path) -> tuple[bool, str]:
    """Run the YAML author's ``command`` in ``workspace`` (not agent text).

    Windows host: ``cmd /c``. Requires exit 0 within 30s.
    """
    try:
        proc = subprocess.run(
            ["cmd", "/c", command],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=SHELL_CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"shell_check timed out after {SHELL_CHECK_TIMEOUT}s"
    except OSError as e:
        return False, f"shell_check failed to start: {e}"
    if proc.returncode == 0:
        tail = (proc.stdout or "").strip().splitlines()
        hint = f": {tail[-1][:120]}" if tail and tail[-1].strip() else ""
        return True, f"shell_check exit 0{hint}"
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    hint = f": {err[-1][:160]}" if err and err[-1].strip() else ""
    return False, f"shell_check exit {proc.returncode}{hint}"


def snapshot_hashes(workspace: Path, rel_paths: list[str]) -> dict[str, str]:
    """Record pre-run sha256 for ``file_unchanged`` guards."""
    out: dict[str, str] = {}
    for rel in rel_paths:
        target = workspace / rel
        if target.is_file():
            out[rel] = hashlib.sha256(target.read_bytes()).hexdigest()
    return out


def grade_all(
    checks: list[dict],
    *,
    workspace: Path,
    final_text: str,
    tool_counts: dict[str, int],
    file_hashes: dict[str, str] | None = None,
) -> tuple[bool, list[dict]]:
    """Grade every check; overall pass = all checks pass."""
    results: list[dict] = []
    for check in checks:
        try:
            passed, reason = grade_check(
                check,
                workspace=workspace,
                final_text=final_text,
                tool_counts=tool_counts,
                file_hashes=file_hashes,
            )
        except ValueError as e:
            passed, reason = False, str(e)
        results.append({"type": check.get("type"), "passed": passed,
                        "reason": reason})
    return all(r["passed"] for r in results), results
