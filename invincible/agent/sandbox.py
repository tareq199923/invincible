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
  ``~/.ENV`` is the same file as ``~/.env``, and resolved through
  SYMLINKS so a link cannot stand in for a path outside the root or for
  an excluded name. Both checks run on the resolved path: the symlink
  escape in the 2026-09-24 deep review (finding 3) was that they ran on
  the unresolved one.
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
    agent can only touch things under this directory.

    Resolved through symlinks so the root is expressed the same way the
    paths checked against it are (see :func:`check_agent_path`): on a
    machine where the home directory is itself reached through a link
    (macOS ``/var`` -> ``/private/var``, some Windows profiles), an
    unresolved root would reject every legitimate path."""
    root = os.getenv("INVINCIBLE_AGENT_ROOT", "").strip()
    base = os.path.expanduser(root) if root else os.path.expanduser("~")
    return os.path.realpath(os.path.abspath(base))


def check_agent_path(path: str, verb: str) -> None:
    """Raise ToolBlocked unless ``path`` is inside the agent root and
    no component matches the basename denylist. ``verb`` is "read" or
    "write" - used only for the error message.

    Both checks run on the RESOLVED path. ``abspath`` only collapses
    ``..``; it does not follow symlinks. So a link inside the root could
    point outside it, and a link named innocently could point at an
    excluded file - `<home>/notes.txt` -> `<home>/.env` satisfied the
    root check and the basename denylist alike, then ``open()`` followed
    it. Resolving first closes both.

    Consequence worth knowing: a legitimate link pointing outside the
    sandbox is now refused. That is what the root check always claimed.

    Known limit: a link swapped between this check and the ``open()``
    that follows it is still a race. Nothing here defends against a
    local attacker changing the filesystem underneath us.

    A hard link is not caught at all - it is a second name for the same
    file, with no path to resolve.
    """
    real_path = os.path.realpath(
        os.path.abspath(os.path.expanduser(path)))
    root = os.path.normcase(agent_root())
    norm = os.path.normcase(real_path)
    if not (norm == root or norm.startswith(root + os.sep)):
        raise ToolBlocked(
            f"{verb} of path outside the agent sandbox root ({root}): "
            f"{path}"
        )
    for part in real_path.split(os.sep):
        for pattern, reason in _BASENAME_PATTERNS:
            if pattern.match(part):
                raise ToolBlocked(f"{verb} of {reason} ({real_path})")


def check_agent_read(path: str) -> None:
    check_agent_path(path, "read")


def check_agent_write(path: str) -> None:
    check_agent_path(path, "write")
