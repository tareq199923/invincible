# invincible/agent/sandbox.py
"""PC-side scoping for the local agent (Phase 10, wall 3).

Why this module exists instead of reusing tool_executor's checks: the
server's write denylist (WRITE_DENYLIST_PATTERNS) is *repo-root
relative* - ``_check_protected_path`` explicitly ignores paths that
resolve outside the server's own repo, because outside-the-repo is a
different risk profile there, gated by approval. On a user's machine
EVERY path is "outside the server repo", so those patterns would match
nothing and confirmed writes to ~/.env or ~/.ssh/authorized_keys
would sail through. The read sandbox is equally server-shaped: it
confines reads to the server's read roots, which are meaningless on a
laptop. The agent needs walls defined for where it actually runs:

- Reads and writes both stay under the agent root (the user's home,
  or INVINCIBLE_AGENT_ROOT), resolved case-insensitively so Windows
  ``~/.ENV`` is the same file as ``~/.env``.
- Basename denylist matched against EVERY path component, both verbs:
  dotfiles that carry credentials (.env*, .git, .ssh), key material
  (id_rsa*, *.pem, *credentials*). Case-insensitive for the same
  Windows reason tool_executor states.

Raises ``ToolBlocked`` (imported from tool_executor so the exception
type, and therefore error handling, is shared) - the runner maps a
block into the job result as ``{"status": "blocked", ...}``: reported
back to the AI, auditable on the server, never a silent drop.

The agent process itself runs as the logged-in user with their own
privileges - never elevated. Wall 1 (server checks) and wall 2 (the
agent re-running tool_executor.check_denylist on every command) live
elsewhere; this is the third wall only.
"""
import os
import re

from invincible.core.tool_executor import ToolBlocked

# Matched on every component of the requested path, case-insensitive.
# Entries appear once and are applied to BOTH reads and writes: unlike
# the server (where reading its own source is the point of read_file),
# nothing on a user's machine needs to be readable by their cloud AI
# through this agent.
_BASENAME_PATTERNS = [
    (re.compile(r"^\.env(\..+)?$", re.I), "an .env file"),
    (re.compile(r"^\.git$", re.I), "git internals"),
    (re.compile(r"^\.ssh$", re.I), "the .ssh directory"),
    (re.compile(r"^id_rsa(\..+)?$", re.I), "an SSH private key"),
    (re.compile(r"^id_ed25519(\..+)?$", re.I), "an SSH private key"),
    (re.compile(r"^.*\.pem$", re.I), "a PEM key/certificate file"),
    (re.compile(r"^.*credentials.*$", re.I), "a credentials file"),
]


def agent_root() -> str:
    """Sandbox root: the user's home, or INVINCIBLE_AGENT_ROOT. The
    agent can only touch things under this directory."""
    root = os.getenv("INVINCIBLE_AGENT_ROOT", "").strip()
    if root:
        return os.path.abspath(os.path.expanduser(root))
    return os.path.abspath(os.path.expanduser("~"))


def check_agent_path(path: str, verb: str) -> None:
    """Raise ToolBlocked unless ``path`` is inside the agent root and
    no component matches the basename denylist. ``verb`` is "read" or
    "write" - used only for the error message."""
    abs_path = os.path.abspath(os.path.expanduser(path))
    root = os.path.normcase(agent_root())
    norm = os.path.normcase(abs_path)
    if not (norm == root or norm.startswith(root + os.sep)):
        raise ToolBlocked(
            f"{verb} of path outside the agent sandbox root ({root}): "
            f"{path}"
        )
    for part in abs_path.split(os.sep):
        for pattern, reason in _BASENAME_PATTERNS:
            if pattern.match(part):
                raise ToolBlocked(f"{verb} of {reason} ({abs_path})")


def check_agent_read(path: str) -> None:
    check_agent_path(path, "read")


def check_agent_write(path: str) -> None:
    check_agent_path(path, "write")
