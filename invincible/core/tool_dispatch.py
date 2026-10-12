# invincible/core/tool_dispatch.py
"""Shared tool-execution primitives for the webchat agent loop.

Neutral home for the dispatch helpers formerly defined in
``core/webchat_agent.py`` (moved verbatim in Step 8 Phase 1a): machine
reads, agent-only reads, staged mutating calls, and the MCP-parity
data-plane tools. Both the main agent loop (``webchat_agent``) and the
explore sub-agent (``harness_explore``) import from here, which is what
breaks the would-be import cycle between those two modules.

Layering: pure ``core/`` business logic - no FastAPI, no Router import,
no ``endpoints/`` imports. Callers inject the router, stores, executor,
and waiter; this module only executes individual tool calls. Wire
behavior is unchanged by the move (pinned by ``tests/test_webchat_agent.py``,
``tests/test_todo_tool.py``, and the harness golden suites).
"""
import json
import logging

from invincible.core import harness_tools, tool_executor
from invincible.core.accounts import AccountError, ProjectService
from invincible.core.continuity import ContinuityConflictError
from invincible.core.harness_policy import before_tool_call
from invincible.core.identity import resolve_project_by_name
from invincible.core.memory import (
    MAX_CONTENT_CHARS,
    MCP_CONFIDENCE,
    MEMORY_KINDS,
)
from invincible.core.principal import Principal
from invincible.core.settings import settings
from invincible.core.todos import run_todo

logger = logging.getLogger("invincible.webchat.dispatch")

# Same derivations as the live loop (single metadata source stays
# ``core/harness_tools.py``; these are local aliases, not new views).
READ_ONLY_TOOLS = harness_tools.read_only_names()
AGENT_ONLY_TOOLS = harness_tools.agent_only_names()
DATA_READ_TOOLS = harness_tools.data_read_names()
DATA_WRITE_TOOLS = harness_tools.data_write_names()
ALL_DATA_TOOLS = DATA_READ_TOOLS + DATA_WRITE_TOOLS
READ_ONLY_BATCH_TOOLS = frozenset(
    READ_ONLY_TOOLS + AGENT_ONLY_TOOLS + DATA_READ_TOOLS)
# Response caps mirrored from POST /mcp (endpoints/mcp.py): MCP results
# land in model context, so the same token discipline applies here.
_MEMORY_SEARCH_DEFAULT = harness_tools.MEMORY_SEARCH_DEFAULT
_MEMORY_SEARCH_MAX = harness_tools.MEMORY_SEARCH_MAX
_MEMORY_LIST_DEFAULT = harness_tools.MEMORY_LIST_DEFAULT
_MEMORY_LIST_MAX = harness_tools.MEMORY_LIST_MAX
_PROJECT_CAP = harness_tools.PROJECT_CAP


def result_ok(result: dict) -> bool:
    return result.get("status") not in (
        "error", "agent_offline", "agent_timeout", "declined", "blocked")


def coerce_int(value, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


async def run_read(executor, name: str, args: dict,
                   agent_routed: bool) -> dict:
    """Execute a read-only tool: policy gate, then agent or local."""
    try:
        before_tool_call(name, args, agent_routed=agent_routed)
    except tool_executor.ToolBlocked as e:
        return {"status": "blocked", "error": f"Blocked: {e}"}
    if executor is not None:
        return await executor(name, args)
    if name == "read_file":
        limit_raw = args.get("limit")
        return await tool_executor.read_file(
            str(args.get("path", "")),
            coerce_int(args.get("offset"), 1),
            None if limit_raw is None
            else coerce_int(limit_raw, 0),
        )
    if name == "list_dir":
        return await tool_executor.list_dir(
            str(args.get("path", "")),
            coerce_int(args.get("limit"),
                       tool_executor.LIST_DIR_DEFAULT_LIMIT),
            bool(args.get("show_hidden", False)),
        )
    if name == "find_files":
        return await tool_executor.find_files(
            str(args.get("pattern", "")), str(args.get("path", "")),
            coerce_int(args.get("max_results"),
                       tool_executor.FIND_DEFAULT_MAX_RESULTS),
            bool(args.get("show_hidden", False)),
        )
    if name == "code_search":
        return await tool_executor.search_code(
            str(args.get("pattern", "")), str(args.get("path", "")),
            coerce_int(args.get("max_results"),
                       tool_executor.SEARCH_DEFAULT_MAX_RESULTS),
        )
    if name == "git_status":
        return await tool_executor.git_status(str(args.get("path", "")))
    if name == "git_diff":
        return await tool_executor.git_diff(str(args.get("path", "")))
    if name == "git_log":
        return await tool_executor.git_log(
            str(args.get("path", "")),
            coerce_int(args.get("limit"), 20),
        )
    if name == "process_list":
        return await tool_executor._list_processes(
            coerce_int(args.get("limit"),
                       tool_executor.PROCESSES_DEFAULT_LIMIT)
        )
    valid = ", ".join(
        n for n in harness_tools.webchat_tool_names()
        if n in READ_ONLY_BATCH_TOOLS)
    return {"status": "error",
            "error": f"Unknown tool: {name}. Valid tools: {valid}."}


def stage_mutating(store, name: str, args: dict,
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
    if name == "edit_file":
        # JSON null is not the string "None": a null new_string deletes
        # text, a null/missing old_string errors like an empty one.
        old_raw = args.get("old_string")
        new_raw = args.get("new_string")
        return tool_executor.edit_file(
            str(args.get("path", "")),
            "" if old_raw is None else str(old_raw),
            "" if new_raw is None else str(new_raw),
            store, replace_all=args.get("replace_all", False) is True,
            owner_subject=owner_subject)
    raise tool_executor.ToolBlocked(
        f"Unknown mutating tool: {name}. Valid tools: "
        f"{', '.join(harness_tools.approval_required_names())}.")


async def webchat_project_id(engine, user_id: int, args: dict):
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


async def run_screenshot(executor, args: dict) -> dict:
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
                "one with: invincible connect"
            ),
        }
    return await executor("screenshot", {"url": str(args.get("url", ""))})


async def run_web_fetch(executor, args: dict) -> dict:
    """Agent-only URL fetch: never falls back to local (SSRF posture).

    Mirrors POST /mcp web_fetch: without routing, report unavailability
    instead of fetching caller URLs on the server host.
    """
    if executor is None:
        return {
            "status": "unavailable",
            "reason": (
                "Web fetches run on your paired machine: "
                "set INVINCIBLE_AGENT_ROUTING=1 and start "
                "one with: invincible connect"
            ),
        }
    return await executor("web_fetch", {"url": str(args.get("url", ""))})


async def run_web_research(engine, user_id: int, args: dict) -> dict:
    """Server-side research through the caller's own provider.

    Unlike agent-only reads this never touches the paired machine:
    the question is free text and the provider host comes from the
    user's stored credential (see ``core/web_research.py``).
    """
    from invincible.core import web_research

    return await web_research.run_research(engine, user_id, args or {})


async def run_data_tool(
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
        project_id, project_error = await webchat_project_id(
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
        limit = coerce_int(args.get("limit"), _MEMORY_SEARCH_DEFAULT)
        limit = max(1, min(limit, _MEMORY_SEARCH_MAX))
        project_id, project_error = await webchat_project_id(
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
        limit = coerce_int(args.get("limit"), _MEMORY_LIST_DEFAULT)
        limit = max(1, min(limit, _MEMORY_LIST_MAX))
        kind = args.get("kind")
        if kind is not None and kind not in MEMORY_KINDS:
            return {
                "status": "error",
                "error": "kind must be one of: " + ", ".join(MEMORY_KINDS),
            }, False
        project_id, project_error = await webchat_project_id(
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
                        "error": (
                            "payload must be a JSON object (a {...} "
                            "mapping encoded as a string)."
                            f"{harness_tools.expected_args_hint(name)}"
                        ),
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
    # -- todo --
    if name == "todo":
        if continuity is None or sessions is None:
            return {
                "status": "error",
                "error": "Continuity engine not initialized on this server.",
            }, False
        session_id = str(args.get("session_id") or "") or "webchat"
        action = str(args.get("action") or "").strip()
        try:
            if action == "list":
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
        result, ok = await run_todo(
            continuity, session_id=session_id, session_pk=session_pk,
            action=action, text=str(args.get("text") or ""),
            todo_id=str(args.get("id") or ""),
            actor=f"webchat:{user_id}:todo",
        )
        if not ok:
            result = {
                "status": "error",
                "error": str(result.get("error", "todo failed"))
                + harness_tools.expected_args_hint(name),
            }
        return result, ok
    valid = ", ".join(
        n for n in harness_tools.webchat_tool_names()
        if n in ALL_DATA_TOOLS + AGENT_ONLY_TOOLS)
    return {"status": "error",
            "error": f"Unknown tool: {name}. Valid tools: {valid}."}, False
