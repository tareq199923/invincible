# invincible/core/webchat_agent.py
"""Agentic loop behind the dashboard webchat (cookie realm only).

The text-only webchat sends one user turn and streams the reply. This
module adds the three agent modes on top of the SAME pipeline
(``core/chat_service.py`` preparation + the single
``router._iter_attempts`` failover loop - no parallel attempt loop):

- ``plan``   - read-only inspection tools only, offered by construction
  (the model cannot take a mutating action: the tools are absent from
  the request, not merely forbidden by prompt). Ends with a plan.
- ``manual`` - all tools; every ``execute_bash``/``write_file`` pauses
  for the user's explicit browser approval before running. Reads run
  immediately (same posture as ``POST /mcp``).
- ``auto``   - all tools run immediately, no approvals. Denylists still
  enforced (a blocked action never stages, let alone runs).

Execution locality mirrors ``POST /mcp`` exactly: with
``INVINCIBLE_AGENT_ROUTING=1`` confirmed work (and reads) dispatch to
the caller's paired machine via ``AgentRegistry``; otherwise everything
runs on the server host (which on a self-host IS the user's PC). An
offline agent is a plain tool error naming
``invincible harness connect`` - never a silent local fallback.

Layering: pure ``core/`` business logic - no FastAPI, no Router import,
no ``endpoints/`` imports. The router, stores, executor, and approval
waiter are passed in; the endpoint formats the yielded
``(event, data)`` pairs as SSE.
"""
import asyncio
import contextlib
import json
import logging

from invincible.compat.common import upstream_error_detail
from invincible.core import tool_executor
from invincible.core.accounts import AccountError, ProjectService
from invincible.core.chat_service import _persist_new_turns
from invincible.core.continuity import ContinuityConflictError
from invincible.core.harness_policy import before_tool_call
from invincible.core.identity import resolve_project_by_name
from invincible.core.memory import (
    MAX_CONTENT_CHARS,
    MCP_CONFIDENCE,
    MEMORY_KINDS,
)
from invincible.core.principal import Principal
from invincible.core.router import (
    AllProvidersFailedError,
    NoCredentialsConfiguredError,
    UpstreamClientError,
)
from invincible.core.settings import AGENT_JOB_GRACE_SECONDS, settings

logger = logging.getLogger("invincible.webchat.agent")

WEBCHAT_MODES = ("plan", "manual", "auto")
DEFAULT_MODE = "manual"

READ_ONLY_TOOLS = (
    "read_file",
    "list_dir",
    "code_search",
    "git_status",
    "git_diff",
    "git_log",
    "process_list",
)
MUTATING_TOOLS = ("execute_bash", "write_file")
# Data-plane reads (memory/project/continuity) - safe in plan mode.
DATA_READ_TOOLS = (
    "memory_search",
    "memory_list",
    "project_list",
    "task_state_get",
)
# Data-plane writes - manual/auto only, never plan.
DATA_WRITE_TOOLS = (
    "memory_save",
    "project_create",
    "task_state_set",
    "checkpoint_create",
)
# Agent-only read: runs on the paired machine, never on the server host
# (same SSRF posture as POST /mcp screenshot).
AGENT_ONLY_TOOLS = ("screenshot",)
ALL_DATA_TOOLS = DATA_READ_TOOLS + DATA_WRITE_TOOLS

# Hard cap on tool iterations per turn: bounds provider spend and keeps
# a runaway model from looping forever. On exhaustion the loop makes one
# final no-tools call so the turn still ends with an answer.
MAX_TOOL_ITERATIONS = 10
# How long one turn holds its SSE stream open waiting for a browser
# approval (comfortably inside PendingActionStore's 600s TTL; a late
# approval past this lands on the endpoint's expired-token path).
APPROVAL_WAIT_SECONDS = 300.0
# UI truncation bounds (approvals show the user their own data, but the
# model context and event bodies stay bounded regardless).
_PREVIEW_CHARS = 500
# Response caps mirrored from POST /mcp (endpoints/mcp.py): MCP results
# land in model context, so the same token discipline applies here.
_MEMORY_SEARCH_DEFAULT = 5
_MEMORY_SEARCH_MAX = 10
_MEMORY_LIST_DEFAULT = 10
_MEMORY_LIST_MAX = 20
_PROJECT_CAP = 50


def _tool(name: str, description: str, properties: dict,
          required: list) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


WEBCHAT_TOOL_SCHEMAS = [
    _tool("read_file", "Read a file's contents on the machine that "
          "executes tools (your paired PC when an agent is connected, "
          "else the server host). Secret/state files are rejected.",
          {"path": {"type": "string"}}, ["path"]),
    _tool("list_dir", "List a directory's entries (names, kinds, sizes) "
          "on the executing machine. Hidden files skipped unless asked.",
          {"path": {"type": "string"},
           "limit": {"type": "integer"},
           "show_hidden": {"type": "boolean"}}, ["path"]),
    _tool("code_search", "Search files for a text pattern under a "
          "directory (case-insensitive, capped results).",
          {"pattern": {"type": "string"}, "path": {"type": "string"},
           "max_results": {"type": "integer"}}, ["pattern", "path"]),
    _tool("git_status", "Git working-tree status for the repo "
          "containing a path. Read-only; errors outside a repo.",
          {"path": {"type": "string"}}, ["path"]),
    _tool("git_diff", "Unstaged git diff (+stat) for the repo containing "
          "a path. Read-only; large diffs truncated.",
          {"path": {"type": "string"}}, ["path"]),
    _tool("git_log", "Recent commits for the repo containing a path, "
          "newest first. Read-only.",
          {"path": {"type": "string"},
           "limit": {"type": "integer"}}, ["path"]),
    _tool("process_list", "Running processes (pid, name, cpu/mem) on "
          "the executing machine. Read-only.",
          {"limit": {"type": "integer"}}, []),
    _tool("execute_bash", "Run a shell command on the executing "
          "machine. Denylisted commands (destructive fs ops, privilege "
          "escalation, power commands) are rejected outright. Depending "
          "on the chat mode this call either runs immediately or pauses "
          "for the user's approval first.",
          {"command": {"type": "string"}}, ["command"]),
    _tool("write_file", "Write content to a file on the executing "
          "machine. Security/state paths are rejected outright. "
          "Depending on the chat mode this call either runs immediately "
          "or pauses for the user's approval first.",
          {"path": {"type": "string"},
           "content": {"type": "string"}}, ["path", "content"]),
    _tool("screenshot", "Capture a headless-Chrome screenshot (1280x800 "
          "PNG) of an http(s) URL for visual validation. Runs ONLY on "
          "your paired machine - the server never fetches caller URLs.",
          {"url": {"type": "string"}}, ["url"]),
    _tool("memory_save", "Deliberately store a fact about the user or one "
          "of their projects into their memory store - the same store "
          "dashboard and gateway chats read. Use for durable facts worth "
          "recalling later; for task progress use task_state_set instead.",
          {"content": {"type": "string"},
           "kind": {"type": "string",
                    "enum": list(MEMORY_KINDS)},
           "project": {"type": "string"}}, ["content"]),
    _tool("memory_search", "Search the user's memory store with the same "
          "ranking gateway chats use. Returns a small ranked list, never "
          "a dump.",
          {"query": {"type": "string"},
           "project": {"type": "string"},
           "limit": {"type": "integer"}}, ["query"]),
    _tool("memory_list", "Browse the user's most recent memories, newest "
          "first. Optional kind/project filters; capped rows. No delete "
          "over chat: deletion stays dashboard-only.",
          {"limit": {"type": "integer"},
           "kind": {"type": "string",
                    "enum": list(MEMORY_KINDS)},
           "project": {"type": "string"}}, []),
    _tool("project_create", "Create a new project for the user. Projects "
          "scope memories. Names 1-100 chars, unique per user.",
          {"name": {"type": "string"}}, ["name"]),
    _tool("project_list", "List the user's projects (id, name, "
          "is_default). Call before project-scoped memory_save.",
          {"include_archived": {"type": "boolean"}}, []),
    _tool("task_state_set", "Persist canonical task progress into the "
          "shared continuity store for this session. Payload must be a "
          "JSON OBJECT of structured facts to preserve verbatim.",
          {"payload": {"type": "string"},
           "task_key": {"type": "string"},
           "status": {"type": "string",
                      "enum": ["active", "blocked", "done", "cancelled"]},
           "expected_version": {"type": "integer"},
           "session_id": {"type": "string"}}, ["payload"]),
    _tool("task_state_get", "Read the latest trusted task state "
          "previously persisted via task_state_set.",
          {"task_key": {"type": "string"},
           "session_id": {"type": "string"}}, []),
    _tool("checkpoint_create", "Snapshot the current task-state version "
          "as a named checkpoint (e.g. 'completed through 37').",
          {"note": {"type": "string"},
           "task_key": {"type": "string"},
           "session_id": {"type": "string"}}, []),
]

_PLAN_TOOLS = READ_ONLY_TOOLS + AGENT_ONLY_TOOLS + DATA_READ_TOOLS
_TOOLS_BY_MODE = {
    "plan": [s for s in WEBCHAT_TOOL_SCHEMAS
             if s["function"]["name"] in _PLAN_TOOLS],
    "manual": list(WEBCHAT_TOOL_SCHEMAS),
    "auto": list(WEBCHAT_TOOL_SCHEMAS),
}

MODE_SYSTEM_PROMPTS = {
    "plan": (
        "You are helping plan work on the user's own machine. Produce a "
        "concrete step-by-step plan and stop. You have read-only "
        "inspection tools (read files, list directories, search code, "
        "git info, processes, screenshot) plus read-only memory/project/"
        "task lookups (memory_search/list, project_list, task_state_get) "
        "- use them to ground the plan in reality. "
        "You cannot change anything: no mutating tools are available. "
        "Never claim an action was taken; end with the plan."
    ),
    "manual": (
        "You help operate the user's own machine. You have inspection "
        "tools plus execute_bash and write_file, plus memory/project/"
        "continuity tools (memory_save/search/list, project_create/list, "
        "task_state_set/get, checkpoint_create) and screenshot. Reads run "
        "immediately; each execute_bash/write_file call pauses for the "
        "user's explicit approval before running - call the tool, briefly "
        "say what will happen, and wait for the result to come back. If "
        "the user declines (or approval times out), respect it and offer "
        "an alternative. Keep commands least-privilege; never exfiltrate "
        "data off the machine."
    ),
    "auto": (
        "You help operate the user's own machine autonomously. You have "
        "inspection tools plus execute_bash and write_file, plus "
        "memory/project/continuity tools and screenshot, which run "
        "immediately without further confirmation. Act carefully and "
        "least-privilege: inspect before mutating, verify afterwards, "
        "and stop when done. Never exfiltrate data off the machine."
    ),
}


class ApprovalWaiter:
    """Rendezvous between an SSE stream awaiting approval and the
    browser's approve/deny POST.

    Token -> (owner user_id, Future). Single-instance by design (stored
    on ``app.state``, same trade-off as the agent registry): a restart
    orphans in-flight approvals and the stream's timeout path declines
    them. Cross-user resolution returns False (indistinguishable from
    unknown), and every entry is discarded when its stream ends, so a
    late approval can never fire twice.
    """

    def __init__(self):
        self._waiters: dict[str, tuple[int, asyncio.Future]] = {}

    async def wait(self, token: str, user_id: int) -> bool:
        """Block until this user's browser resolves ``token``. True =
        approved; False = denied or timed out."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        future = loop.create_future()
        self._waiters[token] = (user_id, future)
        try:
            return bool(await asyncio.wait_for(
                future, timeout=APPROVAL_WAIT_SECONDS))
        except asyncio.TimeoutError:
            return False
        finally:
            self._waiters.pop(token, None)

    def resolve(self, token: str, user_id: int, approved: bool) -> bool:
        """Resolve a pending approval. False when unknown, foreign, or
        already settled (double-clicks and late approvals included)."""
        entry = self._waiters.get(token)
        if entry is None:
            return False
        owner, future = entry
        if owner != user_id or future.done():
            return False
        future.set_result(bool(approved))
        return True

    def discard(self, token: str) -> None:
        """Cancel and forget a waiter (stream disconnect / shutdown)."""
        entry = self._waiters.pop(token, None)
        if entry is not None and not entry[1].done():
            entry[1].cancel()

    def pending_tokens(self, user_id: int) -> list[str]:
        """Tokens this user currently has a live waiter for (non-consuming
        peek - the waiter still owns them)."""
        return [
            token for token, (owner, future) in self._waiters.items()
            if owner == user_id and not future.done()
        ]


def build_agent_executor(registry, subject: int, routing_on: bool):
    """Executor callback with the exact semantics of the MCP path: None
    when agent routing is off (caller falls back to local execution);
    otherwise dispatches to the subject's paired agent, mapping an
    offline agent to an ``agent_offline`` result (never a silent local
    run)."""
    if not routing_on or registry is None:
        return None

    async def _execute(action_type: str, args: dict) -> dict:
        if subject is None or not registry.online(subject):
            return {
                "status": "agent_offline",
                "message": (
                    "No invincible agent is connected for this account. "
                    "Start one on your machine with: "
                    "invincible harness connect"
                ),
            }
        timeout = float(args.get("timeout", 30.0)) + AGENT_JOB_GRACE_SECONDS
        return await registry.dispatch(subject, action_type, args,
                                       timeout=timeout)

    return _execute


def summarize_call(name: str, args: dict) -> str:
    """One-line human summary for approval cards and progress events."""
    if name == "execute_bash":
        return f"Run: {str(args.get('command', ''))[:200]}"
    if name == "write_file":
        return f"Write {args.get('path', '')} " \
            f"({len(str(args.get('content', '')))} bytes)"
    if name == "read_file":
        return f"Read {args.get('path', '')}"
    if name == "list_dir":
        return f"List {args.get('path', '')}"
    if name == "code_search":
        return f"Search {args.get('pattern', '')} in {args.get('path', '')}"
    if name in ("git_status", "git_diff", "git_log"):
        return f"Git {name.split('_', 1)[1]} at {args.get('path', '')}"
    if name == "process_list":
        return "List processes"
    if name == "screenshot":
        return f"Screenshot {str(args.get('url', ''))[:200]}"
    if name == "memory_save":
        return f"Save memory ({len(str(args.get('content', '')))} chars)"
    if name == "memory_search":
        return f"Search memory: {str(args.get('query', ''))[:150]}"
    if name == "memory_list":
        return "List memories"
    if name == "project_create":
        return f"Create project {str(args.get('name', ''))[:100]}"
    if name == "project_list":
        return "List projects"
    if name == "task_state_set":
        return f"Set task state {str(args.get('task_key', 'default'))[:50]}"
    if name == "task_state_get":
        return f"Get task state {str(args.get('task_key', 'default'))[:50]}"
    if name == "checkpoint_create":
        return f"Checkpoint: {str(args.get('note', ''))[:150]}"
    return f"{name} {json.dumps(args)[:200]}"


def approval_detail(name: str, args: dict) -> str:
    """Fuller detail for the approval card (the user approving sees
    their own data; the client renders it as text)."""
    if name == "execute_bash":
        return str(args.get("command", ""))
    if name == "write_file":
        content = str(args.get("content", ""))
        preview = content[:_PREVIEW_CHARS]
        if len(content) > _PREVIEW_CHARS:
            preview += f"\n… ({len(content)} bytes total)"
        return f"{args.get('path', '')}\n---\n{preview}"
    return json.dumps(args)[:_PREVIEW_CHARS]


def _preview(result: dict) -> str:
    try:
        rendered = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(result)
    if len(rendered) > _PREVIEW_CHARS:
        return rendered[:_PREVIEW_CHARS] + "…"
    return rendered


def _result_ok(result: dict) -> bool:
    return result.get("status") not in (
        "error", "agent_offline", "agent_timeout", "declined", "blocked")


def _coerce_int(value, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


async def _run_read(executor, name: str, args: dict,
                    agent_routed: bool) -> dict:
    """Execute a read-only tool: policy gate, then agent or local."""
    try:
        before_tool_call(name, args, agent_routed=agent_routed)
    except tool_executor.ToolBlocked as e:
        return {"status": "blocked", "error": f"Blocked: {e}"}
    if executor is not None:
        return await executor(name, args)
    if name == "read_file":
        return await tool_executor.read_file(str(args.get("path", "")))
    if name == "list_dir":
        return await tool_executor.list_dir(
            str(args.get("path", "")),
            _coerce_int(args.get("limit"),
                        tool_executor.LIST_DIR_DEFAULT_LIMIT),
            bool(args.get("show_hidden", False)),
        )
    if name == "code_search":
        return await tool_executor.search_code(
            str(args.get("pattern", "")), str(args.get("path", "")),
            _coerce_int(args.get("max_results"),
                        tool_executor.SEARCH_DEFAULT_MAX_RESULTS),
        )
    if name == "git_status":
        return await tool_executor.git_status(str(args.get("path", "")))
    if name == "git_diff":
        return await tool_executor.git_diff(str(args.get("path", "")))
    if name == "git_log":
        return await tool_executor.git_log(
            str(args.get("path", "")),
            _coerce_int(args.get("limit"), 20),
        )
    if name == "process_list":
        return await tool_executor._list_processes(
            _coerce_int(args.get("limit"),
                        tool_executor.PROCESSES_DEFAULT_LIMIT)
        )
    return {"status": "error", "error": f"Unknown tool: {name}"}


def _stage_mutating(store, name: str, args: dict,
                    owner_subject: int) -> dict:
    """Stage execute_bash/write_file for approval (denylist first).
    Raises ``ToolBlocked`` (mapped to an error result, no token)."""
    if name == "execute_bash":
        return tool_executor.execute_bash(
            str(args.get("command", "")), store,
            owner_subject=owner_subject)
    if name == "write_file":
        return tool_executor.write_file(
            str(args.get("path", "")), str(args.get("content", "")),
            store, owner_subject=owner_subject)
    raise tool_executor.ToolBlocked(f"Unknown mutating tool: {name}")


async def _webchat_project_id(engine, user_id: int, args: dict):
    """Resolve webchat memory tools' optional ``project`` name to id.

    Returns ``(project_id, error)`` - exactly one is set. Mirrors
    ``endpoints/mcp.py::_mcp_project_id``: unknown names error rather
    than silently de-scoping.
    """
    name = str(args.get("project") or "").strip()
    if not name:
        return None, None
    if engine is None:
        return None, "Project lookup is not available on this server."
    project_id = await resolve_project_by_name(engine, user_id, name)
    if project_id is None:
        return None, (
            f"Unknown project: {name}. Save without 'project' for "
            "user-scope, or use one of the user's existing project names."
        )
    return project_id, None


async def _run_screenshot(executor, args: dict) -> dict:
    """Agent-only screenshot: never falls back to local (SSRF posture).

    Mirrors POST /mcp screenshot: without routing, report unavailability
    instead of fetching caller URLs on the server host.
    """
    if executor is None:
        return {
            "status": "unavailable",
            "reason": (
                "Screenshots run on your paired machine: "
                "set INVINCIBLE_AGENT_ROUTING=1 and start "
                "one with: invincible harness connect"
            ),
        }
    return await executor("screenshot", {"url": str(args.get("url", ""))})


async def _run_data_tool(
    name: str,
    args: dict,
    *,
    principal: Principal,
    mode: str,
    memory,
    retrieval,
    continuity,
    sessions,
    engine,
) -> tuple[dict, bool]:
    """Execute one MCP-parity data tool for webchat. Returns (result, ok).

    Same validation, ownership predicates, kill-switch, and caps as
    POST /mcp so the two surfaces can never drift. No confirm gate:
    data-plane rows are user-owned and dashboard-reversible.
    """
    user_id = principal.user_id
    # -- memory_save --
    if name == "memory_save":
        if not settings.memory_enabled():
            return {
                "status": "error",
                "error": "Memory saving is disabled on this server "
                         "(INVINCIBLE_MEMORY is off).",
            }, False
        if memory is None:
            return {
                "status": "error",
                "error": "Memory store is not available on this server.",
            }, False
        content = str(args.get("content") or "").strip()
        if not content:
            return {
                "status": "error",
                "error": "memory_save requires non-empty 'content'.",
            }, False
        if len(content) > MAX_CONTENT_CHARS:
            return {
                "status": "error",
                "error": f"Memory content must be at most "
                         f"{MAX_CONTENT_CHARS} characters.",
            }, False
        kind = str(args.get("kind") or "note")
        if kind not in MEMORY_KINDS:
            return {
                "status": "error",
                "error": "kind must be one of: " + ", ".join(MEMORY_KINDS),
            }, False
        project_id, project_error = await _webchat_project_id(
            engine, user_id, args)
        if project_error:
            return {"status": "error", "error": project_error}, False
        made_id = await memory.save_memory(
            user_id=user_id,
            content=content,
            layer="explicit",
            kind=kind,
            confidence=MCP_CONFIDENCE,
            provenance=f"webchat:{mode}",
            project_id=project_id,
        )
        return {
            "saved": True,
            "id": made_id,
            "kind": kind,
            "scope": "project" if project_id is not None else "user",
        }, True
    # -- memory_search --
    if name == "memory_search":
        if retrieval is None:
            return {
                "status": "error",
                "error": "Memory retrieval is not available on this server.",
            }, False
        query = str(args.get("query") or "").strip()
        if not query:
            return {
                "status": "error",
                "error": "memory_search requires non-empty 'query'.",
            }, False
        limit = _coerce_int(args.get("limit"), _MEMORY_SEARCH_DEFAULT)
        limit = max(1, min(limit, _MEMORY_SEARCH_MAX))
        project_id, project_error = await _webchat_project_id(
            engine, user_id, args)
        if project_error:
            return {"status": "error", "error": project_error}, False
        found = await retrieval.retrieve(
            user_id=user_id, query=query,
            project_id=project_id, limit=limit,
        )
        return {
            "results": [
                {
                    "id": m.id, "kind": m.kind, "content": m.content,
                    "relevance": round(m.score, 4),
                    "created_at": m.created_at,
                }
                for m in found
            ],
            "count": len(found),
        }, True
    # -- memory_list --
    if name == "memory_list":
        if memory is None:
            return {
                "status": "error",
                "error": "Memory store is not available on this server.",
            }, False
        limit = _coerce_int(args.get("limit"), _MEMORY_LIST_DEFAULT)
        limit = max(1, min(limit, _MEMORY_LIST_MAX))
        kind = args.get("kind")
        if kind is not None and kind not in MEMORY_KINDS:
            return {
                "status": "error",
                "error": "kind must be one of: " + ", ".join(MEMORY_KINDS),
            }, False
        project_id, project_error = await _webchat_project_id(
            engine, user_id, args)
        if project_error:
            return {"status": "error", "error": project_error}, False
        rows = await memory.list_for_user(
            user_id, kind=kind, project_id=project_id, limit=limit)
        return {"memories": rows, "count": len(rows)}, True
    # -- project_create / project_list --
    if name in ("project_create", "project_list"):
        if engine is None:
            return {
                "status": "error",
                "error": "Project tools are not available on this server.",
            }, False
        service = ProjectService(engine)
        if name == "project_list":
            include_archived = args.get("include_archived") is True
            listing = await service.list(
                user_id, include_archived=include_archived)
            return {"projects": listing, "count": len(listing)}, True
        project_name = str(args.get("name") or "").strip()
        if not project_name:
            return {
                "status": "error",
                "error": "project_create requires non-empty 'name'.",
            }, False
        if len(project_name) > 100:
            return {
                "status": "error",
                "error": "Project name must be at most 100 characters.",
            }, False
        try:
            made = await service.create(user_id, project_name)
        except AccountError as exc:
            return {"status": "error", "error": exc.message}, False
        listing = await service.list(user_id)
        if len(listing) > _PROJECT_CAP:
            from sqlalchemy import text as _text

            async with engine.begin() as conn:
                await conn.execute(
                    _text("DELETE FROM projects WHERE id = :id"),
                    {"id": made["id"]},
                )
            return {
                "status": "error",
                "error": f"Project limit reached ({_PROJECT_CAP} projects). "
                         "Archive or rename existing ones first.",
            }, False
        return {
            "created": True, "id": made["id"], "name": made["name"],
        }, True
    # -- task_state_set / get / checkpoint_create --
    if name in ("task_state_set", "task_state_get", "checkpoint_create"):
        if continuity is None or sessions is None:
            return {
                "status": "error",
                "error": "Continuity engine not initialized on this server.",
            }, False
        session_id = str(args.get("session_id") or "") or "webchat"
        task_key = str(args.get("task_key") or "") or "default"
        try:
            if name == "task_state_get":
                session_pk = await sessions.lookup(
                    session_id, user_id=user_id,
                    project_id=principal.project_id,
                )
            else:
                session_pk = await sessions.resolve_or_create(
                    session_id, user_id=user_id,
                    project_id=principal.project_id,
                )
        except Exception:
            return {
                "status": "error",
                "error": "Could not resolve the session for this subject.",
            }, False
        if name == "task_state_get" and session_pk is None:
            return {
                "note": f"no state tracked for task "
                        f"'{task_key}' in this session",
                "payload": None, "version": 0,
            }, True
        try:
            if name == "task_state_set":
                try:
                    payload = json.loads(args.get("payload") or "")
                except (json.JSONDecodeError, TypeError):
                    return {
                        "status": "error",
                        "error": "payload must be a JSON object.",
                    }, False
                head = await continuity.set_state(
                    session_id, payload,
                    actor=f"webchat:{user_id}:task_state_set",
                    task_key=task_key,
                    status=str(args.get("status") or "active"),
                    expected_version=args.get("expected_version"),
                    session_pk=session_pk,
                )
                return head, True
            if name == "task_state_get":
                state = await continuity.get_state(
                    session_id, task_key, session_pk=session_pk)
                if state is None:
                    return {
                        "note": f"no state tracked for task "
                                f"'{task_key}' in this session",
                        "payload": None, "version": 0,
                    }, True
                return state, True
            cp = await continuity.create_checkpoint(
                session_id, task_key=task_key,
                note=str(args.get("note") or ""),
                session_pk=session_pk,
            )
            return cp, True
        except ContinuityConflictError as e:
            return {"status": "error", "error": str(e)}, False
        except ValueError as e:
            return {"status": "error", "error": str(e)}, False
    return {"status": "error", "error": f"Unknown tool: {name}."}, False


def _error_text(body: object) -> str:
    return upstream_error_detail(body) or "gateway error"


async def run_agent_turn(
    prepared,
    *,
    model: str | None,
    router,
    sessions,
    memory,
    runs_store,
    principal: Principal,
    mode: str,
    pending_store,
    executor,
    waiter: ApprovalWaiter,
    audit=None,
    retrieval=None,
    continuity=None,
    engine=None,
):
    """Run one agentic turn, yielding ``(event, data)`` pairs.

    ``prepared`` is a ``chat_service.PreparedChat`` for the user's new
    turn. Each iteration routes through the single router loop with the
    mode's tool schemas; tool results feed the next iteration until the
    model answers with text (or the iteration cap forces a no-tools
    closing call). Persistence mirrors the text path (one append of the
    whole turn + memory + stream usage row) so history stays identical
    in shape to API-path turns.
    """
    tools = _TOOLS_BY_MODE[mode]
    full = prepared.full_messages + [
        {"role": "system", "content": MODE_SYSTEM_PROMPTS[mode]}
    ]
    turn = list(prepared.to_persist)
    tools_used = 0
    full_text = ""
    agent_routed = executor is not None

    async def _audit(action: str, **meta):
        if audit is not None:
            try:
                await audit(action, **meta)
            except Exception:
                logger.warning("webchat audit write failed for %s", action)

    async def _persist_partial():
        if turn:
            try:
                await sessions.append(
                    prepared.session_id, turn,
                    user_id=principal.user_id,
                    project_id=principal.project_id,
                    max_turns=prepared.max_turns,
                )
            except Exception:
                logger.exception("Failed to persist partial webchat turn")

    def _route_error(exc: Exception):
        if isinstance(exc, NoCredentialsConfiguredError):
            return 400, {"error": {
                "message": ("No AI provider is connected for this account. "
                            "Connect one at /dashboard/providers."),
                "type": "invalid_request_error"}}
        if isinstance(exc, UpstreamClientError):
            return exc.status_code, exc.body
        if isinstance(exc, AllProvidersFailedError):
            return 503, {"error": {"message": str(exc),
                                   "type": "gateway_error"}}
        logger.exception("webchat agent turn failed")
        return 503, {"error": {"message": "gateway error",
                               "type": "gateway_error"}}

    iteration = 0
    while True:
        current_tools = tools if iteration <= MAX_TOOL_ITERATIONS else None
        if iteration > MAX_TOOL_ITERATIONS:
            full = full + [{
                "role": "system",
                "content": ("Tool budget exhausted - answer the user now "
                            "with what you have, no more tool calls."),
            }]
        try:
            result, info = await router.route_request_detailed(
                full, tools=current_tools, model=model,
                session_id=prepared.session_id,
                session_pk=prepared.session_pk, **prepared.byok_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - mapped below, never leaks
            await _persist_partial()
            status, body = _route_error(exc)
            yield "error", {"message": _error_text(body),
                            "status": status}
            return
        choices = result.get("choices") or []
        message = choices[0].get("message") if choices else None
        if not isinstance(message, dict):
            await _persist_partial()
            yield "error", {"message": "gateway error"}
            return
        text = message.get("content")
        if isinstance(text, str) and text:
            full_text += text
            yield "token", {"text": text}
        calls = message.get("tool_calls") or []
        if not calls:
            final_message = {"role": "assistant", "content": text or None}
            await _persist_new_turns(
                turn, final_message, sessions, prepared.session_id,
                memory, principal, runs_store=runs_store,
                request_id=info["request_id"],
                max_turns=prepared.max_turns,
            )
            yield "done", {
                "text": full_text,
                "provider": info["provider_name"],
                "model": info["model_id"],
                "attempts": info["attempts"],
                "mode": mode,
                "tools_used": tools_used,
                "execution": "agent" if agent_routed else "local",
            }
            return
        # The model wants tools: persist the assistant turn as-is (ids
        # preserved, so the pairing invariant holds on replay).
        turn.append(message)
        full.append(message)
        for call in calls:
            call_id = call.get("id") or f"call_{len(turn)}"
            fname = ((call.get("function") or {}).get("name")) or ""
            raw_args = ((call.get("function") or {}).get("arguments")) or "{}"
            try:
                fargs = json.loads(raw_args) if isinstance(
                    raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError, ValueError):
                fargs = None
            if not isinstance(fargs, dict):
                result_doc = {"status": "error",
                              "error": "Tool arguments were not valid JSON."}
                ok = False
            elif fname not in (
                READ_ONLY_TOOLS + MUTATING_TOOLS
                + ALL_DATA_TOOLS + AGENT_ONLY_TOOLS
            ):
                result_doc = {"status": "error",
                              "error": f"Unknown tool: {fname}."}
                ok = False
            else:
                yield "tool_call", {
                    "call_id": call_id, "name": fname,
                    "summary": summarize_call(fname, fargs),
                }
                outcome: dict = {}
                async for ev_name, ev_data in _execute_call(
                    fname, fargs, call_id=call_id, mode=mode,
                    ctx_principal=principal, pending_store=pending_store,
                    executor=executor, waiter=waiter, audit=_audit,
                    outcome=outcome, memory=memory,
                    retrieval=retrieval, continuity=continuity,
                    sessions=sessions, engine=engine,
                ):
                    yield ev_name, ev_data
                result_doc, ok = outcome["result"], outcome["ok"]
            tools_used += 1
            tool_message = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result_doc, ensure_ascii=False),
            }
            turn.append(tool_message)
            full.append(tool_message)
            yield "tool_result", {
                "call_id": call_id, "name": fname, "ok": ok,
                "preview": _preview(result_doc),
            }
        iteration += 1


async def _execute_call(
    name: str, args: dict, *, call_id: str, mode: str,
    ctx_principal: Principal, pending_store, executor,
    waiter: ApprovalWaiter, audit, outcome: dict,
    memory=None, retrieval=None, continuity=None,
    sessions=None, engine=None,
):
    """Execute one validated tool call per the mode.

    Yields zero or one ``("approval", {...})`` event (manual mutating
    path - the browser must learn the token BEFORE the waiter blocks),
    then records ``outcome["result"]`` / ``outcome["ok"]`` (fed back to
    the model) and returns.
    """
    if name in READ_ONLY_TOOLS:
        result = await _run_read(executor, name, args,
                                 agent_routed=executor is not None)
        outcome["result"], outcome["ok"] = result, _result_ok(result)
        return
    if name in AGENT_ONLY_TOOLS:
        result = await _run_screenshot(executor, args)
        outcome["result"], outcome["ok"] = result, _result_ok(result)
        return
    if name in ALL_DATA_TOOLS:
        result, ok = await _run_data_tool(
            name, args, principal=ctx_principal, mode=mode,
            memory=memory, retrieval=retrieval, continuity=continuity,
            sessions=sessions, engine=engine,
        )
        with contextlib.suppress(Exception):
            await audit(
                f"webchat.{name}.{'ok' if ok else 'error'}",
                meta={"action": name, "call_id": call_id},
            )
        outcome["result"], outcome["ok"] = result, ok
        return
    # Mutating tools: stage first (denylist enforced, no token on hit).
    try:
        staged = _stage_mutating(pending_store, name, args,
                                 ctx_principal.user_id)
    except tool_executor.ToolBlocked as e:
        await audit("webchat.tool.blocked",
                    meta={"action": name, "call_id": call_id})
        outcome["result"] = {"status": "blocked",
                             "error": f"Blocked: {e}"}
        outcome["ok"] = False
        return
    token = staged["token"]
    if mode == "auto":
        result = await tool_executor.confirm_action(
            pending_store, token, True,
            requester_subject=ctx_principal.user_id, executor=executor)
        await audit("webchat.tool.executed",
                    meta={"action": name, "mode": "auto"})
        outcome["result"], outcome["ok"] = result, _result_ok(result)
        return
    # Manual: tell the browser first, then pause for its answer. The
    # endpoint resolves the waiter on approve/deny; timeout or stream
    # disconnect declines.
    yield "approval", {
        "token": token,
        "call_id": call_id,
        "action": name,
        "summary": summarize_call(name, args),
        "detail": approval_detail(name, args),
    }
    result, ok = await _await_approval(
        name, token=token, ctx_principal=ctx_principal,
        pending_store=pending_store, executor=executor, waiter=waiter,
        audit=audit)
    outcome["result"], outcome["ok"] = result, ok


async def _await_approval(
    name: str, *, token: str,
    ctx_principal: Principal, pending_store, executor,
    waiter: ApprovalWaiter, audit,
) -> tuple[dict, bool]:
    """Post-event half of manual approval: waits for the browser's
    answer, then confirms or declines the staged action."""
    approved = await waiter.wait(token, ctx_principal.user_id)
    if approved:
        result = await tool_executor.confirm_action(
            pending_store, token, True,
            requester_subject=ctx_principal.user_id, executor=executor)
        await audit("webchat.tool.approved",
                    meta={"action": name, "mode": "manual"})
        return result, _result_ok(result)
    # Denied or timed out: pop the staged action (single-use either way)
    # and hand the model a declined result it can react to.
    result = await tool_executor.confirm_action(
        pending_store, token, False,
        requester_subject=ctx_principal.user_id, executor=executor)
    await audit("webchat.tool.declined", meta={"action": name})
    if isinstance(result, dict) and result.get("status") == "not_found":
        # Token already gone (disconnect cleanup raced us) - same meaning.
        return {"status": "declined",
                "message": "The action was not approved."}, False
    return result, False
