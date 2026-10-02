"""Deny-by-default approval policy for manual-mode eval runs.

Pure function ``decide(tool, args, workspace) -> (approved, reason)``.
The runner calls it on every ``approval`` SSE event; denied tools get
``approve: false`` so the model sees a decline and must work around it.

* ``write_file``: approve only if the resolved path is inside the run
  workspace.
* ``execute_bash``: approve only if no token resolves to an absolute
  path outside the workspace, no ``..`` escape, and no risky-pattern
  hit (deletes, format, power, taskkill, registry, net user, shell
  wrappers, downloaders, ssh, package installs, sudo).
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


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _workspace_root(workspace: str | Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(str(workspace))))


def _resolve_target(raw: str, workspace: Path) -> Path | None:
    """Resolve a path token against the workspace. None = unresolvable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().strip("\"'")
    # UNC, drive-absolute, tilde, and posix-absolute are outside by shape.
    if text.startswith(("\\\\", "~")):
        return None
    if re.match(r"^[A-Za-z]:", text) and not _inside(
        Path(os.path.realpath(os.path.abspath(text))), workspace
    ):
        # A drive-absolute path: resolve for real; inside check decides.
        try:
            return Path(os.path.realpath(os.path.abspath(text)))
        except OSError:
            return None
    if os.path.isabs(text):
        try:
            return Path(os.path.realpath(os.path.abspath(text)))
        except OSError:
            return None
    # Relative: join onto the workspace, collapse .., resolve symlinks.
    joined = os.path.normpath(os.path.join(str(workspace), text))
    return Path(os.path.realpath(os.path.abspath(joined)))


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


def decide_execute_bash(args: dict, workspace: Path) -> tuple[bool, str]:
    command = str(args.get("command", ""))
    if not command.strip():
        return False, "execute_bash with empty command"
    for pattern, reason in _RISKY_PATTERNS:
        if pattern.search(command):
            return False, f"execute_bash blocked ({reason})"
    if _has_dotdot(command):
        return False, "execute_bash contains .. escape"
    for token in _bash_tokens(command):
        looks_like_path = (
            "/" in token or "\\" in token or token.endswith(".py")
            or token.endswith(".txt") or token.endswith(".json")
            or re.match(r"^[A-Za-z]:", token) is not None
        )
        if not looks_like_path:
            continue
        # Windows drive-absolute or posix-absolute tokens must resolve inside.
        if os.path.isabs(token) or re.match(r"^[A-Za-z]:", token):
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
