# tests/test_cli_agent.py
"""The agent loop (invincible agent -> runner.run_agent) driven
hermetically against the ASGI app, same injectable-client pattern as
device pairing tests.

Covers: a dispatched job travels poll -> local execution -> result and
resolves the waiting /mcp confirm request; wall-2 blocks become
results, not silent drops; a 401 (revoked key) stops the loop instead
of hot-looping denials; network errors back off and retry.
"""
import asyncio
import json

import httpx
import pytest

from invincible.agent import runner
from invincible.core.identity import ApiKeyStore
from invincible.main import app
from tests.conftest import obtain_access_token, register_account


async def _mint_key(client, email="agent-loop@example.com"):
    made, _ = await register_account(client, email=email)
    uid = int(made.json()["id"])
    record = await ApiKeyStore(app.state.engine).create(uid, label="agent")
    return uid, record["raw"]


async def _mcp_call(client, headers, name, arguments):
    return await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
    )


async def _pending_token(body):
    result = json.loads(body["result"]["content"][0]["text"])
    assert result["status"] == "pending_confirmation"
    return result["token"]


@pytest.fixture(autouse=True)
def agent_root(monkeypatch, tmp_path):
    """Keep local execution inside the test sandbox."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROOT", str(tmp_path))
    return tmp_path


async def test_agent_loop_end_to_end(client, monkeypatch, agent_root):
    """The full journey under routing: stage -> confirm -> the real
    agent loop picks the job up, executes it LOCALLY (sandboxed to the
    test root), posts the result, and the /mcp caller sees it."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    uid, key = await _mint_key(client)
    tokens = await obtain_access_token(client)  # MCP token acts as uid
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    target = agent_root / "loop-proof.txt"
    staged = await _mcp_call(client, headers, "execute_bash",
                             {"command":
                              f"echo agent-ran > {target}"})
    token = await _pending_token(staged.json())

    stop = asyncio.Event()
    loop_task = asyncio.ensure_future(
        runner.run_agent("http://test", key, client=client, stop=stop)
    )
    # Steady state first: let the loop complete its opening poll so the
    # agent is registered (online) before the confirm arrives - the
    # offline fast-fail is a separate test.
    await asyncio.sleep(0.2)
    confirmed = asyncio.ensure_future(
        _mcp_call(client, headers, "confirm_action",
                  {"token": token, "approve": True})
    )
    body = (await asyncio.wait_for(confirmed, timeout=10)).json()
    assert body["result"]["isError"] is False
    assert target.read_text().strip() == "agent-ran"

    stop.set()
    await asyncio.wait_for(loop_task, timeout=5)


async def test_wall2_block_becomes_result_not_crash(client, monkeypatch,
                                                    agent_root):
    """A command the server's denylist catches is never staged; a path
    the AGENT sandbox blocks (but the server can't know about - it's
    on the user's machine) executes as a 'blocked' result the AI sees."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    uid, key = await _mint_key(client)
    tokens = await obtain_access_token(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # write_file's server-side check only guards the server repo; a
    # user-home .ssh write passes it and lands on the agent, which
    # blocks it locally (wall 2/3).
    staged = await _mcp_call(client, headers, "write_file",
                             {"path": str(agent_root / ".ssh" /
                                          "authorized_keys"),
                              "content": "evil"})
    token = await _pending_token(staged.json())

    stop = asyncio.Event()
    loop_task = asyncio.ensure_future(
        runner.run_agent("http://test", key, client=client, stop=stop)
    )
    await asyncio.sleep(0.2)  # let the opening poll register the agent
    confirmed = asyncio.ensure_future(
        _mcp_call(client, headers, "confirm_action",
                  {"token": token, "approve": True})
    )
    body = (await asyncio.wait_for(confirmed, timeout=10)).json()
    assert body["result"]["isError"] is False  # a result, not a crash
    assert "blocked" in body["result"]["content"][0]["text"]
    assert not (agent_root / ".ssh").exists()

    stop.set()
    await asyncio.wait_for(loop_task, timeout=5)


async def test_revoked_key_stops_the_loop(client, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    uid, key = await _mint_key(client)
    await ApiKeyStore(app.state.engine).revoke(1)  # id 1 = the minted key

    stop = asyncio.Event()
    # short hold so the refusal comes fast
    import invincible.agent.runner as runner_mod
    monkeypatch.setattr(runner_mod, "AGENT_POLL_HOLD_SECONDS", 0.05)
    await asyncio.wait_for(
        runner.run_agent("http://test", key, client=client, stop=stop),
        timeout=10,
    )  # returns (does not raise) after the 401


async def test_network_errors_back_off_and_retry(client, monkeypatch):
    """A dead transport then a live one: the loop survives its first
    connection failure and keeps serving jobs."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    uid, key = await _mint_key(client)

    class FlakyTransport(httpx.ASGITransport):
        def __init__(self, app):
            super().__init__(app=app)
            self.fail_first = True

        async def handle_async_request(self, request):
            if self.fail_first:
                self.fail_first = False
                raise httpx.ConnectError("boom")
            return await super().handle_async_request(request)

    flaky = httpx.AsyncClient(transport=FlakyTransport(app=app),
                              base_url="http://test")
    monkeypatch.setattr(runner, "POLL_BACKOFF_SECONDS", 0.01)
    stop = asyncio.Event()
    loop_task = asyncio.ensure_future(
        runner.run_agent("http://test", key, client=flaky, stop=stop)
    )
    await asyncio.sleep(0.2)  # survives the failure + retry
    assert not loop_task.done()
    stop.set()
    await asyncio.wait_for(loop_task, timeout=5)
    await flaky.aclose()
