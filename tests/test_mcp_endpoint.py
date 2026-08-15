import ast
import os

from invincible.core import tool_executor
from invincible.core.oauth_store import OAuthStore
from invincible.main import app
from tests.conftest import obtain_access_token

TOOLS_LIST_REQUEST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


# --- auth gate (Bearer tokens, WWW-Authenticate) ---


async def test_mcp_without_bearer_returns_401_with_challenge(client):
    response = await client.post("/mcp", json=TOOLS_LIST_REQUEST)
    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer")
    assert "/.well-known/oauth-protected-resource" in challenge


async def test_mcp_with_invalid_bearer_returns_401(client):
    response = await client.post(
        "/mcp",
        headers={"Authorization": "Bearer garbage"},
        json=TOOLS_LIST_REQUEST,
    )
    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert "oauth-protected-resource" in challenge


async def test_mcp_with_revoked_token_returns_401(client):
    tokens = await obtain_access_token(client)
    store: OAuthStore = app.state.oauth_store
    await store.revoke(tokens["access_token"])
    response = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json=TOOLS_LIST_REQUEST,
    )
    assert response.status_code == 401


async def test_mcp_tools_list(client, bearer_headers):
    response = await client.post(
        "/mcp", headers=bearer_headers, json=TOOLS_LIST_REQUEST
    )
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert names == {"read_file", "execute_bash", "write_file", "confirm_action"}


async def test_mcp_call_blocked_command(client, bearer_headers):
    response = await client.post(
        "/mcp",
        headers=bearer_headers,
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


async def _call_tool(client, headers, name, arguments):
    return await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


async def _call_bash(client, headers, command):
    return await _call_tool(
        client, headers, "execute_bash", {"command": command}
    )


async def _confirm(client, headers, token, approve):
    return await _call_tool(
        client, headers, "confirm_action",
        {"token": token, "approve": approve},
    )


async def test_mcp_execute_bash_stages_pending(client, bearer_headers, monkeypatch):
    executed = []

    async def probe(command, timeout):
        executed.append(command)
        return {"stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tool_executor, "_run_command", probe)

    response = await _call_bash(client, bearer_headers, "echo hi")
    body = response.json()
    assert body["result"]["isError"] is False
    assert "pending_confirmation" in body["result"]["content"][0]["text"]
    _pending_token(body)  # a token was issued
    assert executed == []  # nothing ran until confirmed


async def test_mcp_execute_bash_approve_flow(client, bearer_headers, monkeypatch):
    staged = await _call_bash(client, bearer_headers, "echo hi")
    token = _pending_token(staged.json())

    response = await _confirm(client, bearer_headers, token, True)
    body = response.json()
    assert body["result"]["isError"] is False
    assert "hi" in body["result"]["content"][0]["text"]


async def test_mcp_execute_bash_decline_flow(client, bearer_headers, monkeypatch):
    executed = []

    async def probe(command, timeout):
        executed.append(command)
        return {"stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tool_executor, "_run_command", probe)

    staged = await _call_bash(client, bearer_headers, "echo hi")
    token = _pending_token(staged.json())

    response = await _confirm(client, bearer_headers, token, False)
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Declined" in body["result"]["content"][0]["text"]
    assert executed == []


async def test_mcp_confirm_action_unknown_token(client, bearer_headers, monkeypatch):
    executed = []

    async def probe(command, timeout):
        executed.append(command)
        return {"stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tool_executor, "_run_command", probe)

    response = await _confirm(client, bearer_headers, "not-a-real-token", True)
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Unknown or expired" in body["result"]["content"][0]["text"]
    assert executed == []


async def test_mcp_confirm_action_token_single_use(client, bearer_headers, monkeypatch):
    executed = []

    async def probe(command, timeout):
        executed.append(command)
        return {"stdout": "ok", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tool_executor, "_run_command", probe)

    staged = await _call_bash(client, bearer_headers, "echo hi")
    token = _pending_token(staged.json())

    first = await _confirm(client, bearer_headers, token, True)
    second = await _confirm(client, bearer_headers, token, True)
    assert first.json()["result"]["isError"] is False
    assert second.json()["result"]["isError"] is True
    assert "Unknown or expired" in second.json()["result"]["content"][0]["text"]
    assert len(executed) == 1  # replay never double-executes


async def test_mcp_confirm_non_boolean_approve_denied(
    client, bearer_headers, monkeypatch
):
    executed = []

    async def probe(command, timeout):
        executed.append(command)
        return {"stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(tool_executor, "_run_command", probe)

    staged = await _call_bash(client, bearer_headers, "echo hi")
    token = _pending_token(staged.json())

    response = await _confirm(client, bearer_headers, token, "true")  # not a JSON bool
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Declined" in body["result"]["content"][0]["text"]
    assert executed == []


async def test_mcp_write_file_two_call_flow(client, bearer_headers, tmp_path):
    target = tmp_path / "nested" / "out.txt"

    staged = await _call_tool(client, bearer_headers, "write_file", {
        "path": str(target), "content": "hello from mcp",
    })
    assert "pending_confirmation" in staged.json()["result"]["content"][0]["text"]
    assert not target.exists()  # nothing written until confirmed
    token = _pending_token(staged.json())

    response = await _confirm(client, bearer_headers, token, True)
    body = response.json()
    assert body["result"]["isError"] is False
    assert target.read_text() == "hello from mcp"


async def test_mcp_unknown_tool(client, bearer_headers):
    response = await _call_tool(client, bearer_headers, "delete_everything", {})
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == -32601


async def test_mcp_call_read_file_success(client, bearer_headers, tmp_path):
    target = tmp_path / "readable.txt"
    target.write_text("hello from disk")

    response = await _call_tool(
        client, bearer_headers, "read_file", {"path": str(target)}
    )
    body = response.json()
    assert body["result"]["isError"] is False
    assert "hello from disk" in body["result"]["content"][0]["text"]


async def test_mcp_call_read_env_file_blocked(client, bearer_headers):
    target = os.path.join(tool_executor._REPO_ROOT, ".env")

    response = await _call_tool(client, bearer_headers, "read_file", {"path": target})
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Blocked" in body["result"]["content"][0]["text"]


async def test_mcp_call_read_own_source_allowed(client, bearer_headers):
    """Unlike write_file, read_file must allow invincible/ and tests/ - seeing
    the code is the entire point of giving a cloud AI this tool."""
    target = os.path.join(tool_executor._REPO_ROOT, "invincible", "main.py")

    response = await _call_tool(client, bearer_headers, "read_file", {"path": target})
    body = response.json()
    assert body["result"]["isError"] is False


async def test_mcp_write_to_protected_path_blocked(client, bearer_headers):
    response = await _call_tool(client, bearer_headers, "write_file", {
        "path": ".env", "content": "GATEWAY_API_KEY=stolen",
    })
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Blocked" in body["result"]["content"][0]["text"]
    assert len(app_pending_actions()) == 0  # never staged for approval


def app_pending_actions():
    from invincible.main import app

    return app.state.pending_actions


# --- JSON-RPC protocol hardening ---

async def test_mcp_malformed_json_returns_parse_error(client, bearer_headers):
    response = await client.post(
        "/mcp",
        headers={**bearer_headers, "Content-Type": "application/json"},
        content=b"{not valid json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == -32700


async def test_mcp_non_object_body_returns_invalid_request(client, bearer_headers):
    response = await client.post("/mcp", headers=bearer_headers, json=[1, 2, 3])
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == -32600


async def test_mcp_missing_method_returns_invalid_request(client, bearer_headers):
    response = await client.post(
        "/mcp", headers=bearer_headers, json={"jsonrpc": "2.0", "id": 1}
    )
    body = response.json()
    assert body["id"] == 1
    assert body["error"]["code"] == -32600


async def test_mcp_non_string_method_returns_invalid_request(client, bearer_headers):
    response = await client.post(
        "/mcp", headers=bearer_headers, json={"jsonrpc": "2.0", "id": 1, "method": 123}
    )
    body = response.json()
    assert body["id"] == 1
    assert body["error"]["code"] == -32600


async def test_mcp_missing_method_notification_still_no_body(client, bearer_headers):
    response = await client.post(
        "/mcp", headers=bearer_headers, json={"jsonrpc": "2.0"}
    )  # no "id" and no method -> notification
    assert response.status_code == 204
    assert response.content == b""


async def test_mcp_invalid_params_returns_invalid_params(client, bearer_headers):
    response = await client.post(
        "/mcp",
        headers=bearer_headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1, 2]},
    )
    body = response.json()
    assert body["id"] == 1
    assert body["error"]["code"] == -32602


async def test_mcp_notification_returns_no_body(client, bearer_headers):
    response = await client.post(
        "/mcp", headers=bearer_headers, json={"jsonrpc": "2.0", "method": "tools/list"}
    )  # no "id" -> notification
    assert response.status_code == 204
    assert response.content == b""


async def test_mcp_notification_invalid_params_still_no_body(client, bearer_headers):
    response = await client.post(
        "/mcp",
        headers=bearer_headers,
        json={"jsonrpc": "2.0", "method": "tools/call", "params": [1, 2]},
    )
    assert response.status_code == 204
    assert response.content == b""
