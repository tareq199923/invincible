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
import json
import logging

from invincible.compat.common import upstream_error_detail
from invincible.core import tool_executor
from invincible.core.chat_service import _persist_new_turns
from invincible.core.harness_policy import before_tool_call
from invincible.core.principal import Principal
from invincible.core.router import (
    AllProvidersFailedError,
    NoCredentialsConfiguredError,
    UpstreamClientError,
)
from invincible.core.settings import AGENT_JOB_GRACE_SECONDS

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
]

_TOOLS_BY_MODE = {
    "plan": [s for s in WEBCHAT_TOOL_SCHEMAS
             if s["function"]["name"] in READ_ONLY_TOOLS],
    "manual": list(WEBCHAT_TOOL_SCHEMAS),
    "auto": list(WEBCHAT_TOOL_SCHEMAS),
}

MODE_SYSTEM_PROMPTS = {
    "plan": (
        "You are helping plan work on the user's own machine. Produce a "
        "concrete step-by-step plan and stop. You have read-only "
        "inspection tools (read files, list directories, search code, "
        "git info, processes) - use them to ground the plan in reality. "
        "You cannot change anything: no mutating tools are available. "
        "Never claim an action was taken; end with the plan."
    ),
    "manual": (
        "You help operate the user's own machine. You have inspection "
        "tools plus execute_bash and write_file. Reads run immediately; "
        "each execute_bash/write_file call pauses for the user's "
        "explicit approval before running - call the tool, briefly say "
        "what will happen, and wait for the result to come back. If the "
        "user declines (or approval times out), respect it and offer an "
        "alternative. Keep commands least-privilege; never exfiltrate "
        "data off the machine."
    ),
    "auto": (
        "You help operate the user's own machine autonomously. You have "
        "inspection tools plus execute_bash and write_file, which run "
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
            elif fname not in READ_ONLY_TOOLS + MUTATING_TOOLS:
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
                    outcome=outcome,
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
