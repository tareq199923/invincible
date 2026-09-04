import asyncio

from invincible.core.agent_registry import AgentRegistry
from invincible.core.identity import ApiKeyStore
from invincible.main import app
from tests.conftest import register_account


def agent_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _mint_key(client, email="agent-user@example.com") -> tuple[int, str]:
    """Register an account and mint one inv_ key for it."""
    made, _ = await register_account(client, email=email)
    uid = int(made.json()["id"])
    record = await ApiKeyStore(app.state.engine).create(uid, label="agent")
    return uid, record["raw"]


async def _stage_job(user_id: int):
    """Dispatch a job into the user's queue (no waiter) and return its
    poll-visible form."""
    reg: AgentRegistry = app.state.agent_registry
    task = asyncio.ensure_future(
        reg.dispatch(user_id, "execute_bash",
                     {"command": "echo hi", "timeout": 1}, timeout=5)
    )
    await asyncio.sleep(0.01)  # let dispatch stage + park
    return task


async def test_poll_without_bearer_is_401(client):
    response = await client.post("/agent/poll")
    assert response.status_code == 401


async def test_poll_with_garbage_key_is_401(client):
    response = await client.post("/agent/poll",
                                 headers=agent_headers("inv_garbage"))
    assert response.status_code == 401


async def test_gateway_key_cannot_reach_agent_surface(client):
    """require_agent_auth is deliberately NOT require_auth: the legacy
    gateway-key realm (and the fail-open anonymous mode) must never
    reach agent dispatch."""
    response = await client.post(
        "/agent/poll", headers=agent_headers("test-gateway-key")
    )
    assert response.status_code == 401


async def test_poll_no_work_returns_null_job(client, monkeypatch):
    _, key = await _mint_key(client)
    # The endpoint's hold (25s) is real time even in-process; shrink it
    # for this test the same way other timing-sensitive tests do.
    import invincible.endpoints.agents as agents_mod
    monkeypatch.setattr(agents_mod, "AGENT_POLL_HOLD_SECONDS", 0.05)
    response = await client.post("/agent/poll", headers=agent_headers(key))
    assert response.status_code == 200
    assert response.json() == {"job": None}


async def test_poll_hands_out_dispatched_job(client):
    uid, key = await _mint_key(client)
    task = await _stage_job(uid)
    response = await client.post("/agent/poll", headers=agent_headers(key))
    assert response.status_code == 200
    job = response.json()["job"]
    assert job is not None
    assert job["type"] == "execute_bash"
    assert job["args"]["command"] == "echo hi"
    # the dispatcher resolves once the result is posted
    app.state.agent_registry.submit_result(
        uid, job["job_id"], {"stdout": "hi", "returncode": 0}
    )
    assert (await task)["stdout"] == "hi"


async def test_result_accept_and_replay(client):
    uid, key = await _mint_key(client)
    task = await _stage_job(uid)
    poll = (await client.post("/agent/poll",
                              headers=agent_headers(key))).json()
    job_id = poll["job"]["job_id"]

    accepted = await client.post(
        "/agent/result", headers=agent_headers(key),
        json={"job_id": job_id, "result": {"stdout": "x", "returncode": 0}},
    )
    assert accepted.status_code == 200
    assert accepted.json()["accepted"] is True
    assert (await task)["stdout"] == "x"

    replayed = await client.post(
        "/agent/result", headers=agent_headers(key),
        json={"job_id": job_id, "result": {"stdout": "evil"}},
    )
    assert replayed.status_code == 200
    assert replayed.json()["accepted"] is False


async def test_result_unknown_job_is_just_not_accepted(client):
    _, key = await _mint_key(client)
    response = await client.post(
        "/agent/result", headers=agent_headers(key),
        json={"job_id": "nope", "result": {}},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is False


async def test_result_cross_user_is_not_accepted(client):
    """User 2's key cannot resolve user 1's job - isolation is
    structural: submit_result keys the future to the dispatching
    user."""
    uid_a, key_a = await _mint_key(client, "a@example.com")
    _, key_b = await _mint_key(client, "b@example.com")
    task = await _stage_job(uid_a)
    poll = (await client.post("/agent/poll",
                              headers=agent_headers(key_a))).json()
    job_id = poll["job"]["job_id"]

    stolen = await client.post(
        "/agent/result", headers=agent_headers(key_b),
        json={"job_id": job_id, "result": {"stdout": "evil"}},
    )
    assert stolen.json()["accepted"] is False
    # the real owner still can
    ok = await client.post(
        "/agent/result", headers=agent_headers(key_a),
        json={"job_id": job_id, "result": {"stdout": "good"}},
    )
    assert ok.json()["accepted"] is True
    assert (await task)["stdout"] == "good"


async def test_result_rejects_bad_body(client):
    _, key = await _mint_key(client)
    response = await client.post("/agent/result",
                                 headers=agent_headers(key), json={"x": 1})
    assert response.status_code == 400


async def test_result_rejects_non_dict_result(client):
    _, key = await _mint_key(client)
    response = await client.post(
        "/agent/result", headers=agent_headers(key),
        json={"job_id": "whatever", "result": "not-a-dict"},
    )
    assert response.status_code == 400


async def test_status_requires_session(client):
    response = await client.get("/agent/status")
    assert response.status_code == 401


async def test_status_reports_liveness(client):
    uid, key = await _mint_key(client)
    # not signed in as that user: use the session path via login
    login = await client.post(
        "/auth/login",
        json={"email": "agent-user@example.com",
              "password": "longenough1"},
    )
    assert login.status_code == 200
    # agent has not polled: offline
    offline = await client.get("/agent/status")
    assert offline.json()["agent_online"] is False
    # a poll (any poll) brings it online
    await client.post("/agent/poll", headers=agent_headers(key))
    online = await client.get("/agent/status")
    assert online.json()["agent_online"] is True
