# invincible/core/tool_executor.py
"""Execution layer for MCP tools (execute_bash, write_file, edit_file).

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
import fnmatch
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
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
# Unresolved twin of _REPO_ROOT for the given-path side of the
# edit_file dual check (resolving one side only would mis-relativize
# every path whenever the checkout is reached through a symlink).
_REPO_ROOT_UNRESOLVED = os.path.abspath(os.path.dirname(
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


# Step 3: model-facing next-step hints. Block reasons surface to the model
# inside tool results ("Blocked: <reason>"), so each reason class carries
# a one-sentence directive saying what to do INSTEAD — the live prompt
# already tells the model not to retry blocked calls another way; these
# hints make the blocked result itself actionable.
HINT_BLOCKED_COMMAND = (
    "Pick a non-destructive alternative; this block cannot be approved "
    "away, so do not reword the same action to evade it."
)
HINT_BLOCKED_PATH = (
    "Use a different path, or ask the user for an allowed one — do not "
    "try alternate spellings or copies of this path."
)
HINT_BLOCKED_OUTSIDE_ROOTS = (
    "Ask the user to place the file under the working directory or an "
    "allowed root, or pick a different file."
)


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

    def peek_type(self, token: str, *,
                  requester_subject: int | None = None) -> str | None:
        """Non-consuming type check with the exact subject semantics of
        :meth:`take` (unknown/expired/mismatched-subject reads as None).

        Lets a dispatcher pick a type-specific executor without popping
        the record — e.g. tunnel confirms need their executor even when
        agent routing is off (the executor itself refuses), while
        execute_bash must still fall through to local execution.
        """
        self._sweep()
        record = self._pending.get(token or "")
        if record is None:
            return None
        if time.time() - record["created_at"] > self.TTL_SECONDS:
            return None
        owner = record.get("owner_subject")
        if owner is None and requester_subject is not None:
            return None
        if owner is not None and requester_subject != owner:
            return None
        return record["type"]

    def __len__(self) -> int:
        return len(self._pending)


def check_denylist(command: str) -> None:
    for pattern, reason in DENYLIST_PATTERNS:
        if pattern.search(command):
            raise ToolBlocked(f"{reason}. {HINT_BLOCKED_COMMAND}")


def _check_protected_path(
    path: str, patterns: list, verb: str, *, resolve: bool = True
) -> None:
    # Resolve symlinks FIRST (deep code review 2026-09-24, finding 3):
    # abspath only collapses "..", so a link inside the repo named
    # innocently could point at .env and match no pattern, while a link
    # out of the repo could carry an "invincible/..." name that matches
    # one it does not actually target. Matching the resolved path against
    # the resolved repo root keeps this relative-path match honest.
    # With resolve=False the UNRESOLVED given path is matched instead
    # (edit_file's dual check runs both sides).
    if resolve:
        abs_path = os.path.realpath(os.path.abspath(path))
        root = _REPO_ROOT
    else:
        abs_path = os.path.abspath(path)
        root = _REPO_ROOT_UNRESOLVED
    try:
        rel = os.path.relpath(abs_path, root)
    except ValueError:
        return  # different drive on Windows - can't be inside the repo root
    if rel.startswith(".."):
        return  # outside the repo root - approval (for writes) is the gate here
    rel = rel.replace(os.sep, "/")
    for pattern, reason in patterns:
        if pattern.match(rel):
            raise ToolBlocked(
                f"{verb} of {reason} ({rel}). {HINT_BLOCKED_PATH}")


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
            "Set INVINCIBLE_READ_ROOTS to grant access to other directories. "
            f"{HINT_BLOCKED_OUTSIDE_ROOTS}"
        )
    for part in abs_path.split(os.sep):
        for pattern, reason in _BASENAME_READ_DENYLIST:
            if pattern.match(part):
                raise ToolBlocked(
                    f"read of {reason} ({abs_path}). {HINT_BLOCKED_PATH}")
    _check_protected_path(path, READ_DENYLIST_PATTERNS, "read")


def check_write_denylist(path: str) -> None:
    """Block writes to files this project depends on for its own security
    or state. Only applies to paths that resolve *inside* the repo root -
    a write outside the repo entirely is a different risk profile and is
    left to the approval step, same as any other write."""
    _check_protected_path(path, WRITE_DENYLIST_PATTERNS, "write")


def check_edit_denylist(path: str) -> None:
    """Dual denylist for edit_file: BOTH the path the caller gave and the
    resolved symlink target must pass. The resolved side alone misses a
    link NAMED inside a protected dir (e.g. ``invincible/evil-link`` ->
    ``/tmp/out.txt``); the given side alone misses a link named
    innocently pointing AT a protected file. Same patterns as writes."""
    _check_protected_path(path, WRITE_DENYLIST_PATTERNS, "edit")
    _check_protected_path(
        path, WRITE_DENYLIST_PATTERNS, "edit", resolve=False)


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


EDIT_FILE_BYTES_CAP = 1024 * 1024


def _edit_preview(new_norm: str, match_at: int) -> str:
    """~3 lines of context around the first replacement (bounded)."""
    lines = new_norm.split("\n")
    pos = 0
    idx = 0
    for i, line in enumerate(lines):
        pos += len(line) + 1
        if pos > match_at:
            idx = i
            break
    start = max(0, idx - 1)
    snippet = "\n".join(lines[start:start + 3])
    if len(snippet) > 500:
        snippet = snippet[:500] + "…"
    return snippet


async def _edit_file(
    path: str, old_string: str, new_string: str, replace_all: bool = False
) -> dict:
    """Exact-match replace in an EXISTING file. Only reached after approval.

    Windows-safe: matches newline-insensitively, writes back with the
    file's ORIGINAL line endings, keeps any BOM, atomic replace.
    A symlink edits its TARGET (the link itself is never replaced) and
    permission bits are preserved via copymode. Errors are actionable
    strings the model can react to (no exceptions except ToolBlocked,
    which is raised by the caller before staging).
    """
    if not isinstance(old_string, str) or not old_string:
        return {"status": "error",
                "error": "old_string must be a non-empty string"}
    if not isinstance(path, str) or not path.strip():
        return {"status": "error", "error": f"File not found: {path}"}
    if old_string == new_string:
        return {"status": "error",
                "error": "old_string and new_string are identical"}
    try:
        if os.path.isdir(path):
            return {"status": "error",
                    "error": f"Path is a directory, not a file: {path}"}
        size = os.path.getsize(path)
    except FileNotFoundError:
        return {"status": "error", "error": f"File not found: {path}"}
    except OSError as e:
        return {"status": "error", "error": str(e)}
    if size > EDIT_FILE_BYTES_CAP:
        return {"status": "error",
                "error": f"file too large ({size} bytes, "
                         f"cap {EDIT_FILE_BYTES_CAP})"}
    try:
        with open(path, "rb") as f:
            raw = f.read(EDIT_FILE_BYTES_CAP + 1)
    except FileNotFoundError:
        return {"status": "error", "error": f"File not found: {path}"}
    except OSError as e:
        return {"status": "error", "error": str(e)}
    if b"\x00" in raw[:8192]:
        return {"status": "error",
                "error": "binary file: edit_file is for text files only"}
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"status": "error",
                "error": "cannot decode file as UTF-8"}
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    ending = "\r\n" if crlf > 0 and crlf >= lf else "\n"
    norm_text = text.replace("\r\n", "\n").replace("\r", "\n")
    norm_old = old_string.replace("\r\n", "\n").replace("\r", "\n")
    norm_new = (new_string if isinstance(new_string, str) else "")
    norm_new = norm_new.replace("\r\n", "\n").replace("\r", "\n")
    if not norm_old:
        return {"status": "error",
                "error": "old_string must be a non-empty string"}
    if norm_old == norm_new:
        return {"status": "error",
                "error": "old_string and new_string are identical"}
    count = norm_text.count(norm_old)
    if count == 0:
        return {"status": "error",
                "error": f"old_string not found in {path}"}
    if count > 1 and not replace_all:
        return {"status": "error",
                "error": f"old_string matches {count} times: add "
                         "surrounding context or set replace_all"}
    if replace_all:
        new_norm = norm_text.replace(norm_old, norm_new)
        n = count
    else:
        new_norm = norm_text.replace(norm_old, norm_new, 1)
        n = 1
    out_text = new_norm.replace("\n", ending) if ending == "\r\n" else new_norm
    out_bytes = (b"\xef\xbb\xbf" + out_text.encode("utf-8")
                 if had_bom else out_text.encode("utf-8"))
    match_at = new_norm.find(norm_new[:60]) if norm_new else 0
    if match_at < 0:
        match_at = 0
    preview = _edit_preview(new_norm, match_at)
    tmp = None
    try:
        # Edit the TARGET when path is a symlink: replacing `path`
        # directly would swap the link for a regular file.
        real = os.path.realpath(os.path.abspath(path))
        parent = os.path.dirname(real) or "."
        with tempfile.NamedTemporaryFile(
            dir=parent, delete=False
        ) as handle:
            tmp = handle.name
            handle.write(out_bytes)
        shutil.copymode(real, tmp)
        os.replace(tmp, real)
        tmp = None
        return {"status": "edited", "path": path, "replacements": n,
                "preview": preview}
    except Exception as e:
        logger.error(f"edit_file failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


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


def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    store: PendingActionStore,
    replace_all: bool = False,
    owner_subject: int | None = None,
) -> dict:
    """Stage an exact-match file edit for approval.

    Creating files stays ``write_file``; this only edits EXISTING files
    (missing-file and match errors surface after approval, as the
    action result). Same denylist + approval + routing as ``write_file``
    (both the given path and the resolved symlink target are checked).
    """
    check_edit_denylist(path)  # raises ToolBlocked; caller maps it to a response

    token = store.put(
        "edit_file",
        {"path": path, "old_string": old_string,
         "new_string": new_string, "replace_all": bool(replace_all)},
        owner_subject=owner_subject,
    )
    print(f"[MCP] Pending {token}: edit_file {path}")
    return {
        "status": "pending_confirmation",
        "token": token,
        "action": "edit_file",
        "path": path,
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
    if record["type"] in COMPUTER_TOOLS:
        # Pack 3 computer-use: agent-only by design — there is no local
        # execution path (the server has no desktop to click). Without
        # an agent executor this is unavailability, never a local run.
        if executor is None:
            return {"status": "unavailable",
                    "reason": _COMPUTER_UNAVAILABLE_REASON}
        return await executor(record["type"], record["args"])
    if (record["type"] in ("execute_bash", "write_file", "edit_file",
                           "expose_port")
            and executor is not None):
        return await executor(record["type"], record["args"])
    if record["type"] == "execute_bash":
        args = record["args"]
        return await _run_command(
            args.get("command", ""), args.get("timeout", 30.0)
        )
    if record["type"] == "write_file":
        args = record["args"]
        return await _write_file(args.get("path", ""), args.get("content", ""))
    if record["type"] == "edit_file":
        args = record["args"]
        return await _edit_file(
            args.get("path", ""), args.get("old_string", ""),
            args.get("new_string", ""), bool(args.get("replace_all", False)),
        )
    return {
        "status": "error",
        "error": f"Unknown pending action type: {record['type']}",
    }


READ_FILE_CONTENT_CHAR_CAP = 64 * 1024


async def _read_file(path: str, offset: int | None = None,
                     limit: int | None = None) -> dict:
    """Line-paged read. ``offset`` is the 1-based first line (default 1);
    ``limit`` caps how many lines come back (default: the whole file).
    The 64K character cap still bounds the returned window, and
    ``truncated`` is true whenever content past the window exists —
    more lines, or more characters, than were returned. The default call
    (no offset/limit) reproduces the legacy whole-file read exactly."""
    try:
        first_line = int(offset) if offset is not None else 1
    except (TypeError, ValueError):
        first_line = 1
    first_line = max(first_line, 1)
    try:
        max_lines = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        max_lines = None
    if max_lines is not None:
        max_lines = max(max_lines, 0)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(first_line - 1):
                if not f.readline():
                    return {"status": "read", "path": path,
                            "content": "", "truncated": False}
            chunks: list[str] = []
            chars = 0
            lines_taken = 0
            truncated = False
            while max_lines is None or lines_taken < max_lines:
                line = f.readline()
                if not line:
                    break
                room = READ_FILE_CONTENT_CHAR_CAP + 1 - chars
                if room <= 0:
                    truncated = True
                    break
                if len(line) > room:
                    chunks.append(line[:room])
                    chars += room
                    truncated = True
                    break
                chunks.append(line)
                chars += len(line)
                lines_taken += 1
            if (not truncated and max_lines is not None
                    and lines_taken >= max_lines and f.read(1)):
                truncated = True
            content = "".join(chunks)
            if len(content) > READ_FILE_CONTENT_CHAR_CAP:
                content = content[:READ_FILE_CONTENT_CHAR_CAP]
                truncated = True
            return {
                "status": "read",
                "path": path,
                "content": content,
                "truncated": truncated,
            }
    except FileNotFoundError:
        return {"status": "error", "error": f"File not found: {path}"}
    except IsADirectoryError:
        return {"status": "error",
                "error": f"Path is a directory, not a file: {path}"}
    except Exception as e:
        logger.error(f"read_file failed: {e}")
        return {"status": "error", "error": str(e)}


async def read_file(path: str, offset: int | None = None,
                    limit: int | None = None) -> dict:
    """No approval step - reading isn't destructive, so the friction
    wouldn't buy anything. The sandbox is the gate instead: reads are only
    allowed under the repo root, the server's working directory, and any
    INVINCIBLE_READ_ROOTS directories, with .env / sessions.db / .git
    blocked by name anywhere inside them. Reading invincible/ and tests/
    and providers.yaml stays allowed, since letting the cloud AI see the
    code is the entire point of this tool."""
    check_read_denylist(path)  # raises ToolBlocked; caller maps it to a response

    return await _read_file(path, offset, limit)


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
# Plain-text URL fetching for read-heavy research (agent-side only).
WEB_FETCH_TIMEOUT_SECONDS = 30.0
WEB_FETCH_MAX_BYTES = 1 * 1024 * 1024
_CHROME_BINARIES = (
    "google-chrome", "google-chrome-stable", "chrome", "chromium",
    "chromium-browser", "chrome.exe", "chromium.exe",
    "msedge", "msedge.exe", "microsoft-edge",
    "brave", "brave.exe", "brave-browser",
)

_CHROME_HINT = (
    "Install Chrome/Chromium/Edge, or point "
    "INVINCIBLE_CHROME_BIN at the browser binary "
    "and restart `invincible connect`."
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


LIST_DIR_DEFAULT_LIMIT = 100
LIST_DIR_LIMIT_CAP = 500

GIT_LOG_DEFAULT_LIMIT = 20
GIT_LOG_LIMIT_CAP = 50
GIT_TIMEOUT_SECONDS = 15.0
# Unstaged diffs bigger than this are truncated, not sent (same spirit
# as the search file-bytes cap: bounded work per call, no context
# blowups from a vendored-directory diff).
GIT_DIFF_BYTES_CAP = 100 * 1024


async def _list_dir(path: str, limit: int,
                    show_hidden: bool = False) -> dict:
    """List one directory's entries. Path is ASSUMED vetted by the
    caller (server read denylist or agent home sandbox) — this
    function only executes. Entry names (not contents) are returned,
    dirs-first then case-insensitive alpha, capped with a flag."""
    try:
        capped = max(1, min(int(limit or LIST_DIR_DEFAULT_LIMIT),
                            LIST_DIR_LIMIT_CAP))
    except (TypeError, ValueError):
        capped = LIST_DIR_DEFAULT_LIMIT
    target = os.path.abspath(os.path.expanduser(path or "."))
    try:
        with os.scandir(target) as it:
            entries = []
            for entry in it:
                if not show_hidden and entry.name.startswith("."):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    size = 0
                    if not is_dir:
                        size = entry.stat(
                            follow_symlinks=False).st_size
                except OSError:
                    is_dir, size = False, 0
                entries.append({
                    "name": entry.name,
                    "type": "dir" if is_dir else "file",
                    "size": size,
                })
    except FileNotFoundError:
        return {"status": "error",
                "error": f"Directory not found: {path}"}
    except NotADirectoryError:
        return {"status": "error",
                "error": f"Path is not a directory: {path}"}
    except Exception as e:
        logger.error(f"list_dir failed: {e}")
        return {"status": "error", "error": str(e)}
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"status": "directory", "path": path,
            "entries": entries[:capped],
            "truncated": len(entries) > capped}


async def list_dir(path: str, limit: int = LIST_DIR_DEFAULT_LIMIT,
                   show_hidden: bool = False) -> dict:
    """Server-side entry: read-denylist gate, then shared listing."""
    check_read_denylist(path or ".")  # raises ToolBlocked
    return await _list_dir(path, limit, show_hidden)


FIND_DEFAULT_MAX_RESULTS = 20
FIND_MAX_RESULTS_CAP = 50


def _walk_find(
    pattern: str, root: str, max_results: int, show_hidden: bool = False
) -> tuple[list, int, bool]:
    """Synchronous basename-glob walk (run in a thread). Case-sensitive
    ``fnmatch.fnmatchcase`` on the basename at any depth. Returns
    (matches, files_scanned, truncated) with absolute-path matches."""
    matches: list = []
    files_scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SEARCH_SKIP_DIRS
            and (show_hidden or not d.startswith("."))
        )
        for filename in sorted(filenames):
            if not show_hidden and filename.startswith("."):
                continue
            full = os.path.join(dirpath, filename)
            files_scanned += 1
            if fnmatch.fnmatchcase(filename, pattern):
                matches.append(full)
                if len(matches) >= max_results:
                    truncated = True
                    return matches, files_scanned, truncated
    return matches, files_scanned, truncated


async def _find_files(
    pattern: str, path: str, max_results: int, show_hidden: bool = False
) -> dict:
    """Find files by basename glob under path. Path is ASSUMED vetted
    by the caller (server read denylist or agent home sandbox) — this
    function only executes."""
    if not isinstance(pattern, str) or not pattern.strip():
        return {"status": "error", "error": "pattern must be non-empty"}
    if "/" in pattern or "\\" in pattern:
        return {"status": "error",
                "error": "pattern must be a basename glob, not a path"}
    try:
        limit = max(1, min(int(max_results or FIND_DEFAULT_MAX_RESULTS),
                           FIND_MAX_RESULTS_CAP))
    except (TypeError, ValueError):
        limit = FIND_DEFAULT_MAX_RESULTS
    target = os.path.abspath(os.path.expanduser(path or "."))
    if os.path.isfile(target):
        if fnmatch.fnmatchcase(os.path.basename(target), pattern):
            return {"status": "files", "pattern": pattern, "path": path,
                    "matches": [target], "truncated": False,
                    "files_scanned": 1}
        return {"status": "files", "pattern": pattern, "path": path,
                "matches": [], "truncated": False, "files_scanned": 1}
    if not os.path.isdir(target):
        return {"status": "error", "error": f"Path not found: {path}"}
    matches, scanned, truncated = await asyncio.to_thread(
        _walk_find, pattern, target, limit, bool(show_hidden))
    return {"status": "files", "pattern": pattern, "path": path,
            "matches": matches, "truncated": truncated,
            "files_scanned": scanned}


async def find_files(
    pattern: str, path: str, max_results: int = FIND_DEFAULT_MAX_RESULTS,
    show_hidden: bool = False,
) -> dict:
    """Server-side entry: read-denylist gate, then shared finder."""
    check_read_denylist(path or ".")  # raises ToolBlocked
    return await _find_files(pattern, path, max_results, show_hidden)


async def _git_run(path: str, args: list,
                   timeout: float = GIT_TIMEOUT_SECONDS) -> dict:
    """Run one git command under ``path``. Returns
    ``{"ok": True, "stdout": ...}`` or ``{"ok": False, "error": ...}``
    — never raises, so dispatch shapes stay uniform. A non-zero exit
    (not a repo, unknown revision) is an error result, not a block."""
    target = os.path.abspath(os.path.expanduser(path or "."))
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", target, *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"ok": False,
                "error": "git is not installed on this machine"}
    except Exception as e:
        logger.error(f"git command failed: {e}")
        return {"ok": False, "error": str(e)}
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False,
                "error": f"git command timed out after {timeout}s"}
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        return {"ok": False,
                "error": detail or f"git exited with code {proc.returncode}"}
    return {"ok": True, "stdout": stdout.decode(errors="replace")}


async def _git_status(path: str) -> dict:
    """Working-tree status. Path is ASSUMED vetted by the caller —
    this function only executes."""
    result = await _git_run(path, ["status", "--short", "--branch"])
    if not result["ok"]:
        return {"status": "error", "error": result["error"]}
    lines = [line for line in result["stdout"].splitlines() if line]
    branch = ""
    changes = []
    for line in lines:
        if line.startswith("##"):
            branch = line[2:].strip()
        else:
            changes.append(line)
    return {"status": "git_status", "path": path, "branch": branch,
            "clean": not changes, "changes": changes}


async def git_status(path: str) -> dict:
    """Server-side entry: read-denylist gate, then shared status."""
    check_read_denylist(path or ".")  # raises ToolBlocked
    return await _git_status(path or ".")


async def _git_diff(path: str) -> dict:
    """Unstaged diff + stat, truncated at GIT_DIFF_BYTES_CAP. Path is
    ASSUMED vetted by the caller — this function only executes."""
    stat = await _git_run(path, ["diff", "--stat"])
    if not stat["ok"]:
        return {"status": "error", "error": stat["error"]}
    diff = await _git_run(path, ["diff"])
    if not diff["ok"]:
        return {"status": "error", "error": diff["error"]}
    body = diff["stdout"]
    truncated = len(body.encode("utf-8")) > GIT_DIFF_BYTES_CAP
    if truncated:
        encoded = body.encode("utf-8")[:GIT_DIFF_BYTES_CAP]
        body = encoded.decode("utf-8", errors="ignore")
    return {"status": "git_diff", "path": path,
            "stat": stat["stdout"], "diff": body, "truncated": truncated}


async def git_diff(path: str) -> dict:
    """Server-side entry: read-denylist gate, then shared diff."""
    check_read_denylist(path or ".")  # raises ToolBlocked
    return await _git_diff(path or ".")


async def _git_log(path: str, limit: int) -> dict:
    """Recent commits, newest first. Path is ASSUMED vetted by the
    caller — this function only executes."""
    try:
        capped = max(1, min(int(limit or GIT_LOG_DEFAULT_LIMIT),
                            GIT_LOG_LIMIT_CAP))
    except (TypeError, ValueError):
        capped = GIT_LOG_DEFAULT_LIMIT
    result = await _git_run(path, [
        "log", f"-n{capped}", "--decorate",
        "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=short",
    ])
    if not result["ok"]:
        return {"status": "error", "error": result["error"]}
    commits = []
    for line in result["stdout"].splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        commits.append({"hash": parts[0], "author": parts[1],
                        "date": parts[2], "subject": parts[3]})
    return {"status": "git_log", "path": path, "commits": commits}


async def git_log(path: str,
                  limit: int = GIT_LOG_DEFAULT_LIMIT) -> dict:
    """Server-side entry: read-denylist gate, then shared log."""
    check_read_denylist(path or ".")  # raises ToolBlocked
    return await _git_log(path or ".", limit)


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


# --- look_at_screen (Windows UI Automation, Bluey steal pack 2) ---

# Walk budget mirrors Bluey's ControlsReader (0.45s / 5000 nodes);
# UIA COM calls cost more per node here, so fewer nodes, same wall time.
LOOK_NODE_BUDGET = 2000
LOOK_TIME_BUDGET_SECONDS = 0.5
LOOK_MAX_CONTROLS = 140
LOOK_MAX_CHILDREN_PER_NODE = 400
# Child-process wall clock for one screen read (walk budgets itself;
# this is the backstop that also covers interpreter startup).
LOOK_SUBPROCESS_TIMEOUT_SECONDS = 30.0

# UIA control type ids (UIAutomationCore) worth naming; everything else
# is reported as a plain "control".
_LOOK_KINDS = {
    50000: "button", 50031: "button", 50001: "calendar",
    50002: "checkbox", 50003: "combo box", 50030: "document",
    50004: "text field", 50005: "link", 50007: "list item",
    50008: "list", 50009: "menu", 50010: "menu bar",
    50011: "menu item", 50013: "option", 50012: "progress",
    50015: "slider", 50016: "spinner", 50018: "tab",
    50019: "tab item", 50020: "text", 50021: "toolbar",
    50022: "tooltip", 50023: "tree", 50024: "tree item",
    50026: "group", 50028: "grid", 50029: "item",
    50034: "header", 50036: "table", 50006: "image",
}
# Structural containers: walked into, never reported.
_LOOK_SKIP_TYPES = frozenset({50032, 50033, 50037, 50014, 50017})
# Unlabeled controls are noise, except fields (Bluey keeps those too).
_LOOK_FIELD_TYPES = frozenset({50004, 50003, 50030})

_UIA_NAME = 30005
_UIA_RECT = 30001
_UIA_TYPE = 30003
_UIA_OFFSCREEN = 30022


def look_kind_name(type_id: int) -> str:
    """Human kind for a UIA control type id (pure, hermetic)."""
    return _LOOK_KINDS.get(type_id, "control")


def look_grid(rect: tuple, width: float, height: float) -> tuple[int, int]:
    """Midpoint of a pixel rect on the 0-1000 grid (pure, hermetic)."""
    left, top, w, h = rect
    if width <= 0 or height <= 0:
        return (0, 0)
    gx = int((left + w / 2) / width * 1000)
    gy = int((top + h / 2) / height * 1000)
    return (min(max(gx, 0), 1000), min(max(gy, 0), 1000))


def look_grid_virtual(rect: tuple, vrect: tuple) -> tuple[int, int]:
    """Midpoint of a virtual-screen pixel rect on the 0-1000 grid.

    Pure, hermetic, F15: ``vrect`` is ``(origin_x, origin_y, width,
    height)`` — the conceptual inverse of
    ``agent/overlay.py:grid_to_pixel`` (which folds the same vrect
    onto pixels). UIA bounding rects arrive in virtual-screen
    coordinates, so the origin offset must apply here; the legacy
    ``look_grid`` (primary-only, origin 0,0) stays for compat.
    """
    left, top, w, h = rect
    ox, oy, width, height = vrect
    if width <= 0 or height <= 0:
        return (0, 0)
    gx = int((left + w / 2 - ox) / width * 1000)
    gy = int((top + h / 2 - oy) / height * 1000)
    return (min(max(gx, 0), 1000), min(max(gy, 0), 1000))


def _look_uia_interfaces() -> dict:
    """Load UIAutomationCore entry points (Windows only, lazy)."""
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.windll.ole32
    oleaut32 = ctypes.windll.oleaut32
    user32 = ctypes.windll.user32

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    def guid(text: str) -> GUID:
        parts = text.split("-")
        tail = bytes.fromhex(parts[3] + parts[4])
        return GUID(int(parts[0], 16), int(parts[1], 16),
                    int(parts[2], 16), (ctypes.c_ubyte * 8)(*tail))

    class Variant(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("llVal", ctypes.c_longlong),
                        ("lVal", ctypes.c_long),
                        ("boolVal", ctypes.c_short),
                        ("bstrVal", ctypes.c_void_p),
                        ("parray", ctypes.c_void_p)]

        _fields_ = [("vt", ctypes.c_ushort),
                    ("wReserved1", ctypes.c_ushort),
                    ("wReserved2", ctypes.c_ushort),
                    ("wReserved3", ctypes.c_ushort),
                    ("u", _U)]
        _anonymous_ = ("u",)

    def method(index: int, restype, *argtypes):
        proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)

        def call(iface, *args):
            table = ctypes.cast(iface, ctypes.POINTER(
                ctypes.POINTER(ctypes.c_void_p))).contents
            return ctypes.cast(table[index], proto)(iface, *args)

        return call

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_ulong, ctypes.c_void_p,
                                       ctypes.c_void_p]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    oleaut32.VariantClear.argtypes = [ctypes.c_void_p]
    oleaut32.VariantClear.restype = ctypes.c_long
    oleaut32.SafeArrayGetLBound.argtypes = [ctypes.c_void_p,
                                            ctypes.c_uint,
                                            ctypes.c_void_p]
    oleaut32.SafeArrayGetLBound.restype = ctypes.c_long
    oleaut32.SafeArrayGetUBound.argtypes = [ctypes.c_void_p,
                                            ctypes.c_uint,
                                            ctypes.c_void_p]
    oleaut32.SafeArrayGetUBound.restype = ctypes.c_long
    oleaut32.SafeArrayAccessData.argtypes = [ctypes.c_void_p,
                                             ctypes.c_void_p]
    oleaut32.SafeArrayAccessData.restype = ctypes.c_long
    oleaut32.SafeArrayUnaccessData.argtypes = [ctypes.c_void_p]
    oleaut32.SafeArrayUnaccessData.restype = ctypes.c_long
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetCursorPos.argtypes = [ctypes.c_void_p]
    user32.GetCursorPos.restype = ctypes.c_int

    return {
        "ctypes": ctypes, "ole32": ole32, "oleaut32": oleaut32,
        "user32": user32, "Variant": Variant, "method": method,
        "CLSID_UIA": guid("FF48DBA4-60EF-4201-AA87-54103EEF594E"),
        "IID_UIA": guid("30CBE57D-D9D0-452A-AB13-7AC5AC4825EE"),
    }


def _look_walk(api: dict) -> dict:
    """Foreground-window control walk. Raises on COM failure."""
    ctypes = api["ctypes"]
    ole32 = api["ole32"]
    oleaut32 = api["oleaut32"]
    user32 = api["user32"]
    method = api["method"]
    Variant = api["Variant"]

    release = method(2, ctypes.c_long)
    held: list = []

    def keep(iface):
        held.append(iface)
        return iface

    VT_BSTR, VT_I4, VT_BOOL, VT_R8, VT_ARRAY = 8, 3, 11, 5, 0x2000
    hr = ole32.CoInitializeEx(None, 2)
    if hr < 0:
        raise OSError(f"CoInitializeEx failed: {hr:#x}")
    com_init = hr == 0
    try:
        auto = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(api["CLSID_UIA"]), None, 1,
            ctypes.byref(api["IID_UIA"]), ctypes.byref(auto))
        if hr < 0 or not auto.value:
            raise OSError(f"UI Automation unavailable: {hr:#x}")
        keep(auto.value)
        from_handle = method(6, ctypes.c_long,
                             ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_void_p))
        get_walker = method(14, ctypes.c_long,
                            ctypes.POINTER(ctypes.c_void_p))
        # IUIAutomationTreeWalker vtable (past IUnknown 0-2), verified
        # live: 5 GetFirstChildElement, 6 GetLastChildElement,
        # 7 GetNextSiblingElement. (6 is NOT NextSibling.)
        first_child = method(5, ctypes.c_long,
                             ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_void_p))
        next_sibling = method(7, ctypes.c_long,
                              ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_void_p))
        get_prop = method(10, ctypes.c_long, ctypes.c_int,
                          ctypes.POINTER(Variant))

        walker = ctypes.c_void_p()
        if get_walker(auto.value, ctypes.byref(walker)) < 0 \
                or not walker.value:
            raise OSError("ControlViewWalker unavailable")
        keep(walker.value)

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise OSError("no foreground window")
        root = ctypes.c_void_p()
        if from_handle(auto.value, hwnd, ctypes.byref(root)) < 0 \
                or not root.value:
            raise OSError("foreground window not automatable")
        keep(root.value)

        def prop_text(element, prop) -> str | None:
            var = Variant()
            if get_prop(element, prop, ctypes.byref(var)) < 0:
                return None
            try:
                if var.vt != VT_BSTR or not var.bstrVal:
                    return None
                return ctypes.wstring_at(var.bstrVal)
            finally:
                oleaut32.VariantClear(ctypes.byref(var))

        def prop_int(element, prop) -> int | None:
            var = Variant()
            if get_prop(element, prop, ctypes.byref(var)) < 0:
                return None
            try:
                if var.vt == VT_I4:
                    return int(var.lVal)
                if var.vt == VT_BOOL:
                    return 1 if var.boolVal else 0
                return None
            finally:
                oleaut32.VariantClear(ctypes.byref(var))

        def prop_rect(element) -> tuple | None:
            var = Variant()
            if get_prop(element, _UIA_RECT, ctypes.byref(var)) < 0:
                return None
            try:
                if var.vt != (VT_ARRAY | VT_R8) or not var.parray:
                    return None
                low = ctypes.c_long()
                high = ctypes.c_long()
                oleaut32.SafeArrayGetLBound(var.parray, 1,
                                            ctypes.byref(low))
                oleaut32.SafeArrayGetUBound(var.parray, 1,
                                            ctypes.byref(high))
                if high.value - low.value + 1 != 4:
                    return None
                data = ctypes.c_void_p()
                if oleaut32.SafeArrayAccessData(var.parray,
                                                ctypes.byref(data)) < 0:
                    return None
                try:
                    vals = ctypes.cast(
                        data, ctypes.POINTER(ctypes.c_double * 4)
                    ).contents
                    return (vals[0], vals[1], vals[2], vals[3])
                finally:
                    oleaut32.SafeArrayUnaccessData(var.parray)
            finally:
                oleaut32.VariantClear(ctypes.byref(var))

        ox = user32.GetSystemMetrics(76)
        oy = user32.GetSystemMetrics(77)
        vw = user32.GetSystemMetrics(78) or 1
        vh = user32.GetSystemMetrics(79) or 1
        if vw <= 0 or vh <= 0:
            # Same fallback as agent/overlay.py:virtual_screen —
            # primary-only when the virtual metrics read empty.
            ox, oy = 0, 0
            vw = user32.GetSystemMetrics(0) or 1
            vh = user32.GetSystemMetrics(1) or 1
        vrect = (ox, oy, vw, vh)
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        app = buf.value.strip() or None

        found: list = []
        deadline = time.monotonic() + LOOK_TIME_BUDGET_SECONDS
        visited = 0
        stack = [root.value]
        while stack and visited < LOOK_NODE_BUDGET:
            if time.monotonic() > deadline:
                break
            element = stack.pop()
            visited += 1
            type_id = prop_int(element, _UIA_TYPE)
            if type_id is None:
                type_id = -1
            rect = prop_rect(element)
            if (type_id not in _LOOK_SKIP_TYPES and rect is not None
                    and rect[2] > 3 and rect[3] > 3
                    and rect[0] < ox + vw and rect[1] < oy + vh
                    and rect[0] + rect[2] > ox
                    and rect[1] + rect[3] > oy
                    and prop_int(element, _UIA_OFFSCREEN) != 1):
                label = (prop_text(element, _UIA_NAME) or "").strip()
                label = " ".join(label.split())
                if label or type_id in _LOOK_FIELD_TYPES:
                    found.append({
                        "kind": look_kind_name(type_id),
                        "label": label,
                        "rect": rect,
                    })
            child = ctypes.c_void_p()
            if first_child(walker.value, element,
                           ctypes.byref(child)) == 0 and child.value:
                keep(child.value)
                siblings = [child.value]
                while len(siblings) < LOOK_MAX_CHILDREN_PER_NODE:
                    nxt = ctypes.c_void_p()
                    if next_sibling(walker.value, siblings[-1],
                                    ctypes.byref(nxt)) != 0 \
                            or not nxt.value:
                        break
                    keep(nxt.value)
                    siblings.append(nxt.value)
                stack.extend(reversed(siblings))

        found.sort(key=lambda c: (
            round(c["rect"][1] / 6), round(c["rect"][0] / 6)))
        controls = []
        for i, item in enumerate(found[:LOOK_MAX_CONTROLS]):
            gx, gy = look_grid_virtual(item["rect"], vrect)
            controls.append({
                "id": f"C{i + 1}", "kind": item["kind"],
                "label": item["label"], "x": gx, "y": gy,
            })

        point = ctypes.create_string_buffer(8)
        if user32.GetCursorPos(point):
            mx, my = ctypes.cast(
                point, ctypes.POINTER(ctypes.c_long * 2)).contents
            mx, my = int(mx), int(my)
        else:
            mx, my = 0, 0
        return {"app": app, "controls": controls,
                "mouse": {"x": min(max(int((mx - ox) / vw * 1000), 0), 1000),
                          "y": min(max(int((my - oy) / vh * 1000), 0), 1000)},
                "size": {"w": vw, "h": vh}}
    finally:
        for iface in held:
            with contextlib.suppress(Exception):
                release(iface)
        if com_init:
            with contextlib.suppress(Exception):
                ole32.CoUninitialize()


async def _look_at_screen() -> dict:
    """Frontmost-window controls via Windows UI Automation.

    Agent-only shape (no paths, no URLs, no sandbox surface): the
    runner calls this on the user's own machine. The walk itself runs
    in a child process - raw UIA COM calls can fail fatal
    (RPC_E_DISCONNECTED during teardown takes down the whole
    interpreter, uncatchable), so a crash surfaces as a non-zero exit
    mapped to an error result instead of killing the agent loop.
    Anywhere non-Windows answers ``unavailable`` - never raises.
    """
    if os.name != "nt":
        return {"status": "unavailable",
                "reason": "look_at_screen runs on a paired Windows "
                          "machine: set INVINCIBLE_AGENT_ROUTING=1 and "
                          "start one with: invincible connect"}
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "from invincible.core.tool_executor import _look_main; "
            "_look_main()",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(),
                timeout=LOOK_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"status": "error",
                    "error": "screen read timed out"}
        if proc.returncode != 0:
            detail = err.decode(errors="replace").strip().splitlines()
            tail = detail[-1] if detail else f"exit {proc.returncode}"
            logger.warning("look_at_screen reader failed: %s", tail[-200:])
            return {"status": "error",
                    "error": "Could not read the screen (reader crashed)"}
        try:
            result = json.loads(out.decode())
        except ValueError:
            return {"status": "error",
                    "error": "screen reader returned unreadable output"}
        if not isinstance(result, dict) or "status" not in result:
            return {"status": "error",
                    "error": "screen reader returned unreadable output"}
        return result
    except Exception as e:
        logger.warning("look_at_screen failed: %s", e)
        return {"status": "error",
                "error": f"Could not read the screen: {e}"}


def _look_main() -> None:
    """Subprocess entry for :func:`_look_at_screen` (stdout JSON).

    Bytes, not print(): the Windows console codec cannot encode the
    private-use icon glyphs control labels carry (e.g. Segoe MDL2).
    """
    try:
        payload = {"status": "look",
                   **_look_walk(_look_uia_interfaces())}
    except Exception as e:
        payload = {"status": "error",
                   "error": f"Could not read the screen: {e}"}
    sys.stdout.buffer.write(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"))


async def _fetch_url(
    url: str, timeout: float,
    *, transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Fetch a URL's body as text. Runs ONLY on the user's own machine
    (agent-routed): the server never fetches caller-supplied URLs, so
    this path cannot become an SSRF primitive. Non-http(s) URLs are
    refused outright; responses over WEB_FETCH_MAX_BYTES are refused
    rather than truncated into model context.

    ``transport`` is test-only (httpx.MockTransport keeps unit tests
    hermetic); production callers omit it.
    """
    if not re.match(r"^https?://", url.strip(), re.I):
        return {"status": "error",
                "error": "web_fetch URL must start with http:// or https://"}
    try:
        async with httpx.AsyncClient(
            timeout=timeout, transport=transport,
            follow_redirects=True,
        ) as client, client.stream("GET", url) as response:
            if response.status_code < 200 or response.status_code >= 300:
                return {"status": "error",
                        "error": f"web_fetch failed with status "
                                 f"{response.status_code} for {url}"}
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > WEB_FETCH_MAX_BYTES:
                    return {"status": "error",
                            "error": f"web_fetch response too large "
                                     f"(over {WEB_FETCH_MAX_BYTES} "
                                     f"bytes); narrow the URL"}
                chunks.append(chunk)
            data = b"".join(chunks)
            content_type = response.headers.get("content-type")
        return {"status": "web_fetch",
                "content_text": data.decode("utf-8", errors="replace"),
                "content_type": content_type,
                "bytes": len(data), "url": url}
    except httpx.TimeoutException:
        return {"status": "error",
                "error": f"web_fetch timed out after {timeout}s"}
    except httpx.HTTPError as e:
        logger.error(f"web_fetch failed: {e}")
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.error(f"web_fetch failed: {e}")
        return {"status": "error", "error": str(e)}


# --- Pack 3 computer-use (F10): paired-PC input + launch ---
#
# Seven agent-side tools (click, type_text, press_keys, scroll, drag,
# open_app, open_url). Agent-only like screenshot: the server never moves
# the caller's mouse, so every executor below answers ``unavailable``
# off-Windows and the real input runs on the paired PC via the agent
# runner. All seven stage for approval (needs_approval=True): on /mcp
# through PendingActionStore + confirm_action (which forwards to the
# agent executor — there is deliberately NO local execution path), on
# webchat through the staged mutating path (manual approval, auto
# immediate). Refusals:
# - press_keys chords that lock/close/kill the session (win+l, alt+f4,
#   ctrl+alt+del, ctrl+shift+esc) raise ToolBlocked — checked both
#   server-side (policy Wall 1, pre-stage) and agent-side (runner
#   Wall 2, pre-execute).
# - type_text into a focused password field raises ToolBlocked —
#   agent-side only (the server has no desktop to probe); the approval
#   gate is the server-side backstop.
# This section is pure unless marked: normalize_computer_args validates
# and normalizes, raising ValueError on shape errors (mapped to error
# results, no token issued) and ToolBlocked on denylist hits (no token).
# The ctypes input itself (_computer_send_*) runs in-process on the
# agent machine — SendInput/ShellExecute use no COM, so unlike the UIA
# walk they need no child-process isolation. Only the two UIA reads
# (control resolution reuses _look_at_screen; the password probe is
# _focus_password_main below) cross into a child process.

COMPUTER_GRID_MAX = 1000
COMPUTER_CLICK_BUTTONS = ("left", "right", "middle")
COMPUTER_SCROLL_DIRECTIONS = ("up", "down", "left", "right")
COMPUTER_SCROLL_DEFAULT_AMOUNT = 3
COMPUTER_SCROLL_MAX_AMOUNT = 10
COMPUTER_TYPE_MAX_CHARS = 2000
COMPUTER_APP_NAME_MAX_CHARS = 500
COMPUTER_TOOLS = ("click", "type_text", "press_keys", "scroll", "drag",
                  "open_app", "open_url")

_KEY_ALIASES = {
    "windows": "win", "super": "win", "meta": "win", "cmd": "win",
    "control": "ctrl", "delete": "del", "escape": "esc",
    "return": "enter",
}
_KEY_MODIFIERS = frozenset({"ctrl", "alt", "shift", "win"})
# Refused outright: session lock, window close, secure-attention, and
# task-manager chords. Single keys ("l", "f4") never match — every entry
# needs its modifiers, so plain typing and function keys stay usable.
_BLOCKED_CHORDS = frozenset({
    frozenset({"win", "l"}),
    frozenset({"alt", "f4"}),
    frozenset({"ctrl", "alt", "del"}),
    frozenset({"ctrl", "shift", "esc"}),
})


def parse_keys_chord(text: str) -> frozenset:
    """Normalize a '+'-joined chord to canonical key names."""
    parts = set()
    for raw in str(text or "").split("+"):
        name = raw.strip().lower()
        if name:
            parts.add(_KEY_ALIASES.get(name, name))
    return frozenset(parts)


def check_keys_denylist(keys: str) -> None:
    """Refuse lock/close-session chords outright (raises ToolBlocked)."""
    if parse_keys_chord(keys) in _BLOCKED_CHORDS:
        raise ToolBlocked(
            "session lock/close chord (win+l, alt+f4, ctrl+alt+del, "
            "ctrl+shift+esc never run — ask the user to press it)")


def _computer_grid_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{field} must be a number 0-{COMPUTER_GRID_MAX}")
    ivalue = int(value)
    if not 0 <= ivalue <= COMPUTER_GRID_MAX:
        raise ValueError(
            f"{field} must be 0-{COMPUTER_GRID_MAX}, got {value!r}")
    return ivalue


def _computer_target(args: dict, *, allow_bare: bool, what: str = "target",
                     xkey: str = "x", ykey: str = "y",
                     ckey: str = "control") -> dict:
    """Resolve one pointing target: a C id wins when present, else x+y.

    ``allow_bare`` permits no target at all (scroll at the cursor,
    type into the focused field). Raises ValueError on shape errors.
    """
    if ckey in args and args[ckey] is not None:
        control = args[ckey]
        if not isinstance(control, str) or not control.strip():
            raise ValueError(
                f"{what} control must be a C id string like 'C3'")
        return {"control": control.strip()}
    x = args.get(xkey)
    y = args.get(ykey)
    if x is None and y is None:
        if allow_bare:
            return {}
        raise ValueError(
            f"{what}: give a control (C id) or {xkey} and {ykey} "
            f"(0-{COMPUTER_GRID_MAX} grid)")
    if x is None or y is None:
        raise ValueError(
            f"{what}: {xkey} and {ykey} go together "
            f"(0-{COMPUTER_GRID_MAX} grid)")
    return {xkey: _computer_grid_int(x, xkey),
            ykey: _computer_grid_int(y, ykey)}


def normalize_computer_args(name: str, args: dict) -> dict:
    """Validate + normalize Pack 3 args. Pure (no OS calls).

    Raises ValueError on shape errors and ToolBlocked on denylist hits;
    both surfaces map those to results with no token issued. The runner
    re-runs this at execution (Wall 2) before touching input.
    """
    argv = args or {}
    if name == "click":
        target = _computer_target(argv, allow_bare=False)
        button = str(argv.get("button", "left")).strip().lower()
        if button not in COMPUTER_CLICK_BUTTONS:
            raise ValueError(
                f"button must be one of "
                f"{', '.join(COMPUTER_CLICK_BUTTONS)}")
        return {**target, "button": button,
                "double": argv.get("double") is True}
    if name == "type_text":
        text = argv.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("type_text needs non-empty 'text'")
        if len(text) > COMPUTER_TYPE_MAX_CHARS:
            raise ValueError(
                f"type_text holds at most {COMPUTER_TYPE_MAX_CHARS} "
                "characters per call")
        target = _computer_target(argv, allow_bare=True)
        return {**target, "text": text,
                "submit": argv.get("submit") is True}
    if name == "press_keys":
        keys = argv.get("keys")
        if not isinstance(keys, str) or not keys.strip():
            raise ValueError(
                "press_keys needs 'keys' like 'ctrl+s'")
        check_keys_denylist(keys)  # raises ToolBlocked; no token
        mains = parse_keys_chord(keys) - _KEY_MODIFIERS
        if len(mains) != 1:
            raise ValueError(
                "press_keys takes one non-modifier key plus modifiers "
                "(e.g. 'ctrl+s', 'alt+tab', 'f5')")
        target = _computer_target(argv, allow_bare=True)
        return {**target, "keys": keys.strip()}
    if name == "scroll":
        direction = str(argv.get("direction", "")).strip().lower()
        if direction not in COMPUTER_SCROLL_DIRECTIONS:
            raise ValueError(
                f"direction must be one of "
                f"{', '.join(COMPUTER_SCROLL_DIRECTIONS)}")
        try:
            amount = int(argv.get("amount",
                                  COMPUTER_SCROLL_DEFAULT_AMOUNT))
        except (TypeError, ValueError):
            raise ValueError(
                f"amount must be 1-{COMPUTER_SCROLL_MAX_AMOUNT}") from None
        if not 1 <= amount <= COMPUTER_SCROLL_MAX_AMOUNT:
            raise ValueError(
                f"amount must be 1-{COMPUTER_SCROLL_MAX_AMOUNT}")
        target = _computer_target(argv, allow_bare=True)
        return {**target, "direction": direction, "amount": amount}
    if name == "drag":
        src = _computer_target(argv, allow_bare=False, what="drag start",
                               xkey="x1", ykey="y1",
                               ckey="control_from")
        dst = _computer_target(argv, allow_bare=False, what="drag end",
                               xkey="x2", ykey="y2", ckey="control_to")
        return {"from": src, "to": dst}
    if name == "open_app":
        app = argv.get("name")
        if not isinstance(app, str) or not app.strip():
            raise ValueError("open_app needs non-empty 'name'")
        app = app.strip()
        if len(app) > COMPUTER_APP_NAME_MAX_CHARS:
            raise ValueError("open_app name is too long")
        extra = argv.get("args", "")
        if extra is not None and not isinstance(extra, str):
            raise ValueError("open_app 'args' must be a string")
        return {"name": app, "args": (extra or "").strip()}
    if name == "open_url":
        url = argv.get("url")
        if not isinstance(url, str) or not re.match(
                r"^https?://", url.strip(), re.I):
            raise ValueError(
                "open_url URL must start with http:// or https://")
        return {"url": url.strip()}
    raise ValueError(f"Unknown computer tool: {name}")


def stage_computer_action(
    name: str,
    args: dict,
    store: PendingActionStore,
    owner_subject: int | None = None,
) -> dict:
    """Validate (ValueError/ToolBlocked — no token either way), then
    stage a Pack 3 action for approval. Agent-only: confirm forwards to
    the agent executor; there is deliberately no local execution path
    (see confirm_action's Pack 3 arm).
    """
    normalized = normalize_computer_args(name, args or {})
    token = store.put(name, normalized, owner_subject=owner_subject)
    if name == "type_text":
        summary = f"type_text ({len(normalized['text'])} chars)"
    else:
        summary = f"{name} {json.dumps(normalized)[:160]}"
    print(f"[MCP] Pending {token}: {summary}")
    return {
        "status": "pending_confirmation",
        "token": token,
        "action": name,
        "args": normalized,
        "message": (
            "Call confirm_action with this token "
            "(approve=true/false) to proceed."
        ),
    }


_COMPUTER_UNAVAILABLE_REASON = (
    "computer-use runs on a paired Windows machine: "
    "set INVINCIBLE_AGENT_ROUTING=1 and start one with: "
    "invincible connect")

# Virtual-key codes for press_keys main keys (modifiers are separate).
_COMPUTER_VK_MODIFIERS = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12,
                          "win": 0x5B}
_COMPUTER_VK_NAMED = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "esc": 0x1B,
    "space": 0x20, "pgup": 0x21, "pgdn": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "insert": 0x2D, "del": 0x2E,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
# Single characters with 1:1 virtual-key codes (chords are already
# lowercased by parse_keys_chord, so lowercase keys suffice).
_COMPUTER_VK_ALNUM = {
    **{c: 0x41 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")},
    **{c: 0x30 + i for i, c in enumerate("0123456789")},
}
_UIA_IS_PASSWORD = 30019
# IUIAutomation vtable index of GetFocusedElement (past IUnknown 0-2;
# verified live on the dev box like the TreeWalker indices — do not
# renumber from memory).
_UIA_GET_FOCUSED = 8


def _computer_input_api() -> dict:
    """Lazy user32 SendInput entry points (Windows only, no COM)."""
    import ctypes

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.c_void_p)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.c_void_p)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("u", _U)]

    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p,
                                 ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    shell32.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                      ctypes.c_wchar_p, ctypes.c_wchar_p,
                                      ctypes.c_void_p, ctypes.c_int]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    return {"ctypes": ctypes, "user32": user32, "shell32": shell32,
            "INPUT": INPUT, "MOUSEINPUT": MOUSEINPUT,
            "KEYBDINPUT": KEYBDINPUT}


def _computer_send(api: dict, inputs: list) -> None:
    """One SendInput batch; raises OSError unless all events land."""
    ctypes = api["ctypes"]
    arr = (api["INPUT"] * len(inputs))(*inputs)
    sent = api["user32"].SendInput(len(inputs), arr, ctypes.sizeof(
        api["INPUT"]))
    if sent != len(inputs):
        raise OSError(f"SendInput took {sent}/{len(inputs)} events")


def _computer_mouse_input(api: dict, *, dx: int = 0, dy: int = 0,
                          data: int = 0, flags: int = 0):
    entry = api["INPUT"]()
    entry.type = 0  # INPUT_MOUSE
    entry.u.mi.dx = dx
    entry.u.mi.dy = dy
    entry.u.mi.mouseData = data
    entry.u.mi.dwFlags = flags
    return entry


def _computer_key_input(api: dict, *, vk: int = 0, scan: int = 0,
                        flags: int = 0):
    entry = api["INPUT"]()
    entry.type = 1  # INPUT_KEYBOARD
    entry.u.ki.wVk = vk
    entry.u.ki.wScan = scan
    entry.u.ki.dwFlags = flags
    return entry


def _computer_abs(value: int) -> int:
    """0-1000 grid to SendInput absolute units (0-65535, virtual screen
    — the same virtual grid look_at_screen reports and the overlay
    glides on; F15)."""
    return value * 65535 // COMPUTER_GRID_MAX


# SendInput mouse flags (named so the VIRTUALDESK bit is pinned, F15).
_COMPUTER_MOUSE_MOVE = 0x0001
_COMPUTER_MOUSE_ABSOLUTE = 0x8000
_COMPUTER_MOUSE_VIRTUALDESK = 0x4000


def _computer_move_to(api: dict, x: int, y: int) -> None:
    _computer_send(api, [_computer_mouse_input(
        api, dx=_computer_abs(x), dy=_computer_abs(y),
        flags=(_COMPUTER_MOUSE_MOVE | _COMPUTER_MOUSE_ABSOLUTE
               | _COMPUTER_MOUSE_VIRTUALDESK))])  # MOVE|ABSOLUTE|VIRTUALDESK


def _computer_click_at(api: dict, x: int, y: int, button: str,
                       double: bool) -> None:
    _computer_move_to(api, x, y)
    down, up = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010),
                "middle": (0x0020, 0x0040)}[button]
    _computer_send(api, [_computer_mouse_input(api, flags=down),
                         _computer_mouse_input(api, flags=up)])
    if double:
        time.sleep(0.05)
        _computer_send(api, [_computer_mouse_input(api, flags=down),
                             _computer_mouse_input(api, flags=up)])


def _computer_scroll_at(api: dict, x: int | None, y: int | None,
                        direction: str, amount: int) -> None:
    if x is not None and y is not None:
        _computer_move_to(api, x, y)  # hover; no click — scroll needs none
    delta = 120 * amount
    if direction == "up":
        flags, data = 0x0800, delta  # WHEEL
    elif direction == "down":
        flags, data = 0x0800, -delta & 0xFFFFFFFF
    elif direction == "right":
        flags, data = 0x1000, delta  # HWHEEL
    else:
        flags, data = 0x1000, -delta & 0xFFFFFFFF
    _computer_send(api, [_computer_mouse_input(api, data=data,
                                               flags=flags)])


def _computer_drag(api: dict, x1: int, y1: int, x2: int, y2: int) -> None:
    _computer_move_to(api, x1, y1)
    _computer_send(api, [_computer_mouse_input(api, flags=0x0002)])
    time.sleep(0.05)
    _computer_move_to(api, x2, y2)
    _computer_send(api, [_computer_mouse_input(api, flags=0x0004)])


def _computer_type(api: dict, text: str) -> None:
    """Layout-independent typing via KEYEVENTF_UNICODE (wScan carries
    the UTF-16 code unit, so no keyboard layout is involved)."""
    batch = []
    encoded = text.encode("utf-16-le")
    for i in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[i:i + 2], "little")
        batch.append(_computer_key_input(api, scan=unit, flags=0x0004))
        batch.append(_computer_key_input(api, scan=unit,
                                         flags=0x0004 | 0x0002))
    if batch:
        _computer_send(api, batch)


def _computer_press(api: dict, chord: frozenset) -> None:
    """One main key plus held modifiers (denylist checked upstream).

    Chord mains ride virtual-key codes: KEYEVENTF_UNICODE strokes
    ignore held modifiers, so a chord whose main key rides wScan
    arrives with the modifier dropped (observed live: ctrl+z typed a
    bare ``z``, ctrl+t never opened a tab). Modifier-free presses
    keep the Unicode path (layout-independent typing).
    """
    mains = sorted(chord - _KEY_MODIFIERS)
    main = mains[0]
    batch = []
    for mod in sorted(chord & _KEY_MODIFIERS):
        batch.append(_computer_key_input(
            api, vk=_COMPUTER_VK_MODIFIERS[mod]))
    if len(main) == 1 and (chord & _KEY_MODIFIERS) \
            and main in _COMPUTER_VK_ALNUM:
        vk = _COMPUTER_VK_ALNUM[main]
        batch.append(_computer_key_input(api, vk=vk))
        batch.append(_computer_key_input(api, vk=vk, flags=0x0002))
    elif len(main) == 1:
        for unit in _computer_utf16_units(main):
            batch.append(_computer_key_input(api, scan=unit,
                                             flags=0x0004))
            batch.append(_computer_key_input(api, scan=unit,
                                             flags=0x0004 | 0x0002))
    elif main in _COMPUTER_VK_NAMED:
        vk = _COMPUTER_VK_NAMED[main]
        batch.append(_computer_key_input(api, vk=vk))
        batch.append(_computer_key_input(api, vk=vk, flags=0x0002))
    else:
        raise ValueError(f"unknown key {main!r} in press_keys chord")
    for mod in sorted(chord & _KEY_MODIFIERS, reverse=True):
        batch.append(_computer_key_input(
            api, vk=_COMPUTER_VK_MODIFIERS[mod], flags=0x0002))
    _computer_send(api, batch)


def _computer_utf16_units(text: str) -> list[int]:
    encoded = text.encode("utf-16-le")
    return [int.from_bytes(encoded[i:i + 2], "little")
            for i in range(0, len(encoded), 2)]


def _computer_open(api: dict, name: str, args: str) -> dict:
    rc = api["shell32"].ShellExecuteW(
        None, "open", name, args or None, None, 1)  # SW_SHOWNORMAL
    try:
        code = int(rc or 0)
    except (TypeError, ValueError):
        code = 0
    if code <= 32:
        return {"status": "error",
                "error": f"could not open {name!r} "
                         f"(ShellExecute code {code})"}
    return {"status": "ok", "action": "open_app", "name": name,
            "verified": False}


async def _focus_is_password() -> bool | None:
    """Focused-element IsPassword probe via a child process.

    True/False from the probe; None when the probe itself failed — the
    caller fails closed (refuses the keystrokes). Same isolation as
    _look_at_screen: raw UIA COM teardown can fail fatal.
    """
    if os.name != "nt":
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "from invincible.core.tool_executor import "
            "_focus_password_main; _focus_password_main()",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(),
                timeout=LOOK_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        if proc.returncode != 0:
            detail = err.decode(errors="replace").strip().splitlines()
            tail = detail[-1] if detail else f"exit {proc.returncode}"
            logger.warning("password probe failed: %s", tail[-200:])
            return None
        try:
            payload = json.loads(out.decode())
        except ValueError:
            return None
        password = payload.get("password")
        return password if isinstance(password, bool) else None
    except Exception as e:
        logger.warning("password probe failed: %s", e)
        return None


def _focus_is_password_sync(api: dict) -> bool:
    """Focused element's IsPassword property. Raises on COM failure."""
    ctypes = api["ctypes"]
    ole32 = api["ole32"]
    oleaut32 = api["oleaut32"]
    method = api["method"]
    Variant = api["Variant"]

    release = method(2, ctypes.c_long)
    held: list = []

    def keep(iface):
        held.append(iface)
        return iface

    VT_BOOL = 11
    hr = ole32.CoInitializeEx(None, 2)
    if hr < 0:
        raise OSError(f"CoInitializeEx failed: {hr:#x}")
    com_init = hr == 0
    try:
        auto = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(api["CLSID_UIA"]), None, 1,
            ctypes.byref(api["IID_UIA"]), ctypes.byref(auto))
        if hr < 0 or not auto.value:
            raise OSError(f"UI Automation unavailable: {hr:#x}")
        keep(auto.value)
        get_focused = method(_UIA_GET_FOCUSED, ctypes.c_long,
                             ctypes.POINTER(ctypes.c_void_p))
        get_prop = method(10, ctypes.c_long, ctypes.c_int,
                          ctypes.POINTER(Variant))
        focused = ctypes.c_void_p()
        if get_focused(auto.value, ctypes.byref(focused)) < 0 \
                or not focused.value:
            raise OSError("no focused element")
        keep(focused.value)
        var = Variant()
        if get_prop(focused.value, _UIA_IS_PASSWORD,
                    ctypes.byref(var)) < 0:
            raise OSError("IsPassword unreadable")
        try:
            if var.vt != VT_BOOL:
                raise OSError(
                    f"IsPassword non-bool (vt {var.vt})")
            return bool(var.boolVal)
        finally:
            oleaut32.VariantClear(ctypes.byref(var))
    finally:
        for iface in held:
            with contextlib.suppress(Exception):
                release(iface)
        if com_init:
            with contextlib.suppress(Exception):
                ole32.CoUninitialize()


def _focus_password_main() -> None:
    """Subprocess entry for :func:`_focus_is_password` (stdout JSON)."""
    try:
        payload: dict = {"password": _focus_is_password_sync(
            _look_uia_interfaces())}
    except Exception as e:
        payload = {"error": f"password probe failed: {e}"}
    sys.stdout.buffer.write(json.dumps(payload).encode("utf-8"))


async def _computer_resolve(target: dict, *,
                          on_point=None) -> tuple[dict, dict]:
    """Resolve a normalized target to grid x/y plus a result echo.

    Control ids look up against a FRESH _look_at_screen read (same
    ordering ⇒ same ids); a miss is a stale-id error naming the fix.
    Returns (point, echo); raises ValueError when unresolvable.

    ``on_point`` is the F11 overlay announce hook: every resolved grid
    point is offered to it (drag offers both ends). It must never
    break the job - exceptions are swallowed here.
    """
    if "control" not in target:
        point = {"x": target.get("x"), "y": target.get("y")}
        if point["x"] is None:
            return {}, {}
        _notify_point(on_point, point)
        return point, {"x": point["x"], "y": point["y"]}
    look = await _look_at_screen()
    if not isinstance(look, dict) or look.get("status") != "look":
        raise ValueError(
            f"could not resolve {target['control']}: screen read failed "
            "(is the paired agent on a Windows desktop?)")
    for control in look.get("controls") or []:
        if isinstance(control, dict) and control.get("id") == target[
                "control"]:
            resolved = {"x": control["x"], "y": control["y"]}
            _notify_point(on_point, resolved)
            return (resolved,
                    {"x": control["x"], "y": control["y"],
                     "label": control.get("label") or "",
                     "kind": control.get("kind") or "control",
                     "control": target["control"]})
    raise ValueError(
        f"unknown control {target['control']!r} — it may be stale; "
        "re-read with look_at_screen and use the new C id")


def _notify_point(on_point, point: dict) -> None:
    """Offer one resolved grid point to the overlay hook (never raises)."""
    if on_point is None:
        return
    with contextlib.suppress(Exception):
        on_point(point)


async def _computer_dwell(seconds: float) -> None:
    """Pre-action pause so the overlay glide lands first (0 skips it).

    The runner only passes a nonzero delay when an overlay is alive,
    so headless jobs pay zero added latency.
    """
    try:
        delay = float(seconds or 0)
    except (TypeError, ValueError):
        return
    if delay > 0:
        await asyncio.sleep(delay)


def _resolve_delay(pre_action_delay, delay_for, point) -> float:
    """Dwell covering the overlay glide (pure, never raises).

    ``pre_action_delay`` is the base float (backward compat — a bad
    value reads as 0). ``delay_for`` is an optional per-point estimator
    ``(point) -> seconds`` (the runner's Bluey-flight estimate); its
    answer wins when larger, and any failure falls back to the base.
    """
    try:
        base = float(pre_action_delay or 0)
    except (TypeError, ValueError):
        base = 0.0
    if not 0 <= base < 3600:
        base = 0.0
    if delay_for is None:
        return base
    try:
        extra = float(delay_for(point))
    except Exception:
        return base
    if not 0 <= extra < 3600:
        return base
    return max(base, extra)


async def _computer_act(name: str, args: dict, *,
                      on_point=None,
                      pre_action_delay: float = 0.0,
                      delay_for=None) -> dict:
    """Execute one Pack 3 action on THIS machine (agent-side only).

    ToolBlocked propagates (the runner maps it to a blocked result);
    ValueError becomes an error result. Only ``unavailable`` answers
    off-Windows — never raises there.

    ``ok`` means dispatched to the OS input queue / launcher, NOT
    pixels verified: callers must confirm with a fresh look_at_screen
    (every ``ok`` result carries ``verified: False`` for that reason).

    ``on_point`` announces each resolved grid point to the F11 overlay
    (never breaks the job); ``pre_action_delay`` pauses once before
    the terminal physical action so the overlay glide lands first.
    ``delay_for`` is an optional ``(point) -> seconds`` estimator
    (runner-supplied Bluey-flight cover); each point-bearing branch
    dwells ``max(base, estimate)`` so settle never aborts a glide
    mid-flight. ``None`` keeps the plain base delay.
    """
    if os.name != "nt":
        return {"status": "unavailable",
                "reason": _COMPUTER_UNAVAILABLE_REASON}
    try:
        norm = normalize_computer_args(name, args or {})
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    api = _computer_input_api()
    try:
        if name == "click":
            point, echo = await _computer_resolve(norm,
                                                  on_point=on_point)
            await _computer_dwell(_resolve_delay(
                pre_action_delay, delay_for, point))
            _computer_click_at(api, point["x"], point["y"],
                               norm["button"], norm["double"])
            return {"status": "ok", "action": "click",
                    "button": norm["button"],
                    "double": norm["double"], "verified": False, **echo}
        if name == "type_text":
            if "control" in norm or "x" in norm:
                # Click-to-focus first, so the probe below inspects the
                # field that actually receives the keystrokes (covers
                # the focus race: probe-then-type would check the
                # wrong element).
                point, echo = await _computer_resolve(norm,
                                                      on_point=on_point)
                await _computer_dwell(_resolve_delay(
                    pre_action_delay, delay_for, point))
                _computer_click_at(api, point["x"], point["y"],
                                   "left", False)
            else:
                echo = {}
                await _computer_dwell(pre_action_delay)
            password = await _focus_is_password()
            if password is True:
                raise ToolBlocked(
                    "the focused field is a password field — type_text "
                    "never types into one; ask the user to enter it")
            if password is None:
                return {"status": "error",
                        "error": "could not verify the focused field is "
                                 "not a password field — refusing"}
            _computer_type(api, norm["text"])
            if norm["submit"]:
                _computer_press(api, frozenset({"enter"}))
            return {"status": "ok", "action": "type_text",
                    "chars": len(norm["text"]),
                    "submit": norm["submit"], "verified": False, **echo}
        if name == "press_keys":
            if "control" in norm or "x" in norm:
                point, echo = await _computer_resolve(norm,
                                                      on_point=on_point)
                await _computer_dwell(_resolve_delay(
                    pre_action_delay, delay_for, point))
                _computer_click_at(api, point["x"], point["y"],
                                   "left", False)
            else:
                echo = {}
                await _computer_dwell(pre_action_delay)
            _computer_press(api, parse_keys_chord(norm["keys"]))
            return {"status": "ok", "action": "press_keys",
                    "keys": norm["keys"], "verified": False, **echo}
        if name == "scroll":
            point, echo = await _computer_resolve(norm,
                                                  on_point=on_point)
            await _computer_dwell(_resolve_delay(
                pre_action_delay, delay_for, point))
            _computer_scroll_at(api, point.get("x"), point.get("y"),
                                norm["direction"], norm["amount"])
            return {"status": "ok", "action": "scroll",
                    "direction": norm["direction"],
                    "amount": norm["amount"], "verified": False, **echo}
        if name == "drag":
            start, echo_from = await _computer_resolve(norm["from"],
                                                       on_point=on_point)
            end, echo_to = await _computer_resolve(norm["to"],
                                                   on_point=on_point)
            await _computer_dwell(_resolve_delay(
                pre_action_delay, delay_for, end))
            _computer_drag(api, start["x"], start["y"],
                           end["x"], end["y"])
            return {"status": "ok", "action": "drag",
                    "from": echo_from, "to": echo_to, "verified": False}
        if name == "open_app":
            return _computer_open(api, norm["name"], norm["args"])
        if name == "open_url":
            opened = _computer_open(api, norm["url"], "")
            if opened.get("status") != "ok":
                return opened
            return {"status": "ok", "action": "open_url",
                    "url": norm["url"], "verified": False}
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    except OSError as e:
        logger.warning("computer-use %s failed: %s", name, e)
        return {"status": "error",
                "error": f"could not perform {name}: {e}"}
    return {"status": "error",
            "error": f"Unknown computer tool: {name}"}
