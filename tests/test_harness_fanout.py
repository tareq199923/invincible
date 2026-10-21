# tests/test_harness_fanout.py
"""F04 reframe: read-only parallel fan-out + main-loop trigger.

Provider upstreams are faked via httpx.MockTransport (endpoint tests)
or a stub router (direct ``run_fanout``/``run_step`` tests); the OS
boundary is faked by monkeypatching ``tool_executor`` helpers, so no
real subprocess or filesystem is ever touched.
"""
import json

from invincible.core import harness_fanout
from invincible.core.harness_bus import HarnessBus
from invincible.core.harness_events import HarnessEventType
from invincible.core.harness_router import AGENTS
from invincible.main import app
from tests.conftest import provider_body, register_account

# Pinned views: fan-out specialists are read-only by construction.
# Scout owns the explore (machine-read) set; librarian owns
# data-plane reads. A change here is a deliberate product decision:
# mutating, data-write, and agent-only tools must never fan out
# (manual-mode approvals are sequential browser gates; parallel
# mutating fan-out would multiply approval cards and race files).
GOLDEN_SCOUT_NAMES = (
    "read_file",
    "list_dir",
    "find_files",
    "code_search",
    "git_status",
    "git_diff",
    "git_log",
    "process_list",
)
GOLDEN_LIBRARIAN_NAMES = (
    "memory_search",
    "memory_list",
    "project_list",
    "task_state_get",
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


def text_body(text):
    return {
        "id": "cmpl-text",
        "model": "w1-model",
        "choices": [{
            "message": {"role": "assistant", "content": text}}],
    }


def plan_body(plan):
    return text_body(json.dumps(plan))


class StubRouter:
    """Minimal ``route_request_detailed`` stub serving scripted bodies."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.requests = []
        self.i = 0

    async def route_request_detailed(self, messages, tools=None,
                                     model=None, session_id=None,
                                     session_pk=None, **kwargs):
        # Yield every call so gathered steps truly interleave (a stub
        # with no internal await would run gather sequentially).
        import asyncio

        await asyncio.sleep(0)
        self.requests.append({
            "tools": [t["function"]["name"] for t in (tools or [])],
            "messages": messages,
        })
        body = self.bodies[min(self.i, len(self.bodies) - 1)]
        self.i += 1
        return body, {"request_id": "r", "provider_name": "p",
                      "model_id": "m", "attempts": 1}


def _kw(*, principal=None, mode="manual", router=None, executor=None):
    return {
        "router": router, "model": None, "session_id": "s", "session_pk": 1,
        "byok_kwargs": {}, "executor": executor,
        "data_ctx": {
            "principal": principal, "mode": mode, "memory": None,
            "retrieval": None, "continuity": None, "sessions": None,
            "engine": None,
        },
    }


# -- registry views --------------------------------------------------------

def test_fanout_agents_read_only_pinned():
    assert tuple(harness_fanout.SCOUT_AGENT.tools) == GOLDEN_SCOUT_NAMES
    assert tuple(harness_fanout.LIBRARIAN_AGENT.tools) == GOLDEN_LIBRARIAN_NAMES
    for banned in ("execute_bash", "write_file", "edit_file", "todo",
                   "memory_save", "task_state_set", "checkpoint_create",
                   "project_create", "confirm_action", "screenshot",
                   "web_fetch"):
        assert banned not in harness_fanout.SCOUT_AGENT.tools
        assert banned not in harness_fanout.LIBRARIAN_AGENT.tools
    assert set(harness_fanout.FANOUT_AGENTS) == {"scout", "librarian"}


def test_librarian_schemas_match_names_in_order():
    schemas = harness_fanout.librarian_schemas()
    assert [s["function"]["name"] for s in schemas] == list(
        GOLDEN_LIBRARIAN_NAMES)
    for schema in schemas:
        assert "required" in schema["function"]["parameters"]
    assert schemas is not harness_fanout.librarian_schemas()


def test_plane_of():
    assert harness_fanout.plane_of("read_file") == "machine"
    assert harness_fanout.plane_of("memory_search") == "data"
    assert harness_fanout.plane_of("task_state_get") == "data"
    assert harness_fanout.plane_of("web_fetch") == "agent"
    assert harness_fanout.plane_of("execute_bash") is None
    assert harness_fanout.plane_of("todo") is None
    assert harness_fanout.plane_of("nope") is None


# -- plan validation --------------------------------------------------------

def test_validate_plan_drops_bad_steps():
    plan = {"steps": [
        {"id": "a", "agent": "scout", "objective": "look"},
        {"id": "", "agent": "scout", "objective": "no id"},
        {"id": "b", "agent": "operator", "objective": "mutating family"},
        {"id": "c", "agent": "triage", "objective": "old registry name"},
        {"id": "d", "agent": "scout", "objective": ""},
        "not a dict",
    ]}
    assert harness_fanout.validate_plan(plan) == [
        {"id": "a", "agent": "scout", "objective": "look"}]
    assert harness_fanout.validate_plan({"nope": 1}) == []
    assert harness_fanout.validate_plan(None) == []
    # Registry param stays generic (v1 parity): validated against
    # whatever agent table the caller passes.
    assert harness_fanout.validate_plan(plan, AGENTS) == [
        {"id": "b", "agent": "operator", "objective": "mutating family"},
        {"id": "c", "agent": "triage", "objective": "old registry name"},
    ]


def test_parse_plan_text_strips_fences_and_tolerates_garbage():
    fenced = "```json\n" + json.dumps(
        {"steps": [{"id": "a", "agent": "scout",
                    "objective": "x"}]}) + "\n```"
    assert harness_fanout.parse_plan_text(fenced) == [
        {"id": "a", "agent": "scout", "objective": "x"}]
    assert harness_fanout.parse_plan_text("not json at all") == []
    assert harness_fanout.parse_plan_text("") == []


async def test_make_plan_advertises_valid_agents():
    async def complete(system, user):
        assert "scout" in user and "librarian" in user
        assert "JSON" in system
        return json.dumps({"steps": [
            {"id": "x", "agent": "librarian", "objective": "recall it"},
            {"id": "y", "agent": "ghost", "objective": "dropped"},
        ]})

    steps = await harness_fanout.make_plan("task", complete=complete)
    assert steps == [{"id": "x", "agent": "librarian",
                      "objective": "recall it"}]


# -- run_fanout direct ------------------------------------------------------

async def test_empty_plan_is_noop_costing_one_call(monkeypatch):
    async def fake_search(pattern, path, *args):
        raise AssertionError("no steps, no tools")

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "search_code", fake_search)
    router = StubRouter([plan_body({"steps": []})])
    out = await harness_fanout.run_fanout("t", **_kw(router=router))
    assert out == {"findings": "", "calls": 1, "steps": []}
    assert len(router.requests) == 1  # planner only
    assert router.requests[0]["tools"] == []


async def test_fanout_runs_steps_concurrently_and_merges(monkeypatch):
    async def fake_search(pattern, path, *args):
        return {"status": "ok", "hits": ["hit.py"],
                "secret": "secret-token-xyz"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "search_code", fake_search)
    bus = HarnessBus()
    plan = {"steps": [
        {"id": "a", "agent": "scout", "objective": "check X"},
        {"id": "b", "agent": "librarian", "objective": "recall Y"},
    ]}
    router = StubRouter([
        plan_body(plan),
        tool_body([fn_call("s1", "code_search",
                           {"pattern": "x", "path": "."})]),
        text_body("librarian findings"),
        text_body("scout findings"),
    ])
    out = await harness_fanout.run_fanout(
        "fix it", **_kw(router=router), bus=bus,
        workflow_id="wf1", user_id=42)
    assert out["calls"] == 4  # planner + scout round + scout text + librarian
    assert out["steps"] == plan["steps"]
    assert "[a via scout] scout findings" in out["findings"]
    assert "[b via librarian] librarian findings" in out["findings"]
    types = [e["type"] for e in bus.history()]
    # Parallel interleave (sequential would alternate start/complete).
    assert types == [
        HarnessEventType.PLAN_CREATED,
        HarnessEventType.SUBAGENT_STARTED,
        HarnessEventType.SUBAGENT_STARTED,
        HarnessEventType.SUBAGENT_COMPLETED,
        HarnessEventType.SUBAGENT_COMPLETED,
    ]
    assert HarnessEventType.WORKFLOW_STARTED not in types
    assert HarnessEventType.WORKFLOW_COMPLETED not in types
    # H6c: every durable event carries its owner for the dashboard read.
    assert all(event.get("user_id") == 42 for event in bus.history())
    # Previews bounded, tool-result secrets never on the bus.
    blob = json.dumps(bus.history())
    assert "secret-token-xyz" not in blob
    for event in bus.history():
        for key in ("findings_preview", "objective"):
            if key in event:
                assert len(str(event[key])) <= 200


async def test_fanout_degrades_on_partial_failure(monkeypatch):
    async def fake_search(pattern, path, *args):
        return {"status": "ok", "hits": []}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "search_code", fake_search)
    bus = HarnessBus()
    plan = {"steps": [
        {"id": "a", "agent": "scout", "objective": "ok"},
        {"id": "b", "agent": "scout", "objective": "boom"},
    ]}
    router = StubRouter([
        plan_body(plan),
        tool_body([fn_call("s1", "code_search",
                           {"pattern": "x", "path": "."})]),
        tool_body([fn_call("s2", "code_search",
                           {"pattern": "y", "path": "."})]),
        text_body("scout-a done"),
        {"id": "cmpl-bad", "model": "w1-model", "choices": []},
    ])
    out = await harness_fanout.run_fanout(
        "t", **_kw(router=router), bus=bus, workflow_id="wf2")
    assert "[a via scout] scout-a done" in out["findings"]
    assert "[b via" not in out["findings"]
    types = [e["type"] for e in bus.history()]
    assert HarnessEventType.SUBAGENT_FAILED in types
    assert types.count(HarnessEventType.SUBAGENT_COMPLETED) == 1


async def test_width_cap_drops_excess_with_log():
    bus = HarnessBus()
    plan = {"steps": [
        {"id": f"s{i}", "agent": "librarian", "objective": f"area {i}"}
        for i in range(6)
    ]}
    router = StubRouter([plan_body(plan), text_body("done")] * 10)
    out = await harness_fanout.run_fanout(
        "t", **_kw(router=router), bus=bus, workflow_id="wf3")
    assert len(out["steps"]) == harness_fanout.FANOUT_MAX_WIDTH
    assert out["calls"] == 1 + harness_fanout.FANOUT_MAX_WIDTH
    types = [e["type"] for e in bus.history()]
    assert HarnessEventType.LOG in types
    assert types.count(HarnessEventType.SUBAGENT_COMPLETED) == \
        harness_fanout.FANOUT_MAX_WIDTH


async def test_step_refuses_tool_outside_subset():
    seen = []

    async def recording_executor(name, args):
        seen.append(name)
        return {"status": "ok"}

    router = StubRouter([
        tool_body([fn_call("s1", "execute_bash", {"command": "rm -rf /"})]),
        text_body("could not act"),
    ])
    kw = _kw(router=router)
    kw["executor"] = recording_executor
    out = await harness_fanout.run_step(
        {"id": "a", "agent": "scout", "objective": "look"}, **kw)
    assert "execute_bash" not in seen  # never executed ...
    assert out["summary"] == "could not act"  # ... loop continues
    assert out["calls"] == 2


async def test_step_cap_ends_with_no_tools_call(monkeypatch):
    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    bodies = [tool_body([fn_call(f"s{i}", "read_file",
                                 {"path": f"f{i}.log"})])
              for i in range(harness_fanout.FANOUT_MAX_ITERATIONS + 1)]
    bodies.append(text_body("partial findings"))
    router = StubRouter(bodies)
    out = await harness_fanout.run_step(
        {"id": "a", "agent": "scout", "objective": "read all"},
        **_kw(router=router))
    assert out["summary"] == "partial findings"
    assert out["calls"] == harness_fanout.FANOUT_MAX_ITERATIONS + 2
    assert router.requests[-1]["tools"] == []  # tools withheld at cap


async def test_step_repeat_trip_withholds_tools(monkeypatch):
    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    same = {"path": "loop.log"}
    router = StubRouter(
        [tool_body([fn_call("s1", "read_file", same)])]
        * harness_fanout.FANOUT_REPEAT_LIMIT
        + [text_body("stuck, stop")])
    out = await harness_fanout.run_step(
        {"id": "a", "agent": "scout", "objective": "loop"},
        **_kw(router=router))
    assert out["summary"] == "stuck, stop"
    assert out["calls"] == harness_fanout.FANOUT_REPEAT_LIMIT + 1
    assert router.requests[-1]["tools"] == []


# -- main-loop trigger (endpoint) -------------------------------------------

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


async def test_trigger_fires_on_cross_plane_breadth(
    client, byok_env, router_setter, monkeypatch
):
    plan = {"steps": [
        {"id": "files", "agent": "scout", "objective": "find timeout code"},
        {"id": "past", "agent": "librarian", "objective": "recall context"},
    ]}
    script = [
        tool_body([fn_call("c1", "read_file", {"path": "a.txt"})]),
        tool_body([fn_call("c2", "memory_search", {"query": "timeout"})]),
        tool_body([fn_call("c3", "read_file", {"path": "b.txt"})]),
        tool_body([fn_call("c4", "memory_search", {"query": "retry"})]),
        tool_body([fn_call("c5", "read_file", {"path": "c.txt"})]),
        tool_body([fn_call("c6", "memory_search", {"query": "budget"})]),
        plan_body(plan),  # planner (tools=None)
        tool_body([fn_call("s1", "code_search",
                           {"pattern": "timeout", "path": "."})]),
        text_body("past context: none"),
        text_body("timeout lives in node_11.log line 60"),
        provider_body("w1", content="done: node_11.log line 60"),
    ]
    _bodies, handler = scripted(script)
    router_setter({"w1.example.com": handler})

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "log bytes"}

    async def fake_search(pattern, path, *args):
        return {"status": "ok", "hits": ["node_11.log"]}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    monkeypatch.setattr(te, "search_code", fake_search)
    await agent_user(client, "fanout-fire@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-fanout-fire", "message": "find the timeout",
        "mode": "manual"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    done = [d for n, d in events if n == "done"]
    assert len(done) == 1
    assert done[0]["tools_used"] == 6
    assert done[0]["explorer_calls"] == 0
    assert done[0]["fanout_calls"] == 4  # planner + scout x2 + librarian


async def test_trigger_quiet_below_threshold(
    client, byok_env, router_setter, monkeypatch
):
    script = [
        tool_body([fn_call("c1", "read_file", {"path": "a.txt"})]),
        tool_body([fn_call("c2", "memory_search", {"query": "x"})]),
        tool_body([fn_call("c3", "read_file", {"path": "b.txt"})]),
        tool_body([fn_call("c4", "memory_search", {"query": "y"})]),
        provider_body("w1", content="a and b say nothing"),
    ]
    _bodies, handler = scripted(script)
    router_setter({"w1.example.com": handler})

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    await agent_user(client, "fanout-quiet@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-fanout-quiet", "message": "peek",
        "mode": "manual"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    done = [d for n, d in events if n == "done"]
    assert done[0]["tools_used"] == 4
    assert done[0]["fanout_calls"] == 0
    assert done[0]["explorer_calls"] == 0


async def test_single_plane_spiral_stays_explore(
    client, byok_env, router_setter, monkeypatch
):
    script = [
        tool_body([fn_call("c1", "read_file", {"path": "a.txt"})]),
        tool_body([fn_call("c2", "read_file", {"path": "b.txt"})]),
        tool_body([fn_call("c3", "read_file", {"path": "c.txt"})]),
        tool_body([fn_call("c4", "read_file", {"path": "d.txt"})]),
        tool_body([fn_call("e1", "code_search",
                           {"pattern": "timeout", "path": "."})]),
        provider_body("w1", content="timeout lives in node_11.log line 60"),
        provider_body("w1", content="done: node_11.log line 60"),
    ]
    _bodies, handler = scripted(script)
    router_setter({"w1.example.com": handler})

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "log bytes"}

    async def fake_search(pattern, path, *args):
        return {"status": "ok", "hits": ["node_11.log"]}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    monkeypatch.setattr(te, "search_code", fake_search)
    await agent_user(client, "fanout-spiral@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-fanout-spiral", "message": "find the timeout",
        "mode": "manual"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    done = [d for n, d in events if n == "done"]
    assert len(done) == 1
    assert done[0]["explorer_calls"] == 2  # explore won the single flag ...
    assert done[0]["fanout_calls"] == 0  # ... fan-out never fired


async def test_trigger_fires_once_per_turn(
    client, byok_env, router_setter, monkeypatch
):
    plan = {"steps": [
        {"id": "files", "agent": "scout", "objective": "look"},
    ]}
    script = [
        tool_body([fn_call("c1", "read_file", {"path": "a.txt"})]),
        tool_body([fn_call("c2", "memory_search", {"query": "x"})]),
        tool_body([fn_call("c3", "read_file", {"path": "b.txt"})]),
        tool_body([fn_call("c4", "memory_search", {"query": "y"})]),
        tool_body([fn_call("c5", "read_file", {"path": "c.txt"})]),
        tool_body([fn_call("c6", "memory_search", {"query": "z"})]),
        plan_body(plan),
        text_body("scout says hi"),
    ]
    for i in range(4):
        script.append(
            tool_body([fn_call(f"d{i}", "read_file", {"path": f"g{i}.txt"})]))
        script.append(
            tool_body([fn_call(f"e{i}", "memory_search",
                               {"query": f"q{i}"})]))
    script += [provider_body("w1", content="final answer")]
    _bodies, handler = scripted(script)
    router_setter({"w1.example.com": handler})

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    await agent_user(client, "fanout-once@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-fanout-once", "message": "dig deep",
        "mode": "manual"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    done = [d for n, d in events if n == "done"]
    assert len(done) == 1
    assert done[0]["tools_used"] == 14
    assert done[0]["fanout_calls"] == 2  # planner + one text step, once


async def test_garbage_plan_is_noop_and_turn_completes(
    client, byok_env, router_setter, monkeypatch
):
    script = [
        tool_body([fn_call("c1", "read_file", {"path": "a.txt"})]),
        tool_body([fn_call("c2", "memory_search", {"query": "x"})]),
        tool_body([fn_call("c3", "read_file", {"path": "b.txt"})]),
        tool_body([fn_call("c4", "memory_search", {"query": "y"})]),
        tool_body([fn_call("c5", "read_file", {"path": "c.txt"})]),
        tool_body([fn_call("c6", "memory_search", {"query": "z"})]),
        provider_body("w1", content="not json at all"),
        provider_body("w1", content="main loop finished it"),
    ]
    _bodies, handler = scripted(script)
    router_setter({"w1.example.com": handler})

    async def fake_read(path, *args):
        return {"status": "read", "path": path, "content": "x"}

    import invincible.core.tool_executor as te

    monkeypatch.setattr(te, "read_file", fake_read)
    await agent_user(client, "fanout-garbage@example.com")
    resp = await client.post("/dashboard/chat/stream", json={
        "session_id": "web-fanout-garbage", "message": "search hard",
        "mode": "manual"})
    assert resp.status_code == 200, resp.text
    events = parse_web_events(resp.text)
    done = [d for n, d in events if n == "done"]
    assert len(done) == 1  # turn completes, never aborts
    assert done[0]["tools_used"] == 6
    assert done[0]["fanout_calls"] == 1  # planner only, empty plan no-op
