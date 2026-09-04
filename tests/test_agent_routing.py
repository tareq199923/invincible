import asyncio
import json

from invincible.main import app
from tests.conftest import obtain_access_token


async def _call(client, headers, name, arguments):
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


async def _pending_token(body):
    result = json.loads(body["result"]["content"][0]["text"])
    assert result["status"] == "pending_confirmation"
    return result["token"]


async def _fake_agent(user_id: int, registry):
    """Act as the user's agent: poll for one job and return it WITHOUT
    executing - the routing tests care about transport, not execution."""
    job = await asyncio.wait_for(registry.poll(user_id, hold=1), timeout=2)
    return job


async def test_routing_off_by_default_runs_locally(client, bearer_headers,
                                                    monkeypatch, tmp_path):
    """Default (INVINCIBLE_AGENT_ROUTING unset): confirm_action executes
    on the server, byte-identical to Phase 9."""
    probe = tmp_path / "ran.txt"
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "")

    staged = await _call(client, bearer_headers, "execute_bash",
                         {"command": f"echo routing-off > {probe}"})
    token = await _pending_token(staged.json())

    confirmed = await _call(client, bearer_headers, "confirm_action",
                            {"token": token, "approve": True})
    body = confirmed.json()
    assert body["result"]["isError"] is False
    assert probe.read_text().strip() == "routing-off"


async def test_routing_on_execute_bash_reaches_agent(client, monkeypatch,
                                                     tmp_path):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    tokens = await obtain_access_token(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    registry = app.state.agent_registry

    staged = await _call(client, headers, "execute_bash",
                         {"command": "echo hi"})
    token = await _pending_token(staged.json())

    # resolve the subject the same way require_mcp_auth does
    access = await app.state.oauth_store.validate_access(
        tokens["access_token"])
    subject = int(access["subject_user_id"])

    confirmed = asyncio.ensure_future(
        _call(client, headers, "confirm_action",
              {"token": token, "approve": True})
    )
    # the agent picks the job up mid-request
    job = await _fake_agent(subject, registry)
    assert job["type"] == "execute_bash"
    assert job["args"]["command"] == "echo hi"
    assert registry.submit_result(
        subject, job["job_id"], {"stdout": "hi", "stderr": "",
                                 "returncode": 0}
    )
    body = (await confirmed).json()
    assert body["result"]["isError"] is False
    assert "hi" in body["result"]["content"][0]["text"]


async def test_routing_on_agent_offline_is_immediate_error(client,
                                                            monkeypatch):
    """Nobody waits, nothing hangs: an offline agent answers
    agent_offline right away."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    tokens = await obtain_access_token(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    staged = await _call(client, headers, "execute_bash",
                         {"command": "echo hi"})
    token = await _pending_token(staged.json())

    confirmed = await _call(client, headers, "confirm_action",
                            {"token": token, "approve": True})
    body = confirmed.json()
    assert body["result"]["isError"] is True
    assert "agent" in body["result"]["content"][0]["text"].lower()


async def test_routing_on_read_file_reaches_agent(client, monkeypatch):
    """read_file has no confirm step - routing happens inside
    tools/call itself."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    tokens = await obtain_access_token(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    registry = app.state.agent_registry
    access = await app.state.oauth_store.validate_access(
        tokens["access_token"])
    subject = int(access["subject_user_id"])
    registry.heartbeat(subject)  # agent online: dispatch is attempted

    reading = asyncio.ensure_future(
        _call(client, headers, "read_file", {"path": "notes.txt"})
    )
    job = await asyncio.wait_for(registry.poll(subject, hold=1), timeout=2)
    assert job["type"] == "read_file"
    assert job["args"]["path"] == "notes.txt"
    registry.submit_result(
        subject, job["job_id"],
        {"status": "read", "path": "notes.txt", "content": "hello"},
    )
    body = (await reading).json()
    assert body["result"]["isError"] is False
    assert "hello" in body["result"]["content"][0]["text"]


async def test_routing_on_read_file_offline_agent_is_error(client,
                                                            monkeypatch):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    tokens = await obtain_access_token(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = await _call(client, headers, "read_file",
                           {"path": "notes.txt"})
    body = response.json()
    assert body["result"]["isError"] is True
    assert "agent" in body["result"]["content"][0]["text"].lower()


async def test_blocked_command_never_reaches_an_agent(client, monkeypatch):
    """Wall 1 stays on the server: a denylisted command is refused at
    staging - no token, no dispatch, even with routing on."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    tokens = await obtain_access_token(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = await _call(client, headers, "execute_bash",
                           {"command": "sudo rm -rf /"})
    body = response.json()
    assert body["result"]["isError"] is True
    assert "Blocked" in body["result"]["content"][0]["text"]
    # nothing was staged for anyone
    assert not app.state.agent_registry._queues


async def test_decline_still_short_circuits_before_dispatch(client,
                                                             monkeypatch):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    tokens = await obtain_access_token(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    staged = await _call(client, headers, "execute_bash",
                         {"command": "echo hi"})
    token = await _pending_token(staged.json())

    declined = await _call(client, headers, "confirm_action",
                           {"token": token, "approve": False})
    body = declined.json()
    assert body["result"]["isError"] is True
    assert "Declined" in body["result"]["content"][0]["text"]
    assert not app.state.agent_registry._queues
