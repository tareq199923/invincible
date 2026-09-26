# invincible/core/tool_executor.py
"""Execution layer for MCP tools (execute_bash, write_file).

Security model - decided explicitly up front, not bolted on after the fact:

  1. execute_bash uses a DENYLIST, not an allowlist: known-dangerous command
     patterns are blocked outright, everything else is allowed through. This
     keeps the tool usable for arbitrary dev work while still catching the
     small set of commands most likely to do irreversible damage.
  2. write_file additionally has its own path denylist: even an otherwise
     harmless-looking write is blocked outright if its target is a file
     this project depends on for its own security or state (`.env`,
     `providers.yaml`, `sessions.db`, Invincible's own source, its tests,
     or `.git/`). Approval is a good backstop, but it shouldn't be the
     only thing standing between a cloud AI and this server rewriting its
     own auth check.
3. Every execute_bash and write_file call that isn't blocked is staged as
      a pending action with an unpredictable token, and nothing runs until
      the caller confirms it through the ``confirm_action`` tool (a second
      ``/mcp`` call with that token). This replaces the old synchronous y/N
      terminal prompt so a remote operator - e.g. someone on their phone
      talking to a cloud AI through a tunnel - can approve without any
      physical access to the machine. By default the store is in-memory
      only and a restart orphans staged actions (the original design: a
      clean slate on restart). Persistence is opt-in: when the
      ``INVINCIBLE_PERSIST_PENDING_ACTIONS`` environment variable is set,
      main.py attaches the shared PostgreSQL engine (INVINCIBLE_DB_URL)
      and staged actions are written to the ``pending_actions`` table and
      survive a restart. Timestamps use wall-clock time (``time.time()``)
      because ``created_at`` crosses process boundaries when persisted; a
      monotonic clock is only meaningful inside one process.
  4. TRUST BOUNDARY (changed deliberately, on purpose): before, only
     someone with physical access to the server's terminal could approve an
     action. After, approval is whatever the calling AI/client reports back
     through a second ``/mcp`` call - the boundary is now "whoever holds a
     valid OAuth bearer access token", the same boundary as every other
     request on ``/mcp``. A live token implies its owner approved the
     client on the /oauth/authorize consent page, and can be revoked with
     ``invincible oauth revoke``. Holding a token is sufficient to approve
     (or deny) pending actions remotely. This is a real security property
     change, not an implementation detail.
  5. Authentication for who can reach this code at all lives one layer up,
     in the MCP endpoint's dependency (OAuth 2.1 + PKCE bearer tokens,
     independent of the /v1/* inv_ API keys). This module assumes the
     caller is already authenticated - it only decides whether a specific
     action is safe and approved, not who's allowed to ask.
  6. read_file has no approval step, so it is sandboxed instead: reads are
     only allowed under the repo root, the server's working directory, and
     any directories listed in INVINCIBLE_READ_ROOTS (os.pathsep-separated).
     Paths outside those roots are blocked outright, and .env / sessions.db
     / .git are blocked by name anywhere inside them.
  7. Every path check resolves SYMLINKS before matching (``realpath``, not
     ``abspath``), and resolves the roots it compares against the same way.
     ``abspath`` only collapses ``..``: without this, a link inside the repo
     named innocently could point at ``.env`` and match no pattern, and a
     link out of the repo could carry a name that matches one it does not
     target. This refuses legitimate links out of the sandbox, which is what
     the root rules always claimed.

KNOWN LIMIT: the denylist is a text-pattern match, not a real shell parser.
`powershell -Command "..."`, `cmd /c "..."`, or any other wrapper/encoding
can smuggle an arbitrary command past every pattern below. The denylist
exists to catch the obvious, high-blast-radius cases without a prompt; it
is not the real safety boundary. The approval step is - whoever holds a
valid bearer token decides what runs, and anything staged for approval is
visible in plain sight at the server's own stdout before it is approved.

KNOWN LIMIT: path resolution closes symlinks, not every filesystem trick.
A HARD link is a second name for the same file and has no path to resolve,
so it is not caught. Nor is a link swapped between the check and the
``open()`` that follows it - nothing here defends against a local attacker
mutating the filesystem underneath the process.
"""
import asyncio
import base64
import contextlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import time

from sqlalchemy import delete

from invincible.core.db import pending_actions
from invincible.core.settings import PENDING_ACTION_TTL_SECONDS, settings

logger = logging.getLogger("invincible.tool_executor")

# Matched against the full command string, case-insensitive. Each entry is
# (compiled pattern, human-readable reason) so a block can explain itself
# in the response instead of failing silently.
DENYLIST_PATTERNS = [
    # --- Unix / POSIX ---
    (re.compile(r"rm\s+(-\w*r\w*f\w*|-\w*f\w*r\w*)\s+(/|~|\$HOME)(\s|/|$)", re.I),
     "recursive force-delete of home or root"),
    (re.compile(r"rm\s+-[a-z]*r[a-z]*\s+/(\s|$)", re.I),
     "recursive delete starting at filesystem root"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.I),
     "fork bomb"),
    (re.compile(r"\bdd\s+.*of=/dev/", re.I),
     "raw write to a block device"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I),
     "filesystem format command"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk)", re.I),
     "redirect writing directly to a disk device"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I),
     "system power/shutdown command"),
    (re.compile(r"\bsudo\b", re.I),
     "privilege escalation via sudo"),
    (re.compile(r"\bchmod\s+(-R\s+)?777\s+/(\s|$)", re.I),
     "world-writable permissions on filesystem root"),
    (re.compile(r"\bchown\s+-R\s+\S+\s+/(\s|$)", re.I),
     "recursive ownership change on filesystem root"),
    (re.compile(r"(curl|wget)\s+.*\|\s*(sudo\s+)?(sh|bash|zsh)\b", re.I),
     "piping a remote download straight into a shell"),
    (re.compile(r"\bkill\s+-9\s+-1\b", re.I),
     "kill all processes"),
    (re.compile(r">\s*/etc/(passwd|shadow|sudoers)\b", re.I),
     "overwrite of a core system credentials file"),

    # --- Windows / cmd.exe ---
    # rd/rmdir/del/erase with an /s (recurse) flag AND a drive-root target
    # (C:\, C:\*, C:\*.*). Flags can appear in either order around the
    # target, so both lookaheads scan the whole command rather than
    # anchoring to a fixed position. A subdirectory target (rd /s C:\build)
    # does NOT match - that's the Windows equivalent of `rm -rf ./build`
    # and is left to the approval step, same as its Unix counterpart.
    (re.compile(
        r"\b(rd|rmdir|del|erase)\b"
        r"(?=.*(?<!\S)/s(?!\S))"
        r"(?=.*[A-Za-z]:\\+(\*(\.\*)?)?(\s|[\"'&|]|$))",
        re.I,
    ), "recursive delete targeting a Windows drive root"),
    (re.compile(r"\bformat\s+[A-Za-z]:", re.I),
     "formatting a Windows drive"),
]

# Paths (relative to the repo root) that write_file refuses to touch
# outright, regardless of approval. Repo root is resolved the same way
# Router resolves providers.yaml (three dirname() calls up from this file:
# invincible/core/tool_executor.py -> invincible/core -> invincible -> repo root).
#
# Case-insensitive on purpose: Windows filesystems treat .env and .ENV as
# the same file, so a differently-cased target must not slip past.
#
# realpath'd because every path compared against it is realpath'd too
# (see _check_protected_path): resolving one side only would make the
# relpath() below report ".." for every legitimate path whenever the
# checkout is itself reached through a symlink.
_REPO_ROOT = os.path.realpath(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
))

WRITE_DENYLIST_PATTERNS = [
    (re.compile(r"^\.env(\..+)?$", re.I), "Invincible's .env file"),
    (re.compile(r"^providers\.yaml$", re.I), "provider configuration"),
    (re.compile(r"^sessions\.db$", re.I), "the session database"),
    (re.compile(r"^invincible/", re.I), "Invincible's own source code"),
    (re.compile(r"^tests/", re.I), "the test suite"),
    (re.compile(r"^\.git/", re.I), "git internals"),
]

# Narrower than WRITE_DENYLIST_PATTERNS on purpose: invincible/ and tests/ are
# blocked from being overwritten, but reading them is the entire point of
# giving a cloud AI a read_file tool - it needs to see the code before it
# can usefully write or run anything. providers.yaml only holds api_key_env
# *names*, not actual key values, so it's not a secret either. This list is
# only things that would leak an actual credential or sensitive local state
# if their contents were read out over the tunnel.
READ_DENYLIST_PATTERNS = [
    (re.compile(r"^\.env(\..+)?$", re.I), "Invincible's .env file"),
    (re.compile(r"^sessions\.db$", re.I), "the session database"),
    (re.compile(r"^\.git/", re.I), "git internals"),
]


class ToolBlocked(Exception):
    """Command or write target matched a denylist; never staged for approval."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# Reserved args key carrying the staging subject through pending_actions
# persistence (Phase 2); stripped on load, never seen by tool execution.
_OWNER_SUBJECT_KEY = "_owner_subject"


class PendingActionStore:
    """In-process store of staged, not-yet-approved actions.

    Tokens are ``secrets.token_urlsafe(16)`` - unpredictable and generated
    per action. Entries expire ``TTL_SECONDS`` after creation; an expired
    token behaves exactly like an unknown one and is purged on the next
    sweep (lazily done on insert and on lookup - no background task).

    ``take()`` pops the entry, making each token single-use: confirming the
    same token twice can never execute the action twice (replay guard).

    Persistence (Phase 16): opt-in via ``attach_engine(engine)`` - main.py
    calls it only when ``INVINCIBLE_PERSIST_PENDING_ACTIONS`` is set, and
    then loads existing rows through :meth:`load_persisted`. Writes are
    fire-and-forget tasks on the running loop against the shared PG engine;
    any failure logs a warning and leaves memory as the source of truth -
    staging must never break the MCP flow over a storage problem. The
    default (no engine) is memory-only, so a restart orphans staged
    actions, matching the original design.
    """

    # Default sourced from Settings; tests shrink this via monkeypatch.
    TTL_SECONDS = PENDING_ACTION_TTL_SECONDS

    def __init__(self):
        self._pending: dict = {}  # token -> {"type", "args", "created_at"}
        self._engine = None
        self._background_tasks: set = set()

    def attach_engine(self, engine) -> None:
        """Opt-in persistence target (shared PG engine)."""
        self._engine = engine

    async def flush_persisted(self) -> None:
        """Wait for outstanding fire-and-forget persistence writes.

        Used at shutdown (and by tests) so a clean exit never drops the
        last staged-action writes."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    async def load_persisted(self) -> None:
        """Reload staged actions written by a previous process."""
        if self._engine is None:
            return
        try:
            from invincible.core.db import pending_actions

            async with self.engine_connect() as conn:
                # Explicit columns, not SELECT *: H5 added
                # suspended_workflow_id/deadline to this table for the
                # slow-path ApprovalStore, and a star-select would both
                # break this unpack and resurrect slow-path rows into the
                # fast-path memory store. Slow-path rows are skipped here
                # (the ApprovalStore owns them) — the two paths never
                # resolve each other's tokens.
                rows = (await conn.execute(
                    pending_actions.select().where(
                        pending_actions.c.suspended_workflow_id.is_(None)
                    )
                )).all()
                for token, action_type, args, created_at, _, _ in rows:
                    args = (
                        args if isinstance(args, dict)
                        else json.loads(args)
                    )
                    # Extract (and strip) the persisted staging subject.
                    owner_subject = args.pop(_OWNER_SUBJECT_KEY, None)
                    self._pending[token] = {
                        "type": action_type,
                        "args": args,
                        "created_at": created_at,
                        "owner_subject": owner_subject,
                    }
                self._sweep()
        except Exception as e:
            logger.warning(
                "PendingActionStore persistence load failed (%s); "
                "continuing in memory only", e
            )

    def engine_connect(self):
        return self._engine.connect()

    def _persist(self, coro_factory) -> None:
        """Schedule a best-effort persistence write on the running loop."""
        if self._engine is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _run():
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(coro_factory())
            except Exception as e:
                logger.warning(
                    "Pending action persistence failed (%s); continuing "
                    "in memory only", e
                )
            finally:
                self._background_tasks.discard(task)

        task = loop.create_task(_run())
        self._background_tasks.add(task)

    def _sweep(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - self.TTL_SECONDS
        expired = [
            t for t, record in self._pending.items()
            if record["created_at"] < cutoff
        ]
        for token in expired:
            del self._pending[token]
        if expired:
            self._persist(lambda: delete(pending_actions).where(
                pending_actions.c.token.in_(expired)
            ))

    def put(self, action_type: str, args: dict,
            owner_subject: int | None = None) -> str:
        """Stage an action and return its confirmation token.

        ``owner_subject`` (Phase 2): user id of the principal that staged
        the action; confirmation requires the same subject (a mismatched
        requester sees the token as unknown - indistinguishable from a
        wrong guess).
        """
        self._sweep()
        token = secrets.token_urlsafe(16)
        created = time.time()
        self._pending[token] = {
            "type": action_type,
            "args": args,
            "created_at": created,
            "owner_subject": owner_subject,
        }
        # Persistence keeps the binding INSIDE the JSONB args blob under a
        # reserved key (the pending_actions table predates subjects); it is
        # stripped again on load and never reaches tool execution, which
        # reads specific keys only.
        self._persist(
            lambda: pending_actions.insert().values(
                token=token,
                type=action_type,
                # JSONB column: bind the dict; SQLAlchemy serializes once.
                args={**args, _OWNER_SUBJECT_KEY: owner_subject},
                created_at=created,
            )
        )
        return token

    def take(self, token: str, *,
             requester_subject: int | None = None) -> dict | None:
        """Pop and return the pending record, or None if unknown/expired
        or staged by a DIFFERENT subject.

        A subject mismatch does NOT consume the entry: the legitimate
        owner can still confirm afterwards, while the mismatched caller
        sees exactly an unknown-token answer. Subject-less records
        (pre-Phase-2 legacy rows) are invisible to subject-holding
        requesters - fail closed (audit MEDIUM-2).
        """
        self._sweep()
        record = self._pending.get(token)
        if record is None:
            return None
        if time.time() - record["created_at"] > self.TTL_SECONDS:
            del self._pending[token]
            self._persist(
                lambda: delete(pending_actions).where(
                    pending_actions.c.token == token)
            )
            return None
        owner = record.get("owner_subject")
        if owner is None and requester_subject is not None:
            # Fail closed (multi-tenant audit MEDIUM-2): a subject-less
            # record (staged by a pre-Phase-2 process) must not be
            # confirmable by an authenticated subject. Subject-less
            # requesters keep full access.
            return None
        if owner is not None and requester_subject != owner:
            return None
        del self._pending[token]
        self._persist(
            lambda: delete(pending_actions).where(
                pending_actions.c.token == token)
        )
        return record

    def __len__(self) -> int:
        return len(self._pending)


def check_denylist(command: str) -> None:
    for pattern, reason in DENYLIST_PATTERNS:
        if pattern.search(command):
            raise ToolBlocked(reason)


def _check_protected_path(path: str, patterns: list, verb: str) -> None:
    # Resolve symlinks FIRST (deep code review 2026-09-24, finding 3):
    # abspath only collapses "..", so a link inside the repo named
    # innocently could point at .env and match no pattern, while a link
    # out of the repo could carry an "invincible/..." name that matches
    # one it does not actually target. Matching the resolved path against
    # the resolved repo root keeps this relative-path match honest.
    abs_path = os.path.realpath(os.path.abspath(path))
    try:
        rel = os.path.relpath(abs_path, _REPO_ROOT)
    except ValueError:
        return  # different drive on Windows - can't be inside the repo root
    if rel.startswith(".."):
        return  # outside the repo root - approval (for writes) is the gate here
    rel = rel.replace(os.sep, "/")
    for pattern, reason in patterns:
        if pattern.match(rel):
            raise ToolBlocked(f"{verb} of {reason} ({rel})")


# read_file is sandboxed to a small set of roots: the repo root, the
# process working directory (where `invincible start` was run), and any
# extra directories listed in INVINCIBLE_READ_ROOTS (os.pathsep-separated).
# Anything outside those roots is blocked outright - no approval step -
# because read_file has no approval step to act as the gate. Inside the
# roots, the basename rules below block the files most likely to hold
# actual credentials, wherever in the tree they sit.
_BASENAME_READ_DENYLIST = [
    (re.compile(r"^\.env(\..+)?$", re.I), "an .env file"),
    (re.compile(r"^sessions\.db$", re.I), "the session database"),
    (re.compile(r"^\.git$", re.I), "git internals"),
]


def _allowed_read_roots() -> list:
    roots = [_REPO_ROOT, os.getcwd()]
    roots.extend(settings.read_roots())
    return [
        os.path.normcase(os.path.realpath(os.path.abspath(root)))
        for root in roots
    ]


def check_read_denylist(path: str) -> None:
    # Resolved for the same reason as _check_protected_path: an
    # unresolved link inside an allowed root could point outside it, and
    # one named innocently could point at an excluded file. Both checks
    # below therefore run on the real target.
    abs_path = os.path.realpath(os.path.abspath(path))
    norm = os.path.normcase(abs_path)
    roots = _allowed_read_roots()
    if not any(norm == root or norm.startswith(root + os.sep) for root in roots):
        raise ToolBlocked(
            f"read of path outside the allowed roots ({abs_path}). "
            "Set INVINCIBLE_READ_ROOTS to grant access to other directories."
        )
    for part in abs_path.split(os.sep):
        for pattern, reason in _BASENAME_READ_DENYLIST:
            if pattern.match(part):
                raise ToolBlocked(f"read of {reason} ({abs_path})")
    _check_protected_path(path, READ_DENYLIST_PATTERNS, "read")


def check_write_denylist(path: str) -> None:
    """Block writes to files this project depends on for its own security
    or state. Only applies to paths that resolve *inside* the repo root -
    a write outside the repo entirely is a different risk profile and is
    left to the approval step, same as any other write."""
    _check_protected_path(path, WRITE_DENYLIST_PATTERNS, "write")


async def _run_command(command: str, timeout: float) -> dict:
    """Actually run a shell command. Only reached after approval."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
                "returncode": -1,
            }

        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "returncode": proc.returncode,
        }
    except Exception as e:
        logger.error(f"execute_bash failed: {e}")
        return {"stdout": "", "stderr": str(e), "returncode": -1}


async def _write_file(path: str, content: str) -> dict:
    """Actually write a file. Only reached after approval."""
    try:
        dirname = os.path.dirname(os.path.abspath(path))
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "written", "path": path, "bytes": len(content)}
    except Exception as e:
        logger.error(f"write_file failed: {e}")
        return {"status": "error", "error": str(e)}


def execute_bash(
    command: str,
    store: PendingActionStore,
    timeout: float = 30.0,
    owner_subject: int | None = None,
) -> dict:
    """Stage a shell command for approval; nothing runs until confirmed.

    The denylist check happens first and raises ``ToolBlocked`` (no token
    is ever issued for a blocked command). A passing command is stored in
    ``store`` under a fresh token and the caller gets a
    ``pending_confirmation`` response; the caller must then call
    :func:`confirm_action` with that token to run (or discard) it.
    """
    check_denylist(command)  # raises ToolBlocked; caller maps it to a response

    token = store.put(
        "execute_bash",
        {"command": command, "timeout": timeout},
        owner_subject=owner_subject,
    )
    print(f'[MCP] Pending {token}: execute_bash "{command}"')
    return {
        "status": "pending_confirmation",
        "token": token,
        "action": "execute_bash",
        "command": command,
        "message": (
            "Call confirm_action with this token "
            "(approve=true/false) to proceed."
        ),
    }


def write_file(
    path: str,
    content: str,
    store: PendingActionStore,
    owner_subject: int | None = None,
) -> dict:
    """Stage a file write for approval; nothing is written until confirmed.

    Same shape as :func:`execute_bash`: denylist first (``ToolBlocked``,
    no token), then a ``pending_confirmation`` response carrying a token
    the caller must confirm via :func:`confirm_action`.
    """
    check_write_denylist(path)  # raises ToolBlocked; caller maps it to a response

    token = store.put(
        "write_file",
        {"path": path, "content": content},
        owner_subject=owner_subject,
    )
    print(f"[MCP] Pending {token}: write_file {path} ({len(content)} bytes)")
    return {
        "status": "pending_confirmation",
        "token": token,
        "action": "write_file",
        "path": path,
        "content_length": len(content),
        "message": (
            "Call confirm_action with this token "
            "(approve=true/false) to proceed."
        ),
    }


async def confirm_action(
    store: PendingActionStore,
    token: str,
    approve: bool,
    requester_subject: int | None = None,
    executor=None,
) -> dict:
    """Resolve a staged action by token.

    ``requester_subject`` (Phase 2): a token staged by another subject
    resolves as not_found - existence never leaks across users.

    ``executor`` (Phase 10): an optional ``async (action_type, args)
    -> dict`` callback that replaces local execution of execute_bash /
    write_file. The mcp endpoint passes one when agent routing is on,
    forwarding the confirmed work to the caller's paired agent instead
    of running it on the server host. Local execution paths below are
    the unchanged fallback (and the only path when routing is off, so
    every pre-Phase-10 test and workflow behaves byte-identically).

    Returns a dict the endpoint maps to an MCP result:
    ``{"status": "not_found"}`` for an unknown/expired/already-used token,
    ``{"status": "declined"}`` when ``approve`` is false, or the real
    action result (as :func:`execute_bash`/:func:`write_file` used to
    return synchronously) when approved. The record is popped regardless,
    so a token can never resolve twice.
    """
    record = store.take(token, requester_subject=requester_subject)
    if record is None:
        return {"status": "not_found"}
    if not approve:
        return {"status": "declined"}
    if record["type"] in ("execute_bash", "write_file") and executor is not None:
        return await executor(record["type"], record["args"])
    if record["type"] == "execute_bash":
        args = record["args"]
        return await _run_command(
            args.get("command", ""), args.get("timeout", 30.0)
        )
    if record["type"] == "write_file":
        args = record["args"]
        return await _write_file(args.get("path", ""), args.get("content", ""))
    return {
        "status": "error",
        "error": f"Unknown pending action type: {record['type']}",
    }


async def read_file(path: str) -> dict:
    """No approval step - reading isn't destructive, so the friction
    wouldn't buy anything. The sandbox is the gate instead: reads are only
    allowed under the repo root, the server's working directory, and any
    INVINCIBLE_READ_ROOTS directories, with .env / sessions.db / .git
    blocked by name anywhere inside them. Reading invincible/ and tests/
    and providers.yaml stays allowed, since letting the cloud AI see the
    code is the entire point of this tool."""
    check_read_denylist(path)  # raises ToolBlocked; caller maps it to a response

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"status": "read", "path": path, "content": content}
    except FileNotFoundError:
        return {"status": "error", "error": f"File not found: {path}"}
    except IsADirectoryError:
        return {"status": "error", "error": f"Path is a directory, not a file: {path}"}
    except Exception as e:
        logger.error(f"read_file failed: {e}")
        return {"status": "error", "error": str(e)}


# --- Harness H6a: read-only machine tools ------------------------------------
# code_search / process_list / screenshot are NON-destructive like
# read_file, so they carry no confirm_action gate — the sandbox (server
# read roots, or the agent's home sandbox when routed) is the gate for
# anything path-shaped. The private ``_`` helpers below are the EXACT
# functions both sides run (same pattern as ``_run_command`` /
# ``_write_file``): the server calls them after its own checks, the agent
# runner after its sandbox re-check, so result shapes are byte-identical
# wherever the work happens.

SEARCH_DEFAULT_MAX_RESULTS = 20
SEARCH_MAX_RESULTS_CAP = 50
# Files bigger than this are skipped, not read (same spirit as the read
# sandbox: bounded work per call, no giant-file context blowups).
SEARCH_FILE_BYTES_CAP = 256 * 1024
SEARCH_TIMEOUT_SECONDS = 15.0
# Directory names never descended into by the Python fallback (rg honors
# .gitignore + skips hidden on its own; both skip these explicitly).
_SEARCH_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
    "build", ".pytest_cache", ".ruff_cache",
})

PROCESSES_DEFAULT_LIMIT = 50
PROCESSES_LIMIT_CAP = 200

# Screenshot framing for UI-validation captures.
SCREENSHOT_WIDTH = 1280
SCREENSHOT_HEIGHT = 800
SCREENSHOT_TIMEOUT_SECONDS = 30.0
SCREENSHOT_MAX_BYTES = 2 * 1024 * 1024
_CHROME_BINARIES = (
    "google-chrome", "google-chrome-stable", "chrome", "chromium",
    "chromium-browser", "chrome.exe", "chromium.exe",
    "msedge", "msedge.exe", "microsoft-edge",
    "brave", "brave.exe", "brave-browser",
)

_CHROME_HINT = (
    "Install Chrome/Chromium/Edge, or point "
    "INVINCIBLE_CHROME_BIN at the browser binary "
    "and restart `invincible harness connect`."
)


def _chrome_common_paths() -> list:
    """Well-known install locations (browser installers rarely touch PATH).

    Covers the default per-machine spots on each OS so users with a
    normal install — and users who installed somewhere the installer
    chose — do not need any configuration. Custom locations beyond
    these are handled by INVINCIBLE_CHROME_BIN and the Windows
    App-Paths registry below.
    """
    system = platform.system().lower()
    candidates: list = []
    if system == "windows":
        for base in (
            os.getenv("PROGRAMFILES", r"C:\Program Files"),
            os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.getenv("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            candidates.extend([
                os.path.join(base, "Google", "Chrome",
                             "Application", "chrome.exe"),
                os.path.join(base, "Microsoft", "Edge",
                             "Application", "msedge.exe"),
                os.path.join(base, "Chromium",
                             "Application", "chrome.exe"),
                os.path.join(base, "BraveSoftware", "Brave-Browser",
                             "Application", "brave.exe"),
            ])
    elif system == "darwin":
        for root in ("/Applications",
                     os.path.expanduser("~/Applications")):
            candidates.extend([
                os.path.join(root, "Google Chrome.app", "Contents",
                             "MacOS", "Google Chrome"),
                os.path.join(root, "Chromium.app", "Contents",
                             "MacOS", "Chromium"),
                os.path.join(root, "Microsoft Edge.app", "Contents",
                             "MacOS", "Microsoft Edge"),
                os.path.join(root, "Brave Browser.app", "Contents",
                             "MacOS", "Brave Browser"),
            ])
    else:
        candidates.extend([
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            "/usr/bin/microsoft-edge",
            "/usr/bin/brave-browser",
            os.path.expanduser("~/.local/bin/google-chrome"),
            os.path.expanduser("~/.local/bin/chromium"),
        ])
    return candidates


def _chrome_registry_candidates() -> list:
    """Windows App-Paths lookups (HKLM + HKCU).

    The registry records the real binary path even when the user
    picked a custom install directory during setup, which neither
    PATH nor the well-known list can see. Non-Windows platforms and
    machines without winreg return [].
    """
    try:
        import winreg
    except ImportError:
        return []
    found: list = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for value in ("chrome.exe", "msedge.exe", "chromium.exe",
                      "brave.exe"):
            try:
                with winreg.OpenKey(
                    root,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                    r"\App Paths\\" + value,
                ) as key:
                    path, _ = winreg.QueryValueEx(key, "")
                if path:
                    # Registry values may be quoted and carry args.
                    cleaned = path.strip().strip('"')
                    found.append(cleaned.split('"')[0].strip())
            except OSError:
                continue
    return found


def _find_chrome() -> str | None:
    """Browser binary for screenshots, or None.

    Order (first hit wins, so explicit config beats guessing):
    1. INVINCIBLE_CHROME_BIN (per-machine override, no source edit),
    2. PATH (shutil.which over _CHROME_BINARIES),
    3. OS well-known install paths,
    4. Windows App-Paths registry (custom install directories).
    """
    override = settings.chrome_bin()
    if override:
        expanded = os.path.expanduser(os.path.expandvars(override))
        if os.path.isfile(expanded):
            return expanded
    for name in _CHROME_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    for candidate in _chrome_common_paths():
        if candidate and os.path.isfile(candidate):
            return candidate
    for candidate in _chrome_registry_candidates():
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _is_text_sample(path: str) -> bool:
    """Null-byte probe: binary files are skipped, never decoded."""
    try:
        with open(path, "rb") as f:
            return b"\x00" not in f.read(8192)
    except OSError:
        return False


def _walk_search(
    pattern: str, root: str, max_results: int
) -> tuple[list, int, bool]:
    """Synchronous fallback search (run in a thread). Case-insensitive
    substring match per line. Returns (hits, files_searched, truncated)."""
    lowered = pattern.lower()
    hits: list = []
    files_searched = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _SEARCH_SKIP_DIRS
        )
        for filename in sorted(filenames):
            if len(hits) >= max_results:
                truncated = True
                return hits, files_searched, truncated
            full = os.path.join(dirpath, filename)
            try:
                if os.path.getsize(full) > SEARCH_FILE_BYTES_CAP:
                    continue
            except OSError:
                continue
            if not _is_text_sample(full):
                continue
            files_searched += 1
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, start=1):
                        if lowered in line.lower():
                            hits.append({
                                "path": full,
                                "line": lineno,
                                "text": line.strip()[:300],
                            })
                            if len(hits) >= max_results:
                                truncated = True
                                return hits, files_searched, truncated
            except OSError:
                continue
    return hits, files_searched, truncated


async def _search_with_rg(
    pattern: str, root: str, max_results: int
) -> tuple[list, int, bool] | None:
    """ripgrep fast path (``rg --json``). None when rg is absent or fails
    (the caller falls back to the walker) — never raises into the tool."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "rg", "--json", "-m", str(max_results), "-S", pattern, root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, NotImplementedError):
        return None
    try:
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=SEARCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        if proc.returncode not in (0, 1):  # 1 = no matches, fine
            return None
        hits: list = []
        files: set = set()
        for raw in stdout.decode(errors="replace").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data", {})
            path = (
                data.get("path", {}).get("text", "")
                if isinstance(data.get("path"), dict)
                else str(data.get("path", ""))
            )
            lines = data.get("lines", {})
            text = ""
            if isinstance(lines, dict):
                text = str(lines.get("text", "")).strip()[:300]
            hits.append({
                "path": path,
                "line": int(data.get("line_number", 0)),
                "text": text,
            })
            files.add(path)
            if len(hits) >= max_results:
                break
        return hits, len(files), len(hits) >= max_results
    except Exception:
        return None


async def _search_code(
    pattern: str, path: str, max_results: int
) -> dict:
    """Search path for pattern. Path is ASSUMED vetted by the caller
    (server read denylist or agent home sandbox) — this function only
    executes."""
    if not pattern.strip():
        return {"status": "error", "error": "pattern must be non-empty"}
    limit = max(1, min(int(max_results or SEARCH_DEFAULT_MAX_RESULTS),
                       SEARCH_MAX_RESULTS_CAP))
    target = os.path.abspath(os.path.expanduser(path or "."))
    if os.path.isfile(target):
        if not _is_text_sample(target):
            return {"status": "search", "pattern": pattern, "path": path,
                    "hits": [], "truncated": False, "files_searched": 0}
        result = await _search_with_rg(pattern, target, limit)
        if result is not None:
            hits, files, truncated = result
            return {"status": "search", "pattern": pattern, "path": path,
                    "hits": hits, "truncated": truncated,
                    "files_searched": files}
        hits, _, truncated = await asyncio.to_thread(
            _walk_search, pattern, os.path.dirname(target), limit)
        hits = [h for h in hits if h["path"] == target][:limit]
        return {"status": "search", "pattern": pattern, "path": path,
                "hits": hits, "truncated": truncated,
                "files_searched": 1}
    if not os.path.isdir(target):
        return {"status": "error", "error": f"Path not found: {path}"}
    result = await _search_with_rg(pattern, target, limit)
    if result is None:
        hits, files, truncated = await asyncio.to_thread(
            _walk_search, pattern, target, limit)
        return {"status": "search", "pattern": pattern, "path": path,
                "hits": hits, "truncated": truncated,
                "files_searched": files}
    hits, files, truncated = result
    return {"status": "search", "pattern": pattern, "path": path,
            "hits": hits, "truncated": truncated,
            "files_searched": files}


async def search_code(
    pattern: str, path: str, max_results: int = SEARCH_DEFAULT_MAX_RESULTS
) -> dict:
    """Server-side entry: read-denylist gate, then shared search."""
    check_read_denylist(path or ".")  # raises ToolBlocked
    return await _search_code(pattern, path, max_results)


async def _list_processes(limit: int) -> dict:
    """Process table via stdlib subprocess only. No paths, no network —
    safe to run on either side; the routing posture (server-local vs
    agent) is the same as every other machine-plane tool."""
    capped = max(1, min(int(limit or PROCESSES_DEFAULT_LIMIT),
                        PROCESSES_LIMIT_CAP))
    try:
        if os.name == "nt":
            proc = await asyncio.create_subprocess_exec(
                "tasklist", "/FO", "CSV", "/NH",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=SEARCH_TIMEOUT_SECONDS)
            rows = []
            for line in stdout.decode(errors="replace").splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2:
                    try:
                        pid = int(parts[1])
                    except ValueError:
                        continue
                    rows.append({
                        "pid": pid, "name": parts[0],
                        "mem": parts[4] if len(parts) > 4 else "",
                    })
                    if len(rows) >= capped:
                        break
            return {"status": "processes", "processes": rows,
                    "truncated": len(rows) >= capped}
        proc = await asyncio.create_subprocess_shell(
            "ps -eo pid=,comm=,etime=,pcpu=,pmem=,args=",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=SEARCH_TIMEOUT_SECONDS)
        rows = []
        for line in stdout.decode(errors="replace").splitlines():
            parts = line.split(None, 5)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            rows.append({
                "pid": pid,
                "name": parts[1],
                "elapsed": parts[2] if len(parts) > 2 else "",
                "cpu": parts[3] if len(parts) > 3 else "",
                "mem": parts[4] if len(parts) > 4 else "",
                "cmd": (parts[5] if len(parts) > 5 else "")[:200],
            })
            if len(rows) >= capped:
                break
        return {"status": "processes", "processes": rows,
                "truncated": len(rows) >= capped}
    except asyncio.TimeoutError:
        return {"status": "error",
                "error": "process listing timed out"}
    except Exception as e:
        logger.error(f"process listing failed: {e}")
        return {"status": "error", "error": str(e)}


async def _take_screenshot(url: str, timeout: float) -> dict:
    """Headless-Chrome capture of an http(s) URL. Runs ONLY on the user's
    own machine (agent-routed): the server never fetches caller-supplied
    URLs, so this path cannot become an SSRF primitive. Non-http(s) URLs
    are refused outright."""
    if not re.match(r"^https?://", url.strip(), re.I):
        return {"status": "error",
                "error": "screenshot URL must start with http:// or https://"}
    chrome = _find_chrome()
    if chrome is None:
        return {"status": "unavailable",
                "reason": "No Chrome/Chromium/Edge binary found on this "
                          "machine (checked INVINCIBLE_CHROME_BIN, PATH, "
                          "well-known install paths, and the Windows "
                          "App-Paths registry). " + _CHROME_HINT}
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".png", delete=False
        ) as handle:
            tmp = handle.name
        proc = await asyncio.create_subprocess_exec(
            chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
            f"--window-size={SCREENSHOT_WIDTH},{SCREENSHOT_HEIGHT}",
            f"--screenshot={tmp}", "--virtual-time-budget=5000", url,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"status": "error",
                    "error": f"screenshot timed out after {timeout}s"}
        try:
            with open(tmp, "rb") as f:
                data = f.read()
        except OSError:
            return {"status": "error",
                    "error": "Chrome produced no screenshot "
                             f"({stderr.decode(errors='replace')[:200]})"}
        if len(data) > SCREENSHOT_MAX_BYTES:
            return {"status": "error",
                    "error": f"screenshot too large ({len(data)} bytes, "
                             f"cap {SCREENSHOT_MAX_BYTES})"}
        return {"status": "screenshot", "mime": "image/png",
                "data_b64": base64.b64encode(data).decode("ascii"),
                "bytes": len(data), "url": url}
    except Exception as e:
        logger.error(f"screenshot failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
