# tests/test_todo_tool.py
"""Step 4 (todo): the model tracks its own steps.

Single ``todo`` tool (add/list/complete/clear) backed by the existing
continuity store under the reserved ``task_key="todos"`` — no
migration, per-session/per-user isolation and restart survival
inherited from ``task_state_*``. Offered on MCP + webchat (all three
modes, including plan: plans are step lists).
"""
import json

import httpx
from sqlalchemy import text

from invincible.core.todos import (
    TODO_ACTIONS,
    TODO_MAX_ITEMS,
    TODO_MAX_TEXT_CHARS,
    TODO_TASK_KEY,
    blank_payload,
    normalize,
    run_todo,
)
from invincible.main import app
from tests.conftest import provider_body, register_account

# --- pure payload handling (hermetic: no Postgres) ---------------------------


def test_blank_and_normalize_shapes():
    assert blank_payload() == {"items": [], "next_id": 1}
    assert normalize(None) == {"items": [], "next_id": 1}
    assert normalize("garbage") == {"items": [], "next_id": 1}
    assert normalize({"items": "nope", "next_id": -3}) == {
        "items": [], "next_id": 1}
    # Foreign-shaped rows degrade: bad items dropped, done defaults.
    assert normalize({"items": [
        {"id": 7, "text": "keep me", "done": 1},
        {"text": "no id"},
        {"id": "x"},
        42,
    ], "next_id": 9}) == {
        "items": [{"id": "7", "text": "keep me", "done": False}],
        "next_id": 9,
    }


class FakeContinuity:
    """In-memory set_state/get_state honoring the ValueError contract."""

    def __init__(self):
        self.chains: dict[tuple, list] = {}

    async def get_state(self, session_id, task_key="default",
                        session_pk=None):
        chain = self.chains.get((session_pk, task_key), [])
        if not chain:
            return None
        head = chain[-1]
        return {**head, "session_id": session_id, "task_key": task_key}

    async def set_state(self, session_id, payload, *, actor,
                        task_key="default", status="active",
                        expected_version=None, session_pk=None):
        if session_pk is None:
            from invincible.core.scope import UnresolvedScopeError
            raise UnresolvedScopeError("no owner")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object (dict)")
        if len(json.dumps(payload)) > 4096:
            raise ValueError("payload exceeds 4096 chars")
        chain = self.chains.setdefault((session_pk, task_key), [])
        version = len(chain) + 1
        head = {"status": status, "payload": payload, "version": version}
        chain.append(head)
        return {"session_id": session_id, "task_key": task_key,
                "status": status, "payload": payload, "version": version}


async def test_run_todo_add_list_complete_clear():
    store = FakeContinuity()
    kw = {"session_id": "s", "session_pk": 1, "actor": "test"}

    result, ok = await run_todo(store, action="list", **kw)
    assert ok and result == {"items": [], "count": 0, "version": 0}

    result, ok = await run_todo(
        store, action="add", text="  first step  ", **kw)
    assert ok
    assert result["added"] == {"id": "1", "text": "first step",
                               "done": False}
    assert result["count"] == 1 and result["version"] == 1

    result, ok = await run_todo(store, action="add", text="second", **kw)
    assert ok and result["added"]["id"] == "2"

    result, ok = await run_todo(store, action="list", **kw)
    assert ok and result["count"] == 2 and result["version"] == 2
    assert [i["id"] for i in result["items"]] == ["1", "2"]

    result, ok = await run_todo(store, action="complete", todo_id="1",
                                **kw)
    assert ok
    assert result["completed"] == {"id": "1", "text": "first step",
                                   "done": True}
    assert result["items"][0]["done"] is True
    assert result["items"][1]["done"] is False

    result, ok = await run_todo(store, action="clear", **kw)
    assert ok and result == {"cleared": True, "count": 0, "version": 4}

    result, ok = await run_todo(store, action="list", **kw)
    assert ok and result["items"] == [] and result["version"] == 4

    # Ids are never reused within a session (next_id survives clear).
    result, ok = await run_todo(store, action="add", text="again", **kw)
    assert ok and result["added"]["id"] == "3"


async def test_run_todo_arg_errors_are_plain_dicts():
    store = FakeContinuity()
    kw = {"session_id": "s", "session_pk": 1, "actor": "test"}

    result, ok = await run_todo(store, action="frobnicate", **kw)
    assert not ok and "Valid actions" in result["error"]
    assert ", ".join(TODO_ACTIONS) in result["error"]

    result, ok = await run_todo(store, action="add", text="   ", **kw)
    assert not ok and "non-empty 'text'" in result["error"]

    result, ok = await run_todo(
        store, action="add", text="x" * (TODO_MAX_TEXT_CHARS + 1), **kw)
    assert not ok and "at most" in result["error"]

    result, ok = await run_todo(store, action="complete", todo_id="", **kw)
    assert not ok and "requires 'id'" in result["error"]

    result, ok = await run_todo(
        store, action="complete", todo_id="99", **kw)
    assert not ok and "unknown todo id" in result["error"]

    for i in range(TODO_MAX_ITEMS):
        result, ok = await run_todo(store, action="add", text=f"t{i}",
                                    **kw)
        assert ok, result
    result, ok = await run_todo(store, action="add", text="overflow",
                                **kw)
    assert not ok and "list is full" in result["error"]


async def test_run_todo_payload_cap_fails_loudly():
    """Worst case (20 max-length texts ≈ 4.9KB) exceeds the store's
    4096-char payload cap: the add fails with a clean error and the
    prior items survive untouched — never silent truncation."""
    store = FakeContinuity()
    kw = {"session_id": "s", "session_pk": 1, "actor": "test"}
    succeeded = 0
    for _ in range(TODO_MAX_ITEMS):
        result, ok = await run_todo(
            store, action="add", text="x" * TODO_MAX_TEXT_CHARS, **kw)
        if not ok:
            break
        succeeded += 1
    assert succeeded < TODO_MAX_ITEMS
    assert not ok and "exceeds 4096" in result["error"]
    result, ok = await run_todo(store, action="list", **kw)
    assert ok and result["count"] == succeeded
    assert all(i["text"] == "x" * TODO_MAX_TEXT_CHARS
               for i in result["items"])


async def test_run_todo_scope_edges():
    store = FakeContinuity()
    result, ok = await run_todo(None, action="list", session_id="s",
                                session_pk=1)
    assert not ok and "not initialized" in result["error"]

    # Unresolved scope: reads see nothing, writes fail closed.
    result, ok = await run_todo(store, action="list", session_id="s",
                                session_pk=None)
    assert ok and result["items"] == [] and result["version"] == 0
    result, ok = await run_todo(store, action="add", text="x",
                                session_id="s", session_pk=None)
    assert not ok and "resolve the session" in result["error"]


# --- registry tripwires (hermetic: fail loudly pre-handler) ------------------


def test_todo_registry_classification():
    from invincible.core import harness_tools

    tool = harness_tools.get_tool("todo")
    assert tool is not None
    assert tool.mcp and tool.webchat
    assert tool.webchat_modes == ("plan", "manual", "auto")
    assert tool.data_plane and not tool.read_only
    assert not tool.needs_approval
    assert tool.agent_job is None
    assert set(tool.router_agents) == {"triage", "operator"}
    assert tool.required == ("action",)

    assert "todo" in harness_tools.mcp_tool_names()
    assert "todo" in harness_tools.webchat_names_for_mode("plan")
    assert "todo" in harness_tools.webchat_names_for_mode("manual")
    assert "todo" in harness_tools.webchat_names_for_mode("auto")
    assert "todo" in harness_tools.data_write_names()
    assert "todo" in harness_tools.docs_data_plane_names()
    assert "todo" in harness_tools.router_tools("triage")
    assert "todo" in harness_tools.router_tools("operator")
    hint = harness_tools.expected_args_hint("todo")
    assert "action" in hint and "text" in hint and "required: action" in hint


def test_todo_policy_and_summary():
    from invincible.core.harness_policy import before_tool_call
    from invincible.core.webchat_agent import summarize_call

    assert before_tool_call("todo", {"action": "list"}) is None
    assert summarize_call("todo", {"action": "add"}) == "Todo add"
    assert TODO_TASK_KEY == "todos"


def test_environment_note_todo_guidance_in_all_modes():
    from invincible.core.webchat_agent import environment_note

    for mode in ("plan", "manual", "auto"):
        for execution in ("local", "agent"):
            note = environment_note(execution, mode=mode)
            assert "Track multi-step work with todo." in note, (
                mode, execution)


# --- live surfaces (real Postgres) -------------------------------------------


async def _mcp_call(client, headers, name, arguments, rpc_id=1):
    return await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": rpc_id,
              "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
    )


async def _mcp_token_for(client, email: str) -> str:
    from invincible.core.oauth_store import OAuthStore

    engine = app.state.engine
    async with engine.begin() as conn:
        uid = (await conn.execute(text(
            "INSERT INTO users (email, created_at)"
            " VALUES (:e, 1.0) RETURNING id"
        ), {"e": email})).scalar_one()
        await conn.execute(text(
            "INSERT INTO projects (user_id, name, is_default, created_at)"
            " VALUES (:u, 'personal', TRUE, 1.0)"
        ), {"u": uid})
        cid = (await conn.execute(text(
            "INSERT INTO oauth_clients (client_id, client_name,"
            " redirect_uris, owner_user_id, created_at)"
            " VALUES ('c-' || :e, 't', '[\"http://localhost:9/cb\"]',"
            " :u, 1.0) RETURNING client_id"
        ), {"e": email, "u": uid})).scalar_one()
    store = OAuthStore(engine=engine)
    pair = await store.issue_token_pair(cid, subject_user_id=int(uid))
    return pair["access_token"]


def _mcp_text(response) -> tuple[bool, str]:
    body = response.json()
    content = body["result"]["content"][0]
    return body["result"]["isError"], content["text"]


MCP_JSON = {"Content-Type": "application/json"}


async def test_mcp_todo_round_trip(client):
    token = await _mcp_token_for(client, "todo-mcp@example.com")
    headers = {**MCP_JSON, "Authorization": f"Bearer {token}"}

    is_err, text_out = _mcp_text(await _mcp_call(
        client, headers, "todo",
        {"action": "add", "text": "write tests",
         "session_id": "todo-work"}))
    assert not is_err, text_out
    assert json.loads(text_out)["added"]["text"] == "write tests"

    is_err, text_out = _mcp_text(await _mcp_call(
        client, headers, "todo",
        {"action": "list", "session_id": "todo-work"}))
    assert not is_err
    assert json.loads(text_out)["count"] == 1

    is_err, text_out = _mcp_text(await _mcp_call(
        client, headers, "todo",
        {"action": "complete", "id": "1",
         "session_id": "todo-work"}))
    assert not is_err
    assert json.loads(text_out)["completed"]["done"] is True

    is_err, text_out = _mcp_text(await _mcp_call(
        client, headers, "todo",
        {"action": "clear", "session_id": "todo-work"}))
    assert not is_err
    assert json.loads(text_out)["cleared"] is True

    is_err, text_out = _mcp_text(await _mcp_call(
        client, headers, "todo",
        {"action": "list", "session_id": "todo-work"}))
    assert not is_err and json.loads(text_out)["count"] == 0


async def test_mcp_todo_arg_error_carries_schema_echo(client):
    token = await _mcp_token_for(client, "todo-echo@example.com")
    headers = {**MCP_JSON, "Authorization": f"Bearer {token}"}
    is_err, text_out = _mcp_text(await _mcp_call(
        client, headers, "todo", {"action": "frobnicate"}))
    assert is_err
    assert "Valid actions" in text_out
    assert "Expected arguments for todo" in text_out


async def test_todos_are_invisible_across_principals(client):
    token_a = await _mcp_token_for(client, "todo-a@example.com")
    token_b = await _mcp_token_for(client, "todo-b@example.com")
    ha = {**MCP_JSON, "Authorization": f"Bearer {token_a}"}
    hb = {**MCP_JSON, "Authorization": f"Bearer {token_b}"}

    is_err, text_out = _mcp_text(await _mcp_call(
        client, ha, "todo",
        {"action": "add", "text": "A private step",
         "session_id": "shared-todos"}))
    assert not is_err, text_out

    # B lists the same client string: nothing tracked for them.
    is_err, text_out = _mcp_text(await _mcp_call(
        client, hb, "todo",
        {"action": "list", "session_id": "shared-todos"}))
    assert not is_err
    assert json.loads(text_out)["count"] == 0

    # B builds an independent list; A's stays untouched.
    await _mcp_call(client, hb, "todo",
                    {"action": "add", "text": "B step",
                     "session_id": "shared-todos"})
    is_err, text_out = _mcp_text(await _mcp_call(
        client, ha, "todo",
        {"action": "list", "session_id": "shared-todos"}))
    assert not is_err
    listed = json.loads(text_out)
    assert [i["text"] for i in listed["items"]] == ["A private step"]


async def test_todos_survive_continuity_restart_shape(client):
    """Restart survival is structural: todos ride the versioned
    ``todos`` chain, so a later read sees every prior head."""
    token = await _mcp_token_for(client, "todo-restart@example.com")
    headers = {**MCP_JSON, "Authorization": f"Bearer {token}"}
    for step in ("one", "two"):
        await _mcp_call(client, headers, "todo",
                        {"action": "add", "text": step,
                         "session_id": "todo-persist"})
    is_err, text_out = _mcp_text(await _mcp_call(
        client, headers, "todo",
        {"action": "list", "session_id": "todo-persist"}))
    assert not is_err
    assert json.loads(text_out)["version"] == 2


# --- webchat round-trip (plan mode offers + executes todo) -------------------


def _fn_call(call_id, name, args):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _tool_body(provider, calls):
    return {"id": "cmpl-tools", "model": f"{provider}-model",
            "choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": calls}}]}


def _scripted(responses):
    bodies = []
    state = {"i": 0}

    def handler(request):
        bodies.append(json.loads(request.content))
        resp = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return resp if isinstance(resp, httpx.Response) else \
            httpx.Response(200, json=resp)

    return bodies, handler


async def _webchat_user(client, email):
    registered, _ = await register_account(client, email)
    assert registered.status_code == 201, registered.text
    body = registered.json()
    from invincible.core.credential_store import ByokCredentialStore
    await ByokCredentialStore(app.state.engine).create(
        user_id=body["id"], provider_name="Web1", model_id="w1-model",
        base_url="https://w1.example.com/v1", api_key="web-key-1",
    )
    return body["id"], body["project_id"]


async def test_webchat_plan_mode_runs_todo(
    client, byok_env, router_setter
):
    bodies, handler = _scripted([
        _tool_body("w1", [_fn_call(
            "c1", "todo", {"action": "add", "text": "draft outline"})]),
        provider_body("w1", content="plan: draft, review, ship"),
    ])
    router_setter({"w1.example.com": handler})
    await _webchat_user(client, "todo-plan@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-todo-plan", "message": "plan the work",
        "mode": "plan"})
    assert resp.status_code == 200, resp.text
    offered = [t["function"]["name"]
               for body in bodies for t in body.get("tools") or []]
    assert "todo" in offered
    events = []
    for part in resp.text.split("\n\n"):
        part = part.strip()
        if not part:
            continue
        name, data = None, None
        for line in part.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        events.append((name, data))
    results = [d for n, d in events if n == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is True
    assert results[0]["name"] == "todo"
    done = [d for n, d in events if n == "done"]
    assert done and done[0]["mode"] == "plan"


async def test_webchat_todo_data_branch(client):
    """Direct ``_run_data_tool`` parity: same shapes as the MCP leg."""
    from invincible.core.principal import Principal
    from invincible.core.webchat_agent import _run_data_tool

    registered, _ = await register_account(client, "todo-direct@example.com")
    uid = registered.json()["id"]
    pid = registered.json()["project_id"]
    principal = Principal(user_id=uid, project_id=pid, kind="session")
    base = {"principal": principal, "mode": "manual", "memory": None,
            "retrieval": None, "continuity": app.state.continuity,
            "sessions": app.state.sessions, "engine": app.state.engine}

    result, ok = await _run_data_tool(
        "todo", {"action": "add", "text": "direct",
                 "session_id": "web-direct"}, **base)
    assert ok and result["added"]["id"] == "1"
    result, ok = await _run_data_tool(
        "todo", {"action": "list", "session_id": "web-direct"}, **base)
    assert ok and result["count"] == 1
    result, ok = await _run_data_tool(
        "todo", {"action": "bogus", "session_id": "web-direct"}, **base)
    assert not ok and "Expected arguments for todo" in result["error"]
