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
     request on ``/mcp``. A live token implies the operator approved the
     client on the /oauth/authorize consent page, and can be revoked with
     ``invincible oauth revoke``. Holding a token is sufficient to approve
     (or deny) pending actions remotely. This is a real security property
     change, not an implementation detail.
  5. Authentication for who can reach this code at all lives one layer up,
     in the MCP endpoint's dependency (OAuth 2.1 + PKCE bearer tokens,
     independent of GATEWAY_API_KEY). This module assumes the caller is
     already authenticated - it only decides whether a specific action is
     safe and approved, not who's allowed to ask.
  6. read_file has no approval step, so it is sandboxed instead: reads are
     only allowed under the repo root, the server's working directory, and
     any directories listed in INVINCIBLE_READ_ROOTS (os.pathsep-separated).
     Paths outside those roots are blocked outright, and .env / sessions.db
     / .git are blocked by name anywhere inside them.

KNOWN LIMIT: the denylist is a text-pattern match, not a real shell parser.
`powershell -Command "..."`, `cmd /c "..."`, or any other wrapper/encoding
can smuggle an arbitrary command past every pattern below. The denylist
exists to catch the obvious, high-blast-radius cases without a prompt; it
is not the real safety boundary. The approval step is - whoever holds a
valid bearer token decides what runs, and anything staged for approval is
visible in plain sight at the server's own stdout before it is approved.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
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
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

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
                rows = (await conn.execute(
                    pending_actions.select()
                )).all()
                for token, action_type, args, created_at in rows:
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
        sees exactly an unknown-token answer.
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
    abs_path = os.path.abspath(path)
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
        os.path.normcase(os.path.abspath(root)) for root in roots
    ]


def check_read_denylist(path: str) -> None:
    abs_path = os.path.abspath(path)
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
