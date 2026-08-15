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
"""
import json

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from invincible.core import tool_executor
from invincible.core.oauth_store import OAuthStore

router = APIRouter()

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


async def require_mcp_auth(request: Request):
    """Validate the Bearer access token from the built-in OAuth server."""
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
    if await store.validate_access(token) is None:
        raise _auth_error(request)


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_content(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


async def _dispatch(method, rpc_id, params, request):
    if method == "initialize":
        return _result(rpc_id, {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "invincible-mcp", "version": "0.1.0"},
            "capabilities": {"tools": {}},
        })

    if method == "tools/list":
        return _result(rpc_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        pending_actions = request.app.state.pending_actions

        try:
            if name == "read_file":
                result = await tool_executor.read_file(args.get("path", ""))
                return _result(rpc_id, _tool_content(str(result)))

            if name == "execute_bash":
                result = tool_executor.execute_bash(
                    args.get("command", ""), pending_actions
                )
                return _result(rpc_id, _tool_content(str(result)))

            if name == "write_file":
                result = tool_executor.write_file(
                    args.get("path", ""), args.get("content", ""), pending_actions
                )
                return _result(rpc_id, _tool_content(str(result)))

            if name == "confirm_action":
                # Only a real JSON boolean can approve - anything else
                # (absent, string, number) is treated as deny.
                approve = args.get("approve") is True
                result = await tool_executor.confirm_action(
                    pending_actions, args.get("token", ""), approve
                )
                status = result.get("status")
                if status == "not_found":
                    return _result(rpc_id, _tool_content(
                        "Unknown or expired confirmation token.", is_error=True
                    ))
                if status == "declined":
                    return _result(rpc_id, _tool_content("Declined.", is_error=True))
                return _result(rpc_id, _tool_content(str(result)))

            return _error(rpc_id, -32601, f"Unknown tool: {name}")

        except tool_executor.ToolBlocked as e:
            return _result(rpc_id, _tool_content(f"Blocked: {e.reason}", is_error=True))

    return _error(rpc_id, -32601, f"Unknown method: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request):
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

    response = await _dispatch(method, rpc_id, params, request)

    if is_notification:
        # JSON-RPC 2.0: a request with no "id" is a notification - the
        # side effect (if any) still runs via _dispatch above, but the
        # spec says the server MUST NOT reply with a body.
        return Response(status_code=204)

    return JSONResponse(response)
