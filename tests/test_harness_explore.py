# tests/test_harness_explore.py
"""Step 8 explore sub-agent: bounded read-only search + main-loop delegation.

Provider upstreams are faked via httpx.MockTransport (endpoint tests)
or a stub router (direct ``run_explore`` tests); the OS boundary is
faked by monkeypatching ``tool_executor`` helpers, so no real
subprocess or filesystem is ever touched.
"""
import json

import pytest

from invincible.core import harness_explore, harness_tools
from invincible.core.router import UpstreamClientError
from invincible.main import app
from tests.conftest import provider_body, register_account

# Pinned view: the explorer's tool set is machine reads only, in
# webchat order. A change here is a deliberate product decision (Step 8:
# pressure comes from file spirals, not single lookups).
GOLDEN_EXPLORE_NAMES = (
    "read_file",
    "list_dir",
    "find_files",
    "code_search",
    "git_status",
    "git_diff",
    "git_log",
    "process_list",
)


def fn_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def tool_body(calls):
    return {
        "id": "cmpl-tools",
        "model": "w1-model",
        "choices": [{
            "message": {"role": "assistant", "content": None,
                        "tool_calls": calls},
        }],
    }


class StubRouter:
    """Minimal ``route_request_detailed`` stub serving scripted bodies."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.requests = []
        self.i = 0

    async def route_request_detailed(self, messages, tools=None,
                                     model=None, session_id=None,
                                     session_pk=None, **kwargs):
        self.requests.append({
            "tools": [t["function"]["name"] for t in (tools or [])],
        })
        body = self.bodies[min(self.i, len(self.bodies) - 1)]
        self.i += 1
        return body, {"request_id": "r", "provider_name": "p",
                      "model_id": "m", "attempts": 1}


def offered_is_explore(request):
    names = set(request["tools"])
    return names and names <= set(GOLDEN_EXPLORE_NAMES)


async def agent_user(client, email):
    registered, _ = await register_account(client, email)
    assert registered.status_code == 201, registered.text
    body = registered.json()
    from invincible.core.credential_store import ByokCredentialStore

    store = ByokCredentialStore(app.state.engine)
    await store.create(
        user_id=body["id"], provider_name="Web1", model_id="w1-model",
        base_url="https://w1.example.com/v1", api_key="web-key-1",
    )
    return body["id"], body["project_id"]


def parse_web_events(text):
    events = []
    for part in text.split("\n\n"):
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
    return events


# -- registry view --------------------------------------------------------

def test_explore_names_pinned():
    assert harness_tools.explore_names() == GOLDEN_EXPLORE_NAMES
    assert harness_tools.explore_names() == harness_tools.read_only_names()
    for banned in ("execute_bash", "write_file", "edit_file", "todo",
                   "memory_save", "memory_search", "screenshot",
                   "web_fetch", "task_state_set"):
        assert banned not in harness_tools.explore_names()


def test_explore_schemas_match_names_in_order():
    schemas = harness_tools.explore_schemas()
    assert [s["function"]["name"] for s in schemas] == list(
        GOLDEN_EXPLORE_NAMES)
    for schema in schemas:
        params = schema["function"]["parameters"]
        assert "required" in params  # webchat form always emits required
    assert schemas is not harness_tools.explore_schemas()  # fresh copies


# -- run_explore direct ----------------------------------------------------

async def test_explore_returns_summary_with_capped_words(monkeypatch):
    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    long_text = " ".join(f"w{i}" for i in range(500))
    router = StubRouter([
        tool_body([fn_call("e1", "code_search",
                           {"pattern": "timeout", "path": "."})]),
        tool_body([fn_call("e2", "read_file", {"path": "node_11.log"})]),
        {"id": "cmpl", "model": "w1-model", "choices": [{
            "message": {"role": "assistant", "content": long_text}}]},
    ])
    out = await harness_explore.run_explore(
        "which service timed out?", router=router, model=None,
        session_id="s", session_pk=1, byok_kwargs={}, executor=None)
    assert out["calls"] == 3
    assert len(out["summary"].split()) == harness_explore.EXPLORE_WORD_BUDGET
    # Offered tools are the explore set, never the full loop set.
    assert set(router.requests[0]["tools"]) == set(GOLDEN_EXPLORE_NAMES)


async def test_explore_rejects_mutating_tool_even_if_requested():
    seen = []

    async def recording_executor(name, args):
        seen.append(name)
        return {"status": "ok"}

    router = StubRouter([
        tool_body([fn_call("e1", "execute_bash", {"command": "rm -rf /"})]),
        {"id": "cmpl", "model": "w1-model", "choices": [{
            "message": {"role": "assistant",
                        "content": "could not search"}}]},
    ])
    out = await harness_explore.run_explore(
        "find things", router=router, model=None, session_id="s",
        session_pk=1, byok_kwargs={}, executor=recording_executor)
    assert "execute_bash" not in seen  # never executed ...
    assert out["summary"] == "could not search"  # ... loop continues


async def test_explore_cap_ends_with_no_tools_call(monkeypatch):
    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    bodies = [tool_body([fn_call(f"e{i}", "read_file",
                                 {"path": f"f{i}.log"})])
              for i in range(harness_explore.EXPLORE_MAX_ITERATIONS + 1)]
    bodies.append({"id": "cmpl", "model": "w1-model", "choices": [{
        "message": {"role": "assistant", "content": "partial findings"}}]})
    router = StubRouter(bodies)
    out = await harness_explore.run_explore(
        "read everything", router=router, model=None, session_id="s",
        session_pk=1, byok_kwargs={}, executor=None)
    assert out["summary"] == "partial findings"
    assert out["calls"] == harness_explore.EXPLORE_MAX_ITERATIONS + 2
    assert router.requests[-1]["tools"] == []  # tools withheld at cap


async def test_explore_repeat_trip_withholds_tools(monkeypatch):
    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    same = {"path": "loop.log"}
    router = StubRouter(
        [tool_body([fn_call("e1", "read_file", same)])]
        * harness_explore.EXPLORE_REPEAT_LIMIT
        + [{"id": "cmpl", "model": "w1-model", "choices": [{
            "message": {"role": "assistant", "content": "stuck, stop"}}]}])
    out = await harness_explore.run_explore(
        "loop", router=router, model=None, session_id="s",
        session_pk=1, byok_kwargs={}, executor=None)
    assert out["summary"] == "stuck, stop"
    assert out["calls"] == harness_explore.EXPLORE_REPEAT_LIMIT + 1
    assert router.requests[-1]["tools"] == []


async def test_explore_malformed_message_raises():
    router = StubRouter([{"id": "cmpl", "model": "w1-model",
                          "choices": []}])
    with pytest.raises(UpstreamClientError):
        await harness_explore.run_explore(
            "task", router=router, model=None, session_id="s",
            session_pk=1, byok_kwargs={}, executor=None)


# -- main-loop delegation (endpoint) ---------------------------------------

def scripted(responses):
    bodies = []
    state = {"i": 0}

    def handler(request):
        bodies.append(json.loads(request.content))
        resp = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        import httpx

        return resp if isinstance(resp, httpx.Response) else \
            httpx.Response(200, json=resp)

    return bodies, handler


def offered_names(body):
    return [t["function"]["name"] for t in body.get("tools") or []]


async def test_delegation_fires_after_threshold(
    client, byok_env, router_setter, monkeypatch
):
    bodies, handler = scripted([
        tool_body([fn_call("c1", "read_file", {"path": "a.txt"})]),
        tool_body([fn_call("c2", "read_file", {"path": "b.txt"})]),
        tool_body([fn_call("c3", "read_file", {"path": "c.txt"})]),
        tool_body([fn_call("c4", "read_file", {"path": "d.txt"})]),
        # Explorer's own loop (explore-subset tools only).
        tool_body([fn_call("e1", "code_search",
                           {"pattern": "timeout", "path": "."})]),
        provider_body("w1", content="timeout lives in node_11.log line 60"),
        provider_body("w1", content="done: node_11.log line 60"),
    ])
    router_setter({"w1.example.com": handler})

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "log bytes"}

    async def fake_search(pattern, path, *args):
        return {"status": "ok", "hits": ["node_11.log"]}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    monkeypatch.setattr(te, "search_code", fake_search)
    await agent_user(client, "explore-fire@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-explore-fire", "message": "find the timeout",
        "mode": "manual"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    done = [d for n, d in events if n == "done"]
    assert len(done) == 1
    assert done[0]["tools_used"] == 4
    assert done[0]["explorer_calls"] == 2
    explore_bodies = [b for b in bodies
                      if offered_names(b)
                      and set(offered_names(b)) <= set(GOLDEN_EXPLORE_NAMES)]
    assert len(explore_bodies) == 2
    assert "execute_bash" not in offered_names(explore_bodies[0])


async def test_no_delegation_below_threshold(
    client, byok_env, router_setter, monkeypatch
):
    bodies, handler = scripted([
        tool_body([fn_call("c1", "read_file", {"path": "a.txt"})]),
        tool_body([fn_call("c2", "read_file", {"path": "b.txt"})]),
        provider_body("w1", content="a and b say nothing"),
    ])
    router_setter({"w1.example.com": handler})

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    await agent_user(client, "explore-quiet@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-explore-quiet", "message": "peek",
        "mode": "manual"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    done = [d for n, d in events if n == "done"]
    assert done[0]["tools_used"] == 2
    assert done[0]["explorer_calls"] == 0


async def test_explorer_failure_falls_back_to_main_loop(
    client, byok_env, router_setter, monkeypatch
):
    bodies, handler = scripted([
        tool_body([fn_call("c1", "read_file", {"path": "a.txt"})]),
        tool_body([fn_call("c2", "read_file", {"path": "b.txt"})]),
        tool_body([fn_call("c3", "read_file", {"path": "c.txt"})]),
        tool_body([fn_call("c4", "read_file", {"path": "d.txt"})]),
        provider_body("w1", content="main loop finished it"),
    ])
    router_setter({"w1.example.com": handler})

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)

    async def boom(*args, **kwargs):
        raise RuntimeError("explorer down")

    import invincible.core.webchat_agent as wa

    monkeypatch.setattr(wa.harness_explore, "run_explore", boom)
    await agent_user(client, "explore-fallback@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-explore-fallback", "message": "search hard",
        "mode": "manual"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    done = [d for n, d in events if n == "done"]
    assert len(done) == 1  # turn completes, never aborts
    assert done[0]["tools_used"] == 4
    assert done[0]["explorer_calls"] == 0


async def test_delegation_fires_once_per_turn(
    client, byok_env, router_setter, monkeypatch
):
    # Order matters: one shared transport serves both loops. Main reads
    # 0-3 trip the trigger, the explorer consumes read+text, then four
    # more main reads prove no second delegation, then the final text.
    script = [tool_body([fn_call(f"c{i}", "read_file",
                                 {"path": f"f{i}.txt"})]) for i in range(4)]
    script += [
        tool_body([fn_call("e1", "read_file", {"path": "hit.txt"})]),
        provider_body("w1", content="explorer summary here"),
    ]
    script += [tool_body([fn_call(f"d{i}", "read_file",
                                  {"path": f"g{i}.txt"})]) for i in range(4)]
    script += [provider_body("w1", content="final answer")]
    bodies, handler = scripted(script)
    router_setter({"w1.example.com": handler})

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    await agent_user(client, "explore-once@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-explore-once", "message": "dig deep",
        "mode": "manual"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    done = [d for n, d in events if n == "done"]
    assert len(done) == 1
    assert done[0]["tools_used"] == 8
    explore_bodies = [b for b in bodies
                      if offered_names(b)
                      and set(offered_names(b)) <= set(GOLDEN_EXPLORE_NAMES)]
    assert len(explore_bodies) == 2  # exactly one explorer run
