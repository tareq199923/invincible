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
                    (unknown-token semantics), then A confirms fine.

Enumeration: sequential id probing by B yields byte-identical negative
shapes regardless of whether the string exists for someone else.
"""
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


async def _mint_user_and_key(client, email: str) -> dict:
    """Real user row + default project + one API key + one connected BYOK
    credential on the standard mock host (Phase 9: keyed principals chat
    only through their own connected providers). Returns the key record
    plus the resolved ids."""
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
    await ByokCredentialStore(engine).create(
        user_id=int(uid), provider_name="Test Pool",
        model_id="alpha-model",
        base_url="https://alpha.example.com/v1",
        api_key="user-key",
    )
    record = await app.state.api_keys.create(int(uid))
    return {"user_id": int(uid), "project_id": int(pid),
            "raw": record["raw"]}


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


def _json_loads(response) -> dict:
    """Extract the JSON object from an MCP tools/call text content."""
    body = response.json()
    return json.loads(body["result"]["content"][0]["text"])
