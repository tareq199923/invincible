# tests/test_isolation.py
"""Phase 2 ACCEPTANCE: user A cannot access any user B resource through
ANY surface - including enumeration attempts.

Resource types covered here (each via its real surface, not store
internals):

- sessions/graph  : GET /api/v1/sessions/{id}/graph under another
                    principal reads as nonexistent ("known": false,
                    empty projection); admin override still works;
- task states     : MCP task_state_set by A, task_state_get/history by B
                    -> empty result; independent chains under one string;
- checkpoints     : created by A invisible to B's projections;
- runs            : attempts recorded for A's request carry A's ownership;
                    B's graph shows none of them;
- facts           : extracted from A's chat never injected into B's
                    outgoing context (and vice versa);
- approvals       : execute_bash staged by A cannot be confirmed by B
                    (unknown-token semantics), then A confirms fine;
- agent dispatch  : A's confirmed job never reaches B's /agent/poll and
                    B's forged /agent/result is rejected with responses
                    indistinguishable from unknown/timed-out job ids
                    (audit Step 5, item 3);
- BYOK routing    : A's chat goes out through A's connected credential
                    host only - B's host and the operator pool are
                    provably never called, including while A's own
                    credential sits in cooldown (audit Step 5, item 4).

Enumeration: sequential id probing by B yields byte-identical negative
shapes regardless of whether the string exists for someone else.
"""
import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from invincible.core.credential_store import ByokCredentialStore
from invincible.main import app

GATEWAY = {"Authorization": "Bearer test-gateway-key"}


@pytest.fixture(autouse=True)
def _byok_env(monkeypatch):
    """Phase 9: keyed principals chat only through their own connected
    credentials. Provide a usable master key and hermetic DNS for the
    per-attempt URL re-check."""
    import invincible.core.url_safety as url_safety

    monkeypatch.setenv(
        "INVINCIBLE_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(
        url_safety, "_default_resolve", lambda host: ["93.184.216.34"])


def provider_body(content="ok"):
    return {
        "id": "cmpl-x",
        "model": "alpha-model",
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }


async def _mint_user_and_key(client, email: str, host: str = "alpha.example.com",
                             model_id: str = "alpha-model") -> dict:
    """Real user row + default project + one API key + one connected BYOK
    credential (Phase 9: keyed principals chat only through their own
    connected providers). ``host``/``model_id`` place the credential on a
    chosen mock provider so cross-user routing tests can tell the pools
    apart. Returns the key record plus the resolved ids."""
    engine = app.state.engine
    async with engine.begin() as conn:
        uid = (await conn.execute(text(
            "INSERT INTO users (email, created_at)"
            " VALUES (:e, 1.0) RETURNING id"
        ), {"e": email})).scalar_one()
        pid = (await conn.execute(text(
            "INSERT INTO projects (user_id, name, is_default, created_at)"
            " VALUES (:u, 'personal', TRUE, 1.0) RETURNING id"
        ), {"u": uid})).scalar_one()
    row = await ByokCredentialStore(engine).create(
        user_id=int(uid), provider_name=f"Cred {host.split('.')[0]}",
        model_id=model_id,
        base_url=f"https://{host}/v1",
        api_key="user-key",
    )
    record = await app.state.api_keys.create(int(uid))
    return {"user_id": int(uid), "project_id": int(pid),
            "credential_id": int(row["id"]), "raw": record["raw"]}


def auth_for(key_raw: str) -> dict:
    return {"Authorization": f"Bearer {key_raw}"}


async def _chat(client, headers, session_id, message):
    return await client.post(
        "/v1/chat/completions",
        headers={**headers, "X-Session-Id": session_id},
        json={"messages": [{"role": "user", "content": message}]},
    )


# --- fixtures ------------------------------------------------------------------


@pytest.fixture
def alpha_handler(router_setter):
    """One healthy provider capturing every outgoing payload."""
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=provider_body())

    router_setter({"alpha.example.com": handler})
    return captured


# --- sessions / graph ------------------------------------------------------------


async def test_graph_hides_other_users_session(client, alpha_handler):
    a = await _mint_user_and_key(client, "a@example.com")
    b = await _mint_user_and_key(client, "b@example.com")

    await _chat(client, auth_for(a["raw"]), "a-secret", "hi")
    resp = await client.get(
        "/api/v1/sessions/a-secret/graph", headers=auth_for(a["raw"])
    )
    assert resp.json()["known"] is True

    foreign = await client.get(
        "/api/v1/sessions/a-secret/graph", headers=auth_for(b["raw"])
    )
    assert foreign.status_code == 200  # authenticated, but...
    body = foreign.json()
    assert body["known"] is False      # ...indistinguishable from missing
    assert body["nodes"] == []
    assert body["summary"]["turns"] == 0


async def test_graph_enumeration_probes_leak_nothing(client, alpha_handler):
    b = await _mint_user_and_key(client, "enum@example.com")
    # One REAL session belonging to user A.
    a = await _mint_user_and_key(client, "owner@example.com")
    await _chat(client, auth_for(a["raw"]), "target-session", "hi")

    shapes = set()
    for probe in ("target-session", "enum-0", "enum-1",
                  "target-session ", "TARGET-SESSION"):
        resp = await client.get(
            f"/api/v1/sessions/{probe.strip()}/graph",
            headers=auth_for(b["raw"]),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["known"] is False and data["nodes"] == []
        shapes.add(json.dumps({"known": data["known"],
                               "n_nodes": len(data["nodes"])},
                              sort_keys=True))
    assert len(shapes) == 1  # identical negative shape for every probe


async def test_graph_admin_override_still_reads_any_session(
    client, alpha_handler
):
    a = await _mint_user_and_key(client, "visible@example.com")
    await _chat(client, auth_for(a["raw"]), "admin-visible", "hi")

    from tests.conftest import operator_session, promote_operator

    uid = await operator_session(client, email="override-op@example.com")
    # Raw-SQL users above bypassed the first-human bootstrap, so this
    # account registered as plain; reach for the row directly.
    await promote_operator(uid)
    resp = await client.get("/api/v1/sessions/admin-visible/graph")
    assert resp.status_code == 200
    assert resp.json()["known"] is True


# --- task states / checkpoints (MCP surface) --------------------------------------


async def _mcp_call(client, headers, name, arguments, rpc_id=1):
    return await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": rpc_id,
              "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
    )


async def _mcp_token_for(client, email: str) -> str:
    """A valid OAuth access token whose subject is a brand-new user."""
    import json as _json

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
    del _json
    return pair["access_token"]


MCP_JSON = {"Content-Type": "application/json"}


async def test_task_states_are_invisible_across_principals(client):
    token_a = await _mcp_token_for(client, "mcp-a@example.com")
    token_b = await _mcp_token_for(client, "mcp-b@example.com")
    ha = {**MCP_JSON, "Authorization": f"Bearer {token_a}"}
    hb = {**MCP_JSON, "Authorization": f"Bearer {token_b}"}

    # A tracks progress under a shared-looking client string.
    r = await _mcp_call(client, ha, "task_state_set", {
        "payload": '{"next_value": 6}', "task_key": "count",
        "session_id": "shared-work",
    })
    assert r.status_code == 200
    head = _json_loads(r)
    assert head["version"] == 1

    # B reads the SAME string/key: no state exists for them.
    rb = await _mcp_call(client, hb, "task_state_get", {
        "task_key": "count", "session_id": "shared-work",
    })
    payload = _json_loads(rb)
    assert payload["version"] == 0
    assert payload["payload"] is None

    # B writing the same string/key starts its OWN chain at v1...
    rb2 = await _mcp_call(client, hb, "task_state_set", {
        "payload": '{"next_value": 100}', "task_key": "count",
        "session_id": "shared-work",
    })
    assert _json_loads(rb2)["version"] == 1

    # ...while A still sees its own chain untouched at the next write (v2).
    ra2 = await _mcp_call(client, ha, "task_state_set", {
        "payload": '{"next_value": 7}', "task_key": "count",
        "session_id": "shared-work",
    })
    assert _json_loads(ra2)["version"] == 2


async def test_checkpoints_do_not_leak_across_principals(client):
    token_a = await _mcp_token_for(client, "cp-a@example.com")
    ha = {**MCP_JSON, "Authorization": f"Bearer {token_a}"}
    # B exists with their own project (token unused; only identity matters)
    await _mcp_token_for(client, "cp-b@example.com")

    await _mcp_call(client, ha, "checkpoint_create", {
        "note": "through 5", "session_id": "cp-shared",
    })

    # B's projection over their own (empty) same-string session has no
    # checkpoint nodes; the admin/operator view of B's session neither.
    # Direct engine read scoped by pk proves the row is A-owned.
    engine = app.state.continuity
    sessions = app.state.sessions
    uid_b, pid_b = await _user_ids_by_email("cp-b@example.com")
    pk_b = await sessions.lookup("cp-shared", user_id=uid_b,
                                 project_id=pid_b)
    if pk_b is None:
        cps = []
    else:
        cps = await engine.checkpoints("cp-shared", session_pk=pk_b)
    assert cps == []


async def _user_ids_by_email(email: str) -> tuple[int, int]:
    async with app.state.engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT u.id, p.id FROM users u"
            " JOIN projects p ON p.user_id = u.id"
            " WHERE u.email = :e AND p.is_default"
        ), {"e": email})).first()
    assert row is not None, email
    return int(row[0]), int(row[1])


# --- runs -------------------------------------------------------------------------


async def test_runs_carry_ownership_and_stay_scoped(client, alpha_handler):
    a = await _mint_user_and_key(client, "runs-a@example.com")
    b = await _mint_user_and_key(client, "runs-b@example.com")

    # Wire run recording the way the lifespan does (the client fixture
    # builds routers without it).
    app.state.router.run_recorder = app.state.runs.record

    await _chat(client, auth_for(a["raw"]), "run-sess", "hello")

    # A's run rows are stamped with A's surrogate session.
    pk_a = await app.state.sessions.lookup(
        "run-sess", user_id=a["user_id"], project_id=a["project_id"])
    assert pk_a is not None
    owned = await app.state.runs.recent(session_pk=pk_a)
    assert owned and all(r["session_pk"] == pk_a for r in owned)

    # B never had this session: nothing to see at any scope.
    assert await app.state.sessions.lookup(
        "run-sess", user_id=b["user_id"], project_id=b["project_id"]
    ) is None


# --- facts ------------------------------------------------------------------------


async def test_facts_extracted_for_a_never_reach_b(client, alpha_handler):
    a = await _mint_user_and_key(client, "fact-a@example.com")
    b = await _mint_user_and_key(client, "fact-b@example.com")
    secret = "the launch code is 31337"

    await _chat(client, auth_for(a["raw"]), "fact-sess",
                f"remember that {secret}")

    # A's own follow-up carries the injected memory line upstream...
    await _chat(client, auth_for(a["raw"]), "fact-sess", "continue")
    assert any(secret in json.dumps(payload.get("messages", []))
               for payload in alpha_handler), \
        "A's own context should include A's fact"

    # ...B's context on the same string must not contain it.
    alpha_handler.clear()
    await _chat(client, auth_for(b["raw"]), "fact-sess", "continue")
    for payload in alpha_handler:
        assert secret not in json.dumps(payload.get("messages", []))


# --- staged-action approvals --------------------------------------------------------


async def test_approval_requires_the_staging_subject(client):
    token_a = await _mcp_token_for(client, "appr-a@example.com")
    token_b = await _mcp_token_for(client, "appr-b@example.com")
    ha = {**MCP_JSON, "Authorization": f"Bearer {token_a}"}
    hb = {**MCP_JSON, "Authorization": f"Bearer {token_b}"}
    executed = []

    async def fake_run(command, timeout):
        executed.append(command)
        return {"stdout": "", "stderr": "", "returncode": 0}

    from invincible.core import tool_executor

    original = tool_executor._run_command
    tool_executor._run_command = fake_run
    try:
        staged = await _mcp_call(client, ha, "execute_bash", {
            "command": "echo hi",
        })
        confirm_token = _json_loads(staged)["token"]

        # B tries to approve A's action: treated as an unknown token.
        rb = await _mcp_call(client, hb, "confirm_action", {
            "token": confirm_token, "approve": True,
        }, rpc_id=2)
        assert "Unknown or expired confirmation token." in rb.text
        assert executed == []

        # A approves their own action: it runs.
        ra = await _mcp_call(client, ha, "confirm_action", {
            "token": confirm_token, "approve": True,
        }, rpc_id=3)
        assert _json_loads(ra) == {"stdout": "", "stderr": "",
                                   "returncode": 0}
        assert executed == ["echo hi"]
    finally:
        tool_executor._run_command = original


# --- API keys are per-user ----------------------------------------------------------


async def test_api_keys_resolve_only_to_their_owner(client,
                                                    alpha_handler):
    a = await _mint_user_and_key(client, "key-a@example.com")
    b = await _mint_user_and_key(client, "key-b@example.com")

    resp = await _chat(client, auth_for(a["raw"]), "keys-sess", "hi")
    assert resp.status_code == 200

    # Both principals coexist; each raw key resolves to exactly its owner.
    resolved_a = await app.state.api_keys.resolve(a["raw"])
    resolved_b = await app.state.api_keys.resolve(b["raw"])
    assert resolved_a["user_id"] == a["user_id"]
    assert resolved_b["user_id"] == b["user_id"]


# --- /v1/messages non-streaming persist (HIGH-1 regression) --------------------------


async def test_anthropic_non_streaming_persists_to_the_caller(client,
                                                              alpha_handler):
    """A keyed user's non-streaming Anthropic conversation must land in
    THEIR session row - never the local owner's (audit 2026-09-07
    HIGH-1: the non-streaming _persist call omitted the principal, so
    the turns fell back to the operator's session and leaked there)."""
    from invincible.core.db import ensure_local_owner

    a = await _mint_user_and_key(client, "anthropic-a@example.com")
    b = await _mint_user_and_key(client, "anthropic-b@example.com")

    resp = await client.post(
        "/v1/messages",
        headers={**auth_for(a["raw"]), "X-Session-Id": "anthropic-sess"},
        json={"model": "claude-sonnet-4", "max_tokens": 64,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text

    # The turns live under A's identity...
    pk_a = await app.state.sessions.lookup(
        "anthropic-sess", user_id=a["user_id"], project_id=a["project_id"])
    assert pk_a is not None
    history_a = await app.state.sessions.load(
        "anthropic-sess",
        user_id=a["user_id"], project_id=a["project_id"])
    assert [m["content"] for m in history_a if m["role"] == "assistant"] == ["ok"]

    # ...not under B's, and crucially not under the local owner's.
    assert await app.state.sessions.lookup(
        "anthropic-sess", user_id=b["user_id"], project_id=b["project_id"]
    ) is None
    local_uid, local_pid = await ensure_local_owner(app.state.engine)
    assert await app.state.sessions.lookup(
        "anthropic-sess", user_id=local_uid, project_id=local_pid
    ) is None


# --- agent dispatch (audit Step 5, item 3) ------------------------------------------


async def _mint_agent_key(client, email: str) -> tuple[int, str]:
    """Register an account and mint one inv_ key for agent surfaces."""
    from invincible.core.identity import ApiKeyStore
    from tests.conftest import register_account

    made, _ = await register_account(client, email)
    uid = int(made.json()["id"])
    record = await ApiKeyStore(app.state.engine).create(uid, label="agent")
    return uid, record["raw"]


def _bearer(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


async def _stage_agent_job(user_id: int, timeout: float = 5):
    """Dispatch a job into the user's queue and return the awaiting
    dispatcher task."""
    reg = app.state.agent_registry
    task = asyncio.ensure_future(
        reg.dispatch(user_id, "execute_bash",
                     {"command": "echo hi", "timeout": 1}, timeout=timeout)
    )
    await asyncio.sleep(0.01)  # let dispatch stage + park
    return task


async def test_agent_jobs_never_reach_another_users_poll(client,
                                                          monkeypatch):
    """Dispatch queues are keyed by owner (auth via require_agent_auth):
    user B's long-poll answers ``{"job": null}`` even while user A has a
    confirmed job staged, and A's own poll hands it out right after."""
    import invincible.endpoints.agents as agents_mod

    monkeypatch.setattr(agents_mod, "AGENT_POLL_HOLD_SECONDS", 0.05)
    reg = app.state.agent_registry
    uid_a, key_a = await _mint_agent_key(client, "poll-a@example.com")
    _uid_b, key_b = await _mint_agent_key(client, "poll-b@example.com")
    task = await _stage_agent_job(uid_a)

    b_poll = await client.post("/agent/poll", headers=_bearer(key_b))
    assert b_poll.status_code == 200
    assert b_poll.json() == {"job": None}

    a_poll = await client.post("/agent/poll", headers=_bearer(key_a))
    job = a_poll.json()["job"]
    assert job is not None
    assert job["type"] == "execute_bash"
    assert job["args"]["command"] == "echo hi"
    reg.submit_result(uid_a, job["job_id"], {"stdout": "hi"})
    assert (await task)["stdout"] == "hi"


async def test_cross_user_result_submission_is_indistinguishable(
        client, monkeypatch):
    """Anti-enumeration on /agent/result: user B submitting a result for
    A's live job_id, for A's expired job_id, and for an unknown job_id
    all receive byte-identical ``{"accepted": false}`` responses - no
    signal leaks about which job ids exist - and the forgery never
    resolves A's future."""
    import invincible.endpoints.agents as agents_mod

    monkeypatch.setattr(agents_mod, "AGENT_POLL_HOLD_SECONDS", 0.05)
    uid_a, key_a = await _mint_agent_key(client, "res-a@example.com")
    _uid_b, key_b = await _mint_agent_key(client, "res-b@example.com")

    live_task = await _stage_agent_job(uid_a)
    live_poll = await client.post("/agent/poll", headers=_bearer(key_a))
    live_id = live_poll.json()["job"]["job_id"]

    expired_task = await _stage_agent_job(uid_a, timeout=0.05)
    expired_poll = await client.post("/agent/poll", headers=_bearer(key_a))
    expired_id = expired_poll.json()["job"]["job_id"]
    expired_result = await asyncio.wait_for(expired_task, 1)
    assert expired_result["status"] == "agent_timeout"

    responses = []
    for job_id in (live_id, expired_id, "no-such-job-id"):
        forged = await client.post(
            "/agent/result", headers=_bearer(key_b),
            json={"job_id": job_id, "result": {"stdout": "evil"}},
        )
        responses.append(forged)
    # identical shape: status, body, content type - no oracle for B
    assert {r.status_code for r in responses} == {200}
    assert {json.dumps(r.json(), sort_keys=True) for r in responses} \
        == {'{"accepted": false}'}
    assert len({r.headers["content-type"] for r in responses}) == 1

    # the live job is untouched by the forgeries: A still resolves it
    accepted = await client.post(
        "/agent/result", headers=_bearer(key_a),
        json={"job_id": live_id, "result": {"stdout": "good"}},
    )
    assert accepted.json()["accepted"] is True
    assert (await live_task)["stdout"] == "good"


# --- BYOK routing (audit Step 5, item 4) ---------------------------------------------


def _counting_handlers(router_setter):
    """Counting MockTransport handlers for every host that could carry a
    request: both users' credential hosts plus the operator pool's."""
    hosts = ("a.example.com", "b.example.com",
             "alpha.example.com", "beta.example.com", "gamma.example.com")
    captured = {host: [] for host in hosts}
    handlers = {}
    for host in hosts:
        def handler(request, host=host):
            captured[host].append(json.loads(request.read()))
            return httpx.Response(200, json=provider_body(host.split(".")[0]))
        handlers[host] = handler
    router_setter(handlers)
    return captured


async def test_byok_chat_never_routes_through_another_users_credential(
        client, router_setter):
    """A's chat request leaves through A's connected credential host
    only - B's credential host and the operator pool's hosts are
    provably never called (routing is keyed to the requesting user's
    credential rows; there is no cross-user fallback)."""
    captured = _counting_handlers(router_setter)
    a = await _mint_user_and_key(client, "route-a@example.com",
                                 host="a.example.com", model_id="a-model")
    # B exists with a live credential on a different host; routing to it
    # would be the cross-user leak this test pins against.
    await _mint_user_and_key(client, "route-b@example.com",
                             host="b.example.com", model_id="b-model")

    resp = await client.post(
        "/v1/chat/completions", headers=auth_for(a["raw"]),
        json={"model": "a-model",
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-invincible-provider"] == "Cred a"
    assert len(captured["a.example.com"]) == 1
    assert captured["b.example.com"] == []
    for host in ("alpha.example.com", "beta.example.com",
                 "gamma.example.com"):
        assert captured[host] == [], f"operator host {host} was called"


async def test_byok_cooldown_never_falls_back_to_another_users_pool(
        client, router_setter):
    """A's sole credential in cooldown (health_id ``byok:{id}``) fails
    the request cleanly - the router never consults B's credential pool
    or the operator's to fill the gap."""
    captured = _counting_handlers(router_setter)
    a = await _mint_user_and_key(client, "cool-a@example.com",
                                 host="a.example.com", model_id="a-model")
    await _mint_user_and_key(client, "cool-b@example.com",
                             host="b.example.com", model_id="b-model")

    app.state.router.health_tracker.record_failure(
        f"byok:{a['credential_id']}")

    resp = await client.post(
        "/v1/chat/completions", headers=auth_for(a["raw"]),
        json={"model": "a-model",
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 503, resp.text
    for host, calls in captured.items():
        assert calls == [], f"{host} was called during cooldown"


def _json_loads(response) -> dict:
    """Extract the JSON object from an MCP tools/call text content."""
    body = response.json()
    return json.loads(body["result"]["content"][0]["text"])
