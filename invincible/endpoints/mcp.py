# invincible/endpoints/mcp.py
"""Minimal MCP (Model Context Protocol) tool server.

Exposed over HTTP so a cloud-hosted AI reaching this machine through a
tunnel can call execute_bash and write_file. Speaks the JSON-RPC 2.0 shape
MCP clients expect for initialize / tools/list / tools/call - just enough
surface for this server's own use, not a general-purpose transport.

Auth is OAuth 2.1 + PKCE (RFC 9728 resource-server binding): /mcp accepts
short-lived Bearer access tokens issued by the built-in authorization
server (/oauth/*). A 401 carries a WWW-Authenticate header pointing at
/.well-known/oauth-protected-resource so MCP-compatible clients can
auto-discover the authorization server. The owner secret is no longer sent
on every request - it only ever appears in the browser login form on
/oauth/authorize.

Approval for execute_bash/write_file is remote and token-based: a call
stages a pending action and returns a token; only a confirm_action call
with that token (approve=true) executes it. Whoever holds a valid Bearer
token is the approver, not whoever happens to be sitting at the machine.

The memory tools (memory_save / memory_search / memory_list) are
data-plane instead of machine-plane: they read and write the caller's
own rows in the shared memories store with no confirmation gate - the
same risk class as chat-side "remember this" - and every query is
predicated on the OAuth subject's user_id.
"""
import contextlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from invincible import __version__
from invincible.core import tool_executor
from invincible.core.continuity import ContinuityConflictError
from invincible.core.identity import resolve_project_by_name
from invincible.core.memory import (
    MAX_CONTENT_CHARS,
    MCP_CONFIDENCE,
    MEMORY_KINDS,
)
from invincible.core.oauth_store import OAuthStore
from invincible.core.principal import Principal
from invincible.core.settings import AGENT_JOB_GRACE_SECONDS, settings

router = APIRouter()

# Response caps for the memory tools: MCP results land in an AI's context
# window, so they obey the same token discipline as prompt injection.
_MEMORY_SEARCH_DEFAULT = 5
_MEMORY_SEARCH_MAX = 10
_MEMORY_LIST_DEFAULT = 10
_MEMORY_LIST_MAX = 20

TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read a file's contents from the host machine. Reads are "
            "sandboxed to the server's working directory and repo root "
            "(extend with INVINCIBLE_READ_ROOTS); files holding secrets or "
            "sensitive state (.env, sessions.db, .git/) are rejected "
            "outright wherever they sit. No confirmation is required for "
            "other files since reading is non-destructive."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "execute_bash",
        "description": (
            "Run a shell command on the host machine. Commands matching the "
            "denylist (destructive filesystem ops, privilege escalation, "
            "power commands, etc.) are rejected outright. Everything else "
            "is staged for approval: the call returns a token, and the "
            "command only runs after confirm_action is called with that "
            "token and approve=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file on the host machine. Writes to files "
            "this server depends on for its own security or state (.env, "
            "providers.yaml, sessions.db, its own source/tests, .git/) are "
            "rejected outright. Everything else is staged for approval: "
            "the call returns a token, and the file is only written after "
            "confirm_action is called with that token and approve=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "confirm_action",
        "description": (
            "Approve or deny a pending execute_bash/write_file request. "
            "Must be called with the exact token returned by that request. "
            "approve=true performs the action immediately (runs the "
            "command / writes the file); approve=false discards it without "
            "executing anything. This is how operator approval is obtained: "
            "an action is never executed until this tool confirms it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "token": {"type": "string"},
                "approve": {"type": "boolean"},
            },
            "required": ["token", "approve"],
        },
    },
    {
        "name": "task_state_set",
        "description": (
            "Persist canonical task progress into Invincible's shared "
            "continuity store for this session. Every later LLM request "
            "(any provider/model) receives this state as its continuation "
            "brief, and later MCP reads return it - one canonical store, "
            "no per-model memory. Payload must be a JSON OBJECT of "
            "structured facts you want preserved verbatim (e.g. "
            '{"task":"count 1-100","completed_through":5,"next_value":6}).'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "payload": {"type": "string",
                            "description": "JSON object of structured state"},
                "task_key": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["active", "blocked", "done",
                                    "cancelled"]},
                "expected_version": {"type": "integer",
                                     "description": "optimistic CAS guard"},
                "session_id": {"type": "string"},
            },
            "required": ["payload"],
        },
    },
    {
        "name": "task_state_get",
        "description": (
            "Read the latest trusted task state previously persisted via "
            "task_state_set (or any other writer). Returns "
            "{status,payload,version} or a note when nothing is tracked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_key": {"type": "string"},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "checkpoint_create",
        "description": (
            "Snapshot the current task-state version as a named checkpoint "
            "(e.g. 'completed through 37'). Checkpoints mark reliable "
            "progress points that survive provider failover and appear in "
            "the session's continuation brief."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string"},
                "task_key": {"type": "string"},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "memory_save",
        "description": (
            "Deliberately store a fact about the user or one of their "
            "projects into their memory store - the same store their "
            "dashboard and gateway chats read. Use it for durable facts "
            "worth recalling in later sessions (preferences, decisions, "
            "working context); for transient task progress use "
            "task_state_set instead. No confirmation is required: rows "
            "are user-owned data, reversible from the dashboard."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "the fact to remember, "
                                   f"1-{MAX_CONTENT_CHARS} characters",
                },
                "kind": {
                    "type": "string",
                    "enum": list(MEMORY_KINDS),
                    "description": "coarse classifier (default: note)",
                },
                "project": {
                    "type": "string",
                    "description": "one of the user's project names; "
                                   "tags the memory to that project "
                                   "(default: user-scope)",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_search",
        "description": (
            "Search the user's memory store with the same ranking their "
            "gateway chats use (lexical relevance x recency x "
            "confidence). Returns a small ranked list, never a dump - "
            "look here before asking the user something you may "
            "already know."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {
                    "type": "string",
                    "description": "restrict to that project's "
                                   "memories plus user-scope ones",
                },
                "limit": {
                    "type": "integer",
                    "description": f"max results, 1-{_MEMORY_SEARCH_MAX} "
                                   f"(default {_MEMORY_SEARCH_DEFAULT})",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_list",
        "description": (
            "Browse the user's most recent memories, newest first - "
            "useful for bootstrapping context at the start of a "
            "session. Optional kind/project filters; capped at "
            f"{_MEMORY_LIST_MAX} rows. There is deliberately no "
            "memory_delete over MCP: deletion stays a human, "
            "dashboard-only action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"max rows, 1-{_MEMORY_LIST_MAX} "
                                   f"(default {_MEMORY_LIST_DEFAULT})",
                },
                "kind": {
                    "type": "string",
                    "enum": list(MEMORY_KINDS),
                },
                "project": {
                    "type": "string",
                    "description": "that project's memories plus "
                                   "user-scope ones",
                },
            },
        },
    },
]


def _auth_error(request: Request):
    """401 with the RFC 9728 WWW-Authenticate challenge so MCP clients can
    discover the authorization server instead of failing silently."""
    base = str(request.base_url).rstrip("/")
    return HTTPException(
        status_code=401,
        headers={
            "WWW-Authenticate": (
                'Bearer resource_metadata='
                f'"{base}/.well-known/oauth-protected-resource"'
            )
        },
        detail={
            "error": {
                "message": "Missing or invalid access token",
                "type": "auth_error",
            }
        },
    )


async def require_mcp_auth(request: Request) -> Principal:
    """Validate the Bearer access token and resolve its user subject
    (Phase 2): tokens act as the user who approved the grant, so MCP
    writes land under that principal's sessions."""
    store: OAuthStore | None = getattr(request.app.state, "oauth_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": "OAuth store not initialized; MCP endpoint is disabled.",
                    "type": "config_error",
                }
            },
        )
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise _auth_error(request)
    token = auth[len("Bearer "):].strip()
    if not token:
        raise _auth_error(request)
    access = await store.validate_access(token)
    if access is None:
        raise _auth_error(request)
    # Remember which OAuth client is calling: memory_save provenance
    # records it (mcp:<client_name>) so the dashboard shows which AI
    # saved each row.
    request.state.mcp_client_id = access.get("client_id")

    from invincible.core.identity import ensure_default_project

    engine = getattr(request.app.state, "engine", None)
    subject = access.get("subject_user_id")
    if engine is None or subject is None:
        # No resolvable subject (pre-0003 database or missing engine):
        # fail closed. A subject-less token must never resolve to the
        # system local owner - that fallback silently mixed tenants
        # (multi-tenant audit Step 2).
        raise _auth_error(request)
    project_id = await ensure_default_project(
        engine, int(subject)
    )
    return Principal(
        user_id=int(subject),
        project_id=project_id,
        kind="mcp",
    )


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


async def _audit_action(request: Request, tool: str, status,
                        *, subject: int | None) -> None:
    """Audit staged-action resolutions. Metadata only - never raw
    commands/paths (those can carry secrets)."""
    log = getattr(request.app.state, "audit_log", None)
    if log is None:
        return
    with contextlib.suppress(Exception):
        await log.record(
            f"mcp.{tool}.{status}",
            actor_user_id=subject,
            actor_kind="mcp",
            resource_type="pending_action",
        )


def _tool_content(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


async def _agent_executor(request: Request, subject: int | None):
    """Build the Phase 10 executor callback for confirm_action.

    Returns None when agent routing is off (the default: every tool
    executes locally on the server host, byte-identical to Phase 9).
    When on, confirmed execute_bash/write_file work - and read_file,
    which has no confirm step of its own - travels to the caller's
    paired agent over its open long-poll. The agent re-runs the
    denylist locally (wall 2) and confines reads/writes to the user's
    own machine; the server never executes anything in this mode,
    which is what makes the consent-gate relaxation safe.
    """
    if not settings.agent_routing():
        return None
    registry = getattr(request.app.state, "agent_registry", None)
    if registry is None:
        return None

    async def _execute(action_type: str, args: dict) -> dict:
        if subject is None or not registry.online(subject):
            return {
                "status": "agent_offline",
                "message": (
                    "No invincible agent is connected for this account. "
                    "Start one on your machine with: invincible agent"
                ),
            }
        timeout = float(args.get("timeout", 30.0)) + AGENT_JOB_GRACE_SECONDS
        return await registry.dispatch(subject, action_type, args,
                                       timeout=timeout)

    return _execute


async def _mcp_project_id(request: Request, principal: Principal, args: dict):
    """Resolve the memory tools' optional ``project`` argument (a project
    NAME the caller owns) to its id.

    Returns ``(project_id, error)`` - exactly one is set. Unknown names
    error rather than silently de-scoping: an AI told it saved to
    "invincible" must learn that it didn't.
    """
    name = str(args.get("project") or "").strip()
    if not name:
        return None, None
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return None, "Project lookup is not available on this server."
    project_id = await resolve_project_by_name(
        engine, principal.user_id, name)
    if project_id is None:
        return None, (
            f"Unknown project: {name}. Save without 'project' for "
            "user-scope, or use one of the user's existing project names."
        )
    return project_id, None


async def _mcp_provenance(request: Request) -> str:
    """Provenance tag for MCP-saved memories: the OAuth client's
    registered name when it has one, else its client_id."""
    client_id = getattr(request.state, "mcp_client_id", None)
    if not client_id:
        return "mcp:local"
    store = getattr(request.app.state, "oauth_store", None)
    if store is not None:
        with contextlib.suppress(Exception):
            client = await store.get_client(client_id)
            if client and client.get("client_name"):
                return f"mcp:{client['client_name']}"
    return f"mcp:{client_id}"


async def _dispatch(method, rpc_id, params, request,
                    principal: Principal | None = None):
    if method == "initialize":
        return _result(rpc_id, {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "invincible-mcp", "version": __version__},
            "capabilities": {"tools": {}},
        })

    if method == "tools/list":
        return _result(rpc_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        pending_actions = request.app.state.pending_actions
        # Phase 2: every staged action is bound to the caller's subject;
        # only the same subject may later confirm it.
        owner_subject = principal.user_id if principal else None

        try:
            if name == "read_file":
                # Phase 10: with agent routing on, the read executes on
                # the caller's own machine (their home is the sandbox
                # there) instead of this server's read roots. No
                # confirm step either way - reading is non-destructive;
                # the agent's sandbox is the gate when routed.
                agent_executor = await _agent_executor(request, owner_subject)
                if agent_executor is not None:
                    result = await agent_executor(
                        "read_file", {"path": args.get("path", "")}
                    )
                else:
                    result = await tool_executor.read_file(
                        args.get("path", ""))
                status = result.get("status")
                if status in ("agent_offline", "agent_timeout"):
                    await _audit_action(request, name, status,
                                        subject=owner_subject)
                    return _result(rpc_id, _tool_content(
                        result.get("message", status), is_error=True
                    ))
                return _result(rpc_id, _tool_content(json.dumps(result)))

            if name == "execute_bash":
                result = tool_executor.execute_bash(
                    args.get("command", ""), pending_actions,
                    owner_subject=owner_subject,
                )
                return _result(rpc_id, _tool_content(json.dumps(result)))

            if name == "write_file":
                result = tool_executor.write_file(
                    args.get("path", ""), args.get("content", ""),
                    pending_actions, owner_subject=owner_subject,
                )
                return _result(rpc_id, _tool_content(json.dumps(result)))

            if name == "confirm_action":
                # Only a real JSON boolean can approve - anything else
                # (absent, string, number) is treated as deny.
                approve = args.get("approve") is True
                result = await tool_executor.confirm_action(
                    pending_actions, args.get("token", ""), approve,
                    requester_subject=owner_subject,
                    # Phase 10: with routing on, confirmed work travels
                    # to the caller's agent instead of running here.
                    executor=await _agent_executor(request, owner_subject),
                )
                status = result.get("status")
                await _audit_action(request, name, status,
                                    subject=owner_subject)
                if status == "not_found":
                    return _result(rpc_id, _tool_content(
                        "Unknown or expired confirmation token.", is_error=True
                    ))
                if status == "declined":
                    return _result(rpc_id, _tool_content("Declined.", is_error=True))
                if status in ("agent_offline", "agent_timeout"):
                    return _result(rpc_id, _tool_content(
                        result.get("message", status), is_error=True
                    ))
                return _result(rpc_id, _tool_content(json.dumps(result)))

            if name in ("task_state_set", "task_state_get", "checkpoint_create"):
                engine = getattr(request.app.state, "continuity", None)
                if engine is None:
                    return _result(rpc_id, _tool_content(
                        "Continuity engine not initialized on this server.",
                        is_error=True,
                    ))
                session_id = args.get("session_id") or "mcp"
                task_key = args.get("task_key") or "default"
                # Phase 2 isolation: resolve-or-create the owning session
                # under the caller's subject, then scope every read/write
                # to its surrogate id. Two principals sharing a client
                # string never touch each other's task chains.
                sessions = getattr(request.app.state, "sessions", None)
                if principal is None or sessions is None:
                    return _result(rpc_id, _tool_content(
                        "Session identity is not available on this server.",
                        is_error=True,
                    ))
                try:
                    if name == "task_state_get":
                        session_pk = await sessions.lookup(
                            session_id,
                            user_id=principal.user_id,
                            project_id=principal.project_id,
                        )
                    else:
                        session_pk = await sessions.resolve_or_create(
                            session_id,
                            user_id=principal.user_id,
                            project_id=principal.project_id,
                        )
                except Exception:
                    return _result(rpc_id, _tool_content(
                        "Could not resolve the session for this subject.",
                        is_error=True,
                    ))
                if name == "task_state_get" and session_pk is None:
                    return _result(rpc_id, _tool_content(json.dumps({
                        "note": f"no state tracked for task "
                                f"'{task_key}' in this session",
                        "payload": None,
                        "version": 0,
                    })))
                try:
                    if name == "task_state_set":
                        try:
                            payload = json.loads(args.get("payload") or "")
                        except json.JSONDecodeError as e:
                            return _result(rpc_id, _tool_content(
                                f"payload must be a JSON object: {e}",
                                is_error=True,
                            ))
                        head = await engine.set_state(
                            session_id,
                            payload,
                            actor=f"mcp:{principal.user_id}:task_state_set",
                            task_key=task_key,
                            status=args.get("status") or "active",
                            expected_version=args.get("expected_version"),
                            session_pk=session_pk,
                        )
                        return _result(rpc_id, _tool_content(json.dumps(head)))
                    if name == "task_state_get":
                        state = await engine.get_state(session_id, task_key,
                                                       session_pk=session_pk)
                        if state is None:
                            return _result(rpc_id, _tool_content(json.dumps({
                                "note": f"no state tracked for task "
                                        f"'{task_key}' in this session",
                                "payload": None,
                                "version": 0,
                            })))
                        return _result(rpc_id, _tool_content(json.dumps(state)))
                    cp = await engine.create_checkpoint(
                        session_id,
                        task_key=task_key,
                        note=args.get("note") or "",
                        actor=f"mcp:{principal.user_id}:checkpoint_create",
                        session_pk=session_pk,
                    )
                    return _result(rpc_id, _tool_content(json.dumps(cp)))
                except ContinuityConflictError as e:
                    return _result(rpc_id, _tool_content(str(e), is_error=True))
                except ValueError as e:
                    return _result(rpc_id, _tool_content(str(e), is_error=True))

            if name in ("memory_save", "memory_search", "memory_list"):
                # Data-plane tools: they read/write the caller's own
                # memory rows, never the machine, so no confirm_action
                # gate applies (same risk class as chat-side "remember
                # this"). Every query is ownership-predicated below.
                memory = getattr(request.app.state, "memory", None)
                if principal is None or memory is None:
                    return _result(rpc_id, _tool_content(
                        "Memory store is not available on this server.",
                        is_error=True,
                    ))

                if name == "memory_save":
                    # The kill-switch gates CREATION only - search and
                    # list keep working so saved data is never trapped.
                    if not settings.memory_enabled():
                        return _result(rpc_id, _tool_content(
                            "Memory saving is disabled on this server "
                            "(INVINCIBLE_MEMORY is off).",
                            is_error=True,
                        ))
                    content = str(args.get("content") or "").strip()
                    if not content:
                        return _result(rpc_id, _tool_content(
                            "memory_save requires non-empty 'content'.",
                            is_error=True,
                        ))
                    if len(content) > MAX_CONTENT_CHARS:
                        return _result(rpc_id, _tool_content(
                            "Memory content must be at most "
                            f"{MAX_CONTENT_CHARS} characters.",
                            is_error=True,
                        ))
                    kind = str(args.get("kind") or "note")
                    if kind not in MEMORY_KINDS:
                        return _result(rpc_id, _tool_content(
                            "kind must be one of: "
                            + ", ".join(MEMORY_KINDS), is_error=True,
                        ))
                    project_id, project_error = await _mcp_project_id(
                        request, principal, args)
                    if project_error:
                        return _result(rpc_id, _tool_content(
                            project_error, is_error=True))
                    made_id = await memory.save_memory(
                        user_id=principal.user_id,
                        content=content,
                        layer="explicit",
                        kind=kind,
                        confidence=MCP_CONFIDENCE,
                        provenance=await _mcp_provenance(request),
                        project_id=project_id,
                    )
                    # Audit metadata only - never the content, which
                    # could carry secrets (same rule as _audit_action).
                    log = getattr(request.app.state, "audit_log", None)
                    if log is not None:
                        with contextlib.suppress(Exception):
                            await log.record(
                                "mcp.memory_save.saved",
                                actor_user_id=principal.user_id,
                                actor_kind="mcp",
                                resource_type="memory",
                                resource_id=str(made_id),
                            )
                    return _result(rpc_id, _tool_content(json.dumps({
                        "saved": True,
                        "id": made_id,
                        "kind": kind,
                        "scope": "project" if project_id is not None
                                 else "user",
                    })))

                if name == "memory_search":
                    retrieval = getattr(
                        request.app.state, "retrieval", None)
                    if retrieval is None:
                        return _result(rpc_id, _tool_content(
                            "Memory retrieval is not available on this "
                            "server.", is_error=True,
                        ))
                    query = str(args.get("query") or "").strip()
                    if not query:
                        return _result(rpc_id, _tool_content(
                            "memory_search requires non-empty 'query'.",
                            is_error=True,
                        ))
                    try:
                        limit = int(args.get("limit")
                                    or _MEMORY_SEARCH_DEFAULT)
                    except (TypeError, ValueError):
                        limit = _MEMORY_SEARCH_DEFAULT
                    limit = max(1, min(limit, _MEMORY_SEARCH_MAX))
                    project_id, project_error = await _mcp_project_id(
                        request, principal, args)
                    if project_error:
                        return _result(rpc_id, _tool_content(
                            project_error, is_error=True))
                    found = await retrieval.retrieve(
                        user_id=principal.user_id,
                        query=query,
                        project_id=project_id,
                        limit=limit,
                    )
                    return _result(rpc_id, _tool_content(json.dumps({
                        "results": [
                            {
                                "id": m.id,
                                "kind": m.kind,
                                "content": m.content,
                                "relevance": round(m.score, 4),
                                "created_at": m.created_at,
                            }
                            for m in found
                        ],
                        "count": len(found),
                    })))

                # memory_list: newest-first browse for session bootstrap.
                try:
                    limit = int(args.get("limit") or _MEMORY_LIST_DEFAULT)
                except (TypeError, ValueError):
                    limit = _MEMORY_LIST_DEFAULT
                limit = max(1, min(limit, _MEMORY_LIST_MAX))
                kind = args.get("kind")
                if kind is not None and kind not in MEMORY_KINDS:
                    return _result(rpc_id, _tool_content(
                        "kind must be one of: " + ", ".join(MEMORY_KINDS),
                        is_error=True,
                    ))
                project_id, project_error = await _mcp_project_id(
                    request, principal, args)
                if project_error:
                    return _result(rpc_id, _tool_content(
                        project_error, is_error=True))
                rows = await memory.list_for_user(
                    principal.user_id, kind=kind, project_id=project_id,
                    limit=limit,
                )
                return _result(rpc_id, _tool_content(json.dumps({
                    "memories": rows, "count": len(rows),
                })))

            return _error(rpc_id, -32601, f"Unknown tool: {name}")

        except tool_executor.ToolBlocked as e:
            return _result(rpc_id, _tool_content(f"Blocked: {e.reason}", is_error=True))

    return _error(rpc_id, -32601, f"Unknown method: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request,
                       principal: Principal = Depends(require_mcp_auth)):
    raw = await request.body()
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Can't recover an id from unparseable input - JSON-RPC 2.0 says
        # send id: null for parse errors.
        return JSONResponse(_error(None, -32700, "Parse error"))

    if not isinstance(body, dict):
        return JSONResponse(_error(None, -32600, "Invalid Request"))

    method = body.get("method")
    params = body.get("params") or {}
    is_notification = "id" not in body
    rpc_id = body.get("id")

    if not isinstance(method, str) or not method:
        # JSON-RPC 2.0: a request must carry a method name.
        if is_notification:
            return Response(status_code=204)
        return JSONResponse(_error(rpc_id, -32600, "Invalid Request"))

    if not isinstance(params, dict):
        if is_notification:
            # Notifications never get a response body, even on error.
            return Response(status_code=204)
        return JSONResponse(_error(rpc_id, -32602, "Invalid params"))

    response = await _dispatch(method, rpc_id, params, request,
                               principal=principal)

    if is_notification:
        # JSON-RPC 2.0: a request with no "id" is a notification - the
        # side effect (if any) still runs via _dispatch above, but the
        # spec says the server MUST NOT reply with a body.
        return Response(status_code=204)

    return JSONResponse(response)
