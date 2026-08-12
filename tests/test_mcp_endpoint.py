import ast
import os

from invincible.core import tool_executor

MCP_AUTH = {"X-MCP-Secret": "test-mcp-secret"}
TOOLS_LIST_REQUEST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


async def test_mcp_missing_secret_returns_401(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code == 401


async def test_mcp_wrong_secret_returns_401(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers={"X-MCP-Secret": "wrong"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401


async def test_mcp_disabled_when_secret_unset(client, monkeypatch):
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json=TOOLS_LIST_REQUEST,
    )
    assert response.status_code == 503


async def test_mcp_tools_list(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json=TOOLS_LIST_REQUEST,
    )
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert names == {"read_file", "execute_bash", "write_file", "confirm_action"}


async def test_mcp_call_blocked_command(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "execute_bash",
                "arguments": {"command": "sudo rm -rf /"},
            },
        },
    )
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Blocked" in body["result"]["content"][0]["text"]


def _pending_token(body):
    """Extract the token from a pending_confirmation result."""
    result = ast.literal_eval(body["result"]["content"][0]["text"])
    assert result["status"] == "pending_confirmation"
    return result["token"]


async def _call_tool(client, name, arguments):
    return await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


async def test_mcp_execute_bash_stages_pending_without_running(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    executed = []

    async def probe(command, timeout):
        executed.append(command)
        return {"stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tool_executor, "_run_command", probe)

    response = await _call_tool(
        client, "execute_bash", {"command": "echo hi"}
    )
    body = response.json()
    assert body["result"]["isError"] is False
    assert "pending_confirmation" in body["result"]["content"][0]["text"]
    _pending_token(body)  # a token was issued
    assert executed == []  # nothing ran until confirmed


async def test_mcp_execute_bash_approve_two_call_flow(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")

    staged = await _call_tool(client, "execute_bash", {"command": "echo hi"})
    token = _pending_token(staged.json())

    response = await _call_tool(client, "confirm_action", {
        "token": token, "approve": True,
    })
    body = response.json()
    assert body["result"]["isError"] is False
    assert "hi" in body["result"]["content"][0]["text"]


async def test_mcp_execute_bash_decline_two_call_flow(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    executed = []

    async def probe(command, timeout):
        executed.append(command)
        return {"stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tool_executor, "_run_command", probe)

    staged = await _call_tool(client, "execute_bash", {"command": "echo hi"})
    token = _pending_token(staged.json())

    response = await _call_tool(client, "confirm_action", {
        "token": token, "approve": False,
    })
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Declined" in body["result"]["content"][0]["text"]
    assert executed == []


async def test_mcp_confirm_action_unknown_token(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    executed = []

    async def probe(command, timeout):
        executed.append(command)
        return {"stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tool_executor, "_run_command", probe)

    response = await _call_tool(client, "confirm_action", {
        "token": "not-a-real-token", "approve": True,
    })
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Unknown or expired" in body["result"]["content"][0]["text"]
    assert executed == []


async def test_mcp_confirm_action_token_single_use(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    executed = []

    async def probe(command, timeout):
        executed.append(command)
        return {"stdout": "ok", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tool_executor, "_run_command", probe)

    staged = await _call_tool(client, "execute_bash", {"command": "echo hi"})
    token = _pending_token(staged.json())

    first = await _call_tool(client, "confirm_action", {
        "token": token, "approve": True,
    })
    second = await _call_tool(client, "confirm_action", {
        "token": token, "approve": True,
    })
    assert first.json()["result"]["isError"] is False
    assert second.json()["result"]["isError"] is True
    assert "Unknown or expired" in second.json()["result"]["content"][0]["text"]
    assert len(executed) == 1  # replay never double-executes


async def test_mcp_confirm_action_non_boolean_approve_is_denied(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    executed = []

    async def probe(command, timeout):
        executed.append(command)
        return {"stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tool_executor, "_run_command", probe)

    staged = await _call_tool(client, "execute_bash", {"command": "echo hi"})
    token = _pending_token(staged.json())

    response = await _call_tool(client, "confirm_action", {
        "token": token, "approve": "true",  # string, not a JSON boolean
    })
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Declined" in body["result"]["content"][0]["text"]
    assert executed == []


async def test_mcp_write_file_two_call_flow(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    target = tmp_path / "nested" / "out.txt"

    staged = await _call_tool(client, "write_file", {
        "path": str(target), "content": "hello from mcp",
    })
    assert "pending_confirmation" in staged.json()["result"]["content"][0]["text"]
    assert not target.exists()  # nothing written until confirmed
    token = _pending_token(staged.json())

    response = await _call_tool(client, "confirm_action", {
        "token": token, "approve": True,
    })
    body = response.json()
    assert body["result"]["isError"] is False
    assert target.read_text() == "hello from mcp"


async def test_mcp_unknown_tool(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "delete_everything", "arguments": {}},
        },
    )
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == -32601


async def test_mcp_call_read_file_success(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    target = tmp_path / "readable.txt"
    target.write_text("hello from disk")

    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": str(target)}},
        },
    )
    body = response.json()
    assert body["result"]["isError"] is False
    assert "hello from disk" in body["result"]["content"][0]["text"]


async def test_mcp_call_read_env_file_blocked(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    target = os.path.join(tool_executor._REPO_ROOT, ".env")

    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": target}},
        },
    )
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Blocked" in body["result"]["content"][0]["text"]


async def test_mcp_call_read_own_source_allowed(client, monkeypatch):
    """Unlike write_file, read_file must allow invincible/ and tests/ - seeing
    the code is the entire point of giving a cloud AI this tool."""
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    target = os.path.join(tool_executor._REPO_ROOT, "invincible", "main.py")

    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": target}},
        },
    )
    body = response.json()
    assert body["result"]["isError"] is False


async def test_mcp_call_write_to_protected_path_blocked_without_token(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")

    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "write_file",
                "arguments": {"path": ".env", "content": "GATEWAY_API_KEY=stolen"},
            },
        },
    )
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Blocked" in body["result"]["content"][0]["text"]
    assert len(app_pending_actions()) == 0  # never staged for approval


def app_pending_actions():
    from invincible.main import app

    return app.state.pending_actions


# --- JSON-RPC protocol hardening ---

async def test_mcp_malformed_json_returns_parse_error(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers={**MCP_AUTH, "Content-Type": "application/json"},
        content=b"{not valid json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == -32700


async def test_mcp_non_object_body_returns_invalid_request(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post("/mcp", headers=MCP_AUTH, json=[1, 2, 3])
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == -32600


async def test_mcp_missing_method_returns_invalid_request(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={"jsonrpc": "2.0", "id": 1},
    )
    body = response.json()
    assert body["id"] == 1
    assert body["error"]["code"] == -32600


async def test_mcp_non_string_method_returns_invalid_request(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={"jsonrpc": "2.0", "id": 1, "method": 123},
    )
    body = response.json()
    assert body["id"] == 1
    assert body["error"]["code"] == -32600


async def test_mcp_missing_method_notification_still_no_body(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={"jsonrpc": "2.0"},  # no "id" and no method -> notification
    )
    assert response.status_code == 204
    assert response.content == b""


async def test_mcp_invalid_params_returns_invalid_params(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1, 2]},
    )
    body = response.json()
    assert body["id"] == 1
    assert body["error"]["code"] == -32602


async def test_mcp_notification_returns_no_body(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={"jsonrpc": "2.0", "method": "tools/list"},  # no "id" -> notification
    )
    assert response.status_code == 204
    assert response.content == b""


async def test_mcp_notification_invalid_params_still_no_body(client, monkeypatch):
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-mcp-secret")
    response = await client.post(
        "/mcp",
        headers=MCP_AUTH,
        json={"jsonrpc": "2.0", "method": "tools/call", "params": [1, 2]},
    )
    assert response.status_code == 204
    assert response.content == b""