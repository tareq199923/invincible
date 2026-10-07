# tests/test_web_fetch_tool.py
"""Step 4 (web_fetch): URL fetching on the user's paired machine.

Mirrors the screenshot precedent exactly: agent-routed execution only
(``server_executable=False``), the server never fetches caller URLs
(SSRF posture), no agent connected means an ``agent_offline`` error —
never a server-side fallback. Agent-side helper refuses non-http(s)
URLs, caps responses at 1MB, and times out like the other tools.
"""
import asyncio
import json

import httpx

from invincible.agent.runner import execute_job
from invincible.core import tool_executor
from invincible.main import app
from tests.conftest import obtain_access_token


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --- agent-side helper (hermetic: MockTransport, no network) -----------------


async def test_fetch_refuses_non_http():
    for bad in ("file:///etc/passwd", "gopher://example.com/x", "",
                "  ftp://example.com"):
        out = await tool_executor._fetch_url(bad, 5.0)
        assert out["status"] == "error", bad
        assert "http" in out["error"], bad


async def test_fetch_success_shape():
    transport = _transport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"},
            content=b"<title>hi</title>"))
    out = await tool_executor._fetch_url(
        "https://example.com/", 5.0, transport=transport)
    assert out["status"] == "web_fetch"
    assert out["content_text"] == "<title>hi</title>"
    assert out["content_type"] == "text/html"
    assert out["bytes"] == len(b"<title>hi</title>")
    assert out["url"] == "https://example.com/"


async def test_fetch_non_2xx_is_error():
    transport = _transport(
        lambda request: httpx.Response(404, content=b"nope"))
    out = await tool_executor._fetch_url(
        "https://example.com/missing", 5.0, transport=transport)
    assert out["status"] == "error"
    assert "404" in out["error"]


async def test_fetch_byte_cap_refuses():
    big = b"x" * (tool_executor.WEB_FETCH_MAX_BYTES + 256)
    transport = _transport(
        lambda request: httpx.Response(200, content=big))
    out = await tool_executor._fetch_url(
        "https://example.com/big", 5.0, transport=transport)
    assert out["status"] == "error"
    assert "too large" in out["error"]
    assert str(tool_executor.WEB_FETCH_MAX_BYTES) in out["error"]


async def test_fetch_timeout_is_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    out = await tool_executor._fetch_url(
        "https://example.com/slow", 0.1,
        transport=_transport(handler))
    assert out["status"] == "error"
    assert "timed out" in out["error"]


async def test_runner_web_fetch_refusal_without_network():
    out = await execute_job({
        "job_id": "wf1", "type": "web_fetch",
        "args": {"url": "not-a-url"}})
    assert out["status"] == "error"
    assert "http" in out["error"]


# --- registry tripwires (fail loudly pre-handler) ----------------------------


def test_web_fetch_registry_classification():
    from invincible.core import harness_tools

    tool = harness_tools.get_tool("web_fetch")
    assert tool is not None
    assert tool.mcp and tool.webchat
    assert tool.webchat_modes == ("plan", "manual", "auto")
    assert tool.read_only and not tool.needs_approval
    assert not tool.server_executable
    assert tool.agent_job == "web_fetch"
    assert not tool.data_plane
    assert tool.required == ("url",)

    assert "web_fetch" in harness_tools.mcp_tool_names()
    assert "web_fetch" in harness_tools.webchat_names_for_mode("plan")
    assert "web_fetch" in harness_tools.webchat_names_for_mode("manual")
    assert "web_fetch" in harness_tools.webchat_names_for_mode("auto")
    assert "web_fetch" in harness_tools.agent_only_names()
    assert "web_fetch" in harness_tools.agent_job_names()
    # Agent-only reads are not server reads (screenshot precedent:
    # read_only_names() requires server_executable).
    assert "web_fetch" not in harness_tools.read_only_names()
    hint = harness_tools.expected_args_hint("web_fetch")
    assert "url" in hint and "required: url" in hint


def test_web_fetch_policy_and_summary():
    from invincible.core.harness_policy import before_tool_call
    from invincible.core.webchat_agent import summarize_call

    assert before_tool_call(
        "web_fetch", {"url": "https://example.com"}) is None
    assert summarize_call(
        "web_fetch", {"url": "https://example.com/x"}) == \
        "Fetch https://example.com/x"


async def test_runner_unknown_job_lists_web_fetch():
    from invincible.core import harness_tools

    out = await execute_job({"type": "frobnicate_xyz", "args": {}})
    assert out["status"] == "error"
    assert "web_fetch" in out["error"]
    assert "screenshot" in out["error"]
    assert "todo" not in harness_tools.agent_job_names()


# --- routing parity with screenshot (live MCP surface) -----------------------


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


async def test_mcp_web_fetch_unavailable_without_routing(
        client, bearer_headers):
    """Default mode (no agent routing): agent-only tool reports
    unavailability — the server never fetches caller URLs."""
    result = await _call(client, bearer_headers, "web_fetch",
                         {"url": "http://127.0.0.1:9/"})
    assert result.status_code == 200
    body = result.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["status"] == "unavailable"
    assert "invincible harness connect" in payload["reason"]
    assert "INVINCIBLE_AGENT_ROUTING=1" in payload["reason"]


async def test_mcp_web_fetch_offline_matches_screenshot(client, monkeypatch):
    """Routing on, no agent: web_fetch answers agent_offline with the
    same shape screenshot does (isError + connect hint)."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    tokens = await obtain_access_token(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    for name in ("web_fetch", "screenshot"):
        result = await _call(client, headers, name,
                             {"url": "https://example.com/"})
        body = result.json()
        assert body["result"]["isError"] is True, name
        text = body["result"]["content"][0]["text"]
        assert "invincible harness connect" in text, name


async def test_mcp_web_fetch_reaches_agent(client, monkeypatch):
    """Routing on, agent online: the job travels to the paired agent
    and its result resolves the /mcp call (read_file parity)."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    tokens = await obtain_access_token(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    registry = app.state.agent_registry
    access = await app.state.oauth_store.validate_access(
        tokens["access_token"])
    subject = int(access["subject_user_id"])
    registry.heartbeat(subject)

    fetching = asyncio.ensure_future(
        _call(client, headers, "web_fetch",
              {"url": "https://example.com/"})
    )
    job = await asyncio.wait_for(registry.poll(subject, hold=1), timeout=2)
    assert job["type"] == "web_fetch"
    assert job["args"]["url"] == "https://example.com/"
    registry.submit_result(
        subject, job["job_id"],
        {"status": "web_fetch", "content_text": "hello world",
         "content_type": "text/plain", "bytes": 11,
         "url": "https://example.com/"},
    )
    body = (await fetching).json()
    assert body["result"]["isError"] is False
    assert "hello world" in body["result"]["content"][0]["text"]


async def test_webchat_web_fetch_never_fetches_locally():
    """Webchat leg mirrors the MCP leg: without routing it reports
    unavailability (never a server-side fetch); with an executor the
    job travels under the web_fetch type."""
    from invincible.core.webchat_agent import _run_web_fetch

    out = await _run_web_fetch(None, {"url": "https://example.com/"})
    assert out["status"] == "unavailable"
    assert "invincible harness connect" in out["reason"]

    seen = []

    async def fake_executor(job_type, args):
        seen.append((job_type, args))
        return {"status": "web_fetch", "content_text": "hi",
                "content_type": "text/plain", "bytes": 2,
                "url": args["url"]}

    out = await _run_web_fetch(
        fake_executor, {"url": "https://example.com/"})
    assert out["status"] == "web_fetch"
    assert seen == [("web_fetch", {"url": "https://example.com/"})]
