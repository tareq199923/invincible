"""Deny-by-default approval policy for manual-mode eval runs.

Pure function ``decide(tool, args, workspace) -> (approved, reason)``.
The runner calls it on every ``approval`` SSE event; denied tools get
``approve: false`` so the model sees a decline and must work around it.

* ``write_file``: approve only if the path is ABSOLUTE and resolves
  inside the run workspace. Relative paths are ALWAYS denied: the
  server executes ``write_file`` with its own cwd (the repo root, not
  the workspace), so a relative path would land outside the sandbox.
* ``execute_bash``: two cases.

  a) ANCHORED: the command starts with ``cd /d "<workspace>" &&`` or
     ``cd "<workspace>" &&`` (case-insensitive, quoted, workspace must
     match exactly). Relative operands are then allowed because the
     shell cwd is confined to the workspace. Every other check still
     applies (risky patterns, ``..``, absolute-path-outside, and no
     re-anchoring via a later ``cd``/``pushd``/``popd``/``chdir``/bare
     drive switch).
  b) NOT ANCHORED: every path-like token must be an absolute path
     inside the workspace. Bare relative operands, bare filenames with
     an extension, relative paths with slashes, and wildcards (``*``,
     ``?``) are denied. When in doubt, deny.

LIMIT (defense in depth, not a sandbox): an approved anchored command
can still run arbitrary code (e.g. ``python -c "..."``) inside the
workspace. The policy keeps stray commands from touching the repo;
it does not sandbox what runs inside the workspace.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_RISKY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+.*-[a-z]*r", re.I), "recursive rm"),
    (re.compile(r"\b(rd|rmdir|del|erase)\b(?=.*(?<!\S)/s(?!\S))", re.I),
     "recursive windows delete"),
    (re.compile(r"\bformat\s+[a-z]:", re.I), "drive format"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I), "power command"),
    (re.compile(r"\btaskkill\b", re.I), "taskkill"),
    (re.compile(r"\breg\s+(add|delete|import)\b", re.I), "registry edit"),
    (re.compile(r"\bnet\s+(user|localgroup)\b", re.I), "account change"),
    (re.compile(r"\b(powershell|pwsh)\b", re.I), "powershell wrapper"),
    (re.compile(r"\bcmd\s+/c\b", re.I), "cmd /c wrapper"),
    (re.compile(r"\b(curl|wget|iwr|invoke-webrequest)\b", re.I), "downloader"),
    (re.compile(r"\b(ssh|scp)\b", re.I), "remote shell/copy"),
    (re.compile(r"\b(pip|pip3|npm)\s+(install|ci)\b", re.I), "package install"),
    (re.compile(r"\bsudo\b", re.I), "privilege escalation"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}", re.I), "fork bomb"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I), "filesystem format"),
    (re.compile(r"\bdd\s+.*of=/dev/", re.I), "raw disk write"),
]

# A later directory change would un-anchor the command: relative operands
# after it no longer resolve inside the workspace. Deny them outright.
_REANCHOR_WORDS = re.compile(r"\b(cd|chdir|pushd|popd)\b", re.I)
_BARE_DRIVE_SWITCH = re.compile(r"(?:^|\s)[A-Za-z]:(?:\s|$)")

# Strict anchor: ONLY these two quoted forms count. Unquoted, ;-chained,
# or pushd prefixes are NOT anchored and get deny-by-default rules.
_ANCHOR_RE = re.compile(
    r'^\s*cd\s+(?:/d\s+)?"([^"]+)"\s*&&\s*(.*)$', re.I | re.S
)

# Generic filename-with-extension (covers probe.py, README.md, data.json,
# and any other bare relative operand the old .py/.txt/.json list missed).
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _workspace_root(workspace: str | Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(str(workspace))))


def _norm_ws(text: str) -> str:
    """Normalize a path for workspace comparison.

    Collapses separators and case-differences that are insignificant on
    Windows (mixed slashes, drive-letter case) while staying correct on
    POSIX: only the drive-letter prefix is case-folded, never the rest.
    """
    cleaned = text.strip().strip("\"'")
    cleaned = cleaned.replace("/", os.sep).replace("\\", os.sep)
    cleaned = os.path.normpath(cleaned)
    cleaned = os.path.normcase(cleaned)
    drive = re.match(r"^([A-Za-z]:)", cleaned)
    if drive:
        cleaned = drive.group(1).lower() + cleaned[2:]
    return cleaned


def _is_absolute(text: str) -> bool:
    """True for posix-absolute and Windows drive-absolute paths."""
    if os.path.isabs(text):
        return True
    return re.match(r"^[A-Za-z]:[\\/]", text) is not None


def _resolve_target(raw: str, workspace: Path) -> Path | None:
    """Resolve an ABSOLUTE path token. None = relative/unresolvable.

    Relative tokens are never resolved: the server executes with its own
    cwd (not the workspace), so joining them onto the workspace would
    approve paths that actually land in the repo root.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().strip("\"'")
    # UNC, tilde, and bare drive-relative shapes are outside by shape.
    if text.startswith(("\\\\", "~")):
        return None
    if not _is_absolute(text):
        return None
    try:
        return Path(os.path.realpath(os.path.abspath(text)))
    except OSError:
        return None


def _has_dotdot(text: str) -> bool:
    return any(
        ".." in re.split(r"[\\/]+", chunk) for chunk in re.split(r"\s+", text)
    )


def decide_write_file(args: dict, workspace: Path) -> tuple[bool, str]:
    raw = str(args.get("path", ""))
    if not raw.strip():
        return False, "write_file with empty path"
    if _has_dotdot(raw):
        return False, f"write_file escapes workspace (..): {raw[:120]}"
    text = raw.strip().strip("\"'")
    if not _is_absolute(text):
        return False, (
            "write_file denied: relative path resolves against the "
            f"server's cwd, outside the workspace: {raw[:120]}"
        )
    target = _resolve_target(raw, workspace)
    if target is None or not _inside(target, workspace):
        return False, f"write_file outside workspace: {raw[:120]}"
    return True, f"write_file inside workspace: {target.name}"


def _bash_tokens(command: str) -> list[str]:
    chunks = re.split(r"[&|;`\n$]+", command)
    tokens: list[str] = []
    for chunk in chunks:
        for piece in re.split(r"\s+", chunk.strip()):
            piece = piece.strip("\"',")
            if piece:
                tokens.append(piece)
    return tokens


def _looks_like_path(token: str) -> bool:
    if "/" in token or "\\" in token:
        return True
    if re.match(r"^[A-Za-z]:", token) is not None:
        return True
    return _EXTENSION_RE.search(token) is not None


def _strip_anchor(command: str, workspace: Path) -> str | None:
    """Return the command remainder if ANCHORED, else None.

    Anchored = starts with ``cd /d "<workspace>" &&`` or
    ``cd "<workspace>" &&`` (case-insensitive, quoted, exact workspace
    match after normalization). Anything else (unquoted, ;-chained,
    pushd, different directory) is NOT anchored.
    """
    match = _ANCHOR_RE.match(command)
    if not match:
        return None
    claimed, rest = match.group(1), match.group(2)
    if _norm_ws(claimed) != _norm_ws(str(workspace)):
        return None
    if not rest.strip():
        return None
    return rest


def _check_absolute_tokens(
    command: str, workspace: Path, *, kind: str
) -> tuple[bool, str] | None:
    """Deny absolute tokens resolving outside the workspace.

    Returns ``(False, reason)`` on violation, else None.
    """
    for token in _bash_tokens(command):
        if not _is_absolute(token.strip("\"',")):
            continue
        target = _resolve_target(token, workspace)
        if target is None or not _inside(target, workspace):
            return False, f"{kind} absolute path outside: {token[:120]}"
    return None


def decide_execute_bash(args: dict, workspace: Path) -> tuple[bool, str]:
    command = str(args.get("command", ""))
    if not command.strip():
        return False, "execute_bash with empty command"
    for pattern, reason in _RISKY_PATTERNS:
        if pattern.search(command):
            return False, f"execute_bash blocked ({reason})"
    if _has_dotdot(command):
        return False, "execute_bash contains .. escape"

    anchored = _strip_anchor(command, workspace)
    if anchored is not None:
        # ANCHORED: cwd is confined to the workspace, so relative
        # operands are allowed — but nothing else is relaxed.
        if _REANCHOR_WORDS.search(anchored):
            return False, "execute_bash re-anchors (cd/pushd/popd) after anchor"
        if _BARE_DRIVE_SWITCH.search(anchored):
            return False, "execute_bash switches drive after anchor"
        violation = _check_absolute_tokens(anchored, workspace, kind="execute_bash")
        if violation is not None:
            return violation
        return True, "execute_bash anchored to workspace"

    # NOT ANCHORED: the shell runs in the server's cwd (repo root), so
    # every path-like token must be an absolute path inside the
    # workspace. Bare relative operands, bare filenames with an
    # extension, relative paths with slashes, and wildcards are denied.
    if re.match(r"^\s*cd\b", command, re.I):
        return False, (
            "execute_bash cd prefix is not the exact anchored workspace "
            '(need cd /d "<workspace>" && or cd "<workspace>" &&)'
        )
    for token in _bash_tokens(command):
        if "*" in token or "?" in token:
            return False, f"execute_bash wildcard denied: {token[:120]}"
        if not _looks_like_path(token):
            continue
        if not _is_absolute(token):
            return False, f"execute_bash relative path denied: {token[:120]}"
        target = _resolve_target(token, workspace)
        if target is None or not _inside(target, workspace):
            return False, f"execute_bash absolute path outside: {token[:120]}"
    return True, "execute_bash looks workspace-local"


def decide(tool: str, args: dict, workspace: str | Path) -> tuple[bool, str]:
    """Deny by default. Returns ``(approved, reason)``."""
    root = _workspace_root(workspace)
    if not isinstance(args, dict):
        return False, f"{tool}: non-object args"
    if tool == "write_file":
        return decide_write_file(args, root)
    if tool == "execute_bash":
        return decide_execute_bash(args, root)
    return False, f"{tool}: unknown mutating tool (deny default)"
