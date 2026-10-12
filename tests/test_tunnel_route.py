# tests/test_tunnel_route.py
"""F03 slices 3-5, full-app: MCP stage/confirm, /t/ route, isolation walls.

Uses the real app (``client`` fixture, Postgres-backed) with a fake
agent socket: ``AutoAgent`` acks ``tunnel_open`` and answers request
heads with canned bytes, so stage→confirm→stream→close runs end to end
without a real browser or TCP origin. The other eighteen of the twenty
acceptance tests in the F03 design note live here (the pure-unit two —
throttle timing and the loopback-host rule — are in
tests/test_tunnel_registry.py).
"""
import asyncio
import json
from urllib.parse import urlparse

from invincible.core.harness_tunnel import encode_data_frame
from invincible.endpoints.agents import _route_tunnel_frame
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


_USER_SEQ = 0


async def _user(client):
    """One fresh account + MCP bearer headers per call (the shared
    consent helper pins one email, so two users need two addresses)."""
    global _USER_SEQ
    _USER_SEQ += 1
    email = f"tunnel-user-{_USER_SEQ}@example.com"
    registered = await client.post(
        "/auth/register",
        json={"email": email, "password": "longenough1"})
    assert registered.status_code == 201, registered.text
    tokens = await obtain_access_token(client, email=email)
    return (
        {"Authorization": f"Bearer {tokens['access_token']}"},
        int(tokens["user_id"]),
    )


async def _pending_token(staged):
    result = json.loads(staged.json()["result"]["content"][0]["text"])
    assert result["status"] == "pending_confirmation"
    return result["token"]


def _text(response):
    return response.json()["result"]["content"][0]["text"]


def _payload(response):
    return json.loads(_text(response))


class AutoAgent:
    """Fake paired-agent socket: acks tunnel opens and answers tunneled
    requests with canned bytes. The ack is deferred a tick (real agents
    answer over the network) so the confirm-time future always exists."""

    def __init__(self, tunnels, body: bytes = b"demo-bytes", status=200):
        self.sent: list = []
        self._tunnels = tunnels
        self._body = body
        self._status = status

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)
        if message.get("type") == "tunnel_open":
            async def _ack():
                self._tunnels.resolve_ack(
                    message["tunnel_id"], self, True)

            asyncio.ensure_future(_ack())
        if message.get("type") == "tunnel_request_head":
            async def _answer():
                tid = message["tunnel_id"]
                sid = message["stream_id"]
                _route_tunnel_frame(self._tunnels, {
                    "type": "tunnel_response_head", "tunnel_id": tid,
                    "stream_id": sid, "status": self._status,
                    "headers": {"Content-Type": "text/plain"},
                })
                _route_tunnel_frame(
                    self._tunnels,
                    encode_data_frame(tid, sid, 0, self._body))
                self._tunnels.drop_stream(tid, sid)

            asyncio.ensure_future(_answer())

    def heads(self, kind: str) -> list:
        return [m for m in self.sent if m.get("type") == kind]


async def _open_tunnel(client, headers, subject, port=3000,
                       body: bytes = b"demo-bytes", status=200):
    """Stage + confirm with an auto-ack agent attached. Returns
    (token, agent, result-dict)."""
    agent_registry = app.state.agent_registry
    tunnels = app.state.tunnel_registry
    agent = AutoAgent(tunnels, body=body, status=status)
    agent_registry.attach_ws(subject, agent)
    staged = await _call(client, headers, "expose_port", {"port": port})
    token = await _pending_token(staged)
    confirmed = await _call(client, headers, "confirm_action",
                            {"token": token, "approve": True})
    result = _payload(confirmed)
    assert result["status"] == "tunnel_ready", result
    url_token = urlparse(result["url"]).path.split("/")[2]
    return url_token, agent, result


# --- Wall 1: token-guess rejection -------------------------------------------

async def test_tunnel_unknown_token_returns_404_indistinguishable(client):
    first = await client.get("/t/guess-one/anything")
    second = await client.post("/t/guess-two/", content=b"x")
    assert first.status_code == 404
    assert second.status_code == 404
    # byte-identical bodies: probers learn nothing
    assert first.json() == second.json() == {"detail": "Not found"}


async def test_tunnel_wrong_method_token_shape_still_404(client):
    # (".." normalizes to "/" in every HTTP client — the landing page,
    # not this route — so it is not a shape worth probing here.)
    for token in ("x" * 500, "a b", "null", "undefined", "...",
                  ".hidden", "t", "-1", "0"):
        response = await client.get(f"/t/{token}/")
        assert response.status_code == 404, token
        assert response.json() == {"detail": "Not found"}


# --- Wall 2: cross-user denial ------------------------------------------------

async def test_tunnel_cross_user_token_denied(client, monkeypatch):
    """Management is owner-bound (the shareable URL is bearer by design;
    close is not): B's close of A's tunnel answers not-found and A's
    tunnel survives."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    headers_a, subject_a = await _user(client)
    headers_b, _subject_b = await _user(client)
    token, _agent, _result = await _open_tunnel(
        client, headers_a, subject_a)

    denied = await _call(client, headers_b, "tunnel_close",
                         {"token": token})
    assert denied.json()["result"]["isError"] is True
    assert _text(denied) == "Unknown or expired tunnel token."

    survived = await _call(client, headers_a, "tunnel_close",
                           {"token": token})
    assert _payload(survived) == {"status": "closed"}


async def test_tunnel_bytes_never_routed_across_users(client, monkeypatch):
    """A's stream bytes ride A's acking socket only — B's attached
    socket receives nothing (mirror of test_cross_user_isolation)."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    headers_a, subject_a = await _user(client)
    _headers_b, subject_b = await _user(client)
    agent_b = AutoAgent(app.state.tunnel_registry)
    app.state.agent_registry.attach_ws(subject_b, agent_b)

    token, agent_a, _result = await _open_tunnel(
        client, headers_a, subject_a)
    response = await client.get(f"/t/{token}/hello")
    assert response.status_code == 200
    assert response.content == b"demo-bytes"
    assert len(agent_a.heads("tunnel_request_head")) == 1
    assert agent_b.sent == []  # B's socket got nothing


# --- Wall 3: expired-token replay ---------------------------------------------

async def test_tunnel_expired_token_replays_as_404(client, monkeypatch):
    """A TTL-dead token replays exactly like an unknown one."""
    import time

    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    monkeypatch.setenv("INVINCIBLE_TUNNEL_TTL_SECONDS", "100")
    headers_a, subject_a = await _user(client)
    token, _agent, _result = await _open_tunnel(
        client, headers_a, subject_a)

    real = time.time
    reg = app.state.tunnel_registry
    monkeypatch.setattr(reg, "_clock", lambda: real() + 101)
    replay = await client.get(f"/t/{token}/late")
    assert replay.status_code == 404
    assert replay.json() == {"detail": "Not found"}


async def test_tunnel_consumed_close_token_stays_dead(client, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    headers_a, subject_a = await _user(client)
    token, _agent, _result = await _open_tunnel(
        client, headers_a, subject_a)

    assert _payload(await _call(client, headers_a, "tunnel_close",
                                {"token": token})) == {"status": "closed"}
    replay = await client.get(f"/t/{token}/")
    assert replay.status_code == 404
    assert replay.json() == {"detail": "Not found"}
    again = await _call(client, headers_a, "tunnel_close",
                        {"token": token})
    assert _text(again) == "Unknown or expired tunnel token."


# --- Wall 4: cap enforcement ---------------------------------------------------

async def test_tunnel_total_bytes_cap_closes_stream(client, monkeypatch):
    """A 20-byte canned body against a 10-byte lifetime total arrives
    truncated (empty) — the stream ends, fail closed, with a 200 head."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    monkeypatch.setenv("INVINCIBLE_TUNNEL_TOTAL_BYTES", "10")
    headers_a, subject_a = await _user(client)
    token, _agent, _result = await _open_tunnel(
        client, headers_a, subject_a, body=b"0123456789abcdef7890")
    response = await client.get(f"/t/{token}/big")
    assert response.status_code == 200
    assert response.content == b""


async def test_tunnel_max_per_user_refused(client, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    monkeypatch.setenv("INVINCIBLE_TUNNEL_MAX_PER_USER", "1")
    headers_a, subject_a = await _user(client)
    token, _agent, _result = await _open_tunnel(
        client, headers_a, subject_a)

    staged = await _call(client, headers_a, "expose_port", {"port": 3001})
    second = await _pending_token(staged)
    capped = await _call(client, headers_a, "confirm_action",
                         {"token": second, "approve": True})
    assert capped.json()["result"]["isError"] is True
    assert "cap 1" in _text(capped)
    # the first tunnel is untouched
    assert _payload(await _call(client, headers_a, "tunnel_close",
                                {"token": token})) == {"status": "closed"}


# --- stage / confirm flow -------------------------------------------------------

async def test_expose_port_stages_pending_confirmation(client):
    """Staging issues a token and exposes nothing (no tunnel record)."""
    headers_a, subject_a = await _user(client)
    staged = await _call(client, headers_a, "expose_port", {"port": 3000})
    body = staged.json()
    assert body["result"]["isError"] is False
    result = json.loads(body["result"]["content"][0]["text"])
    assert result["status"] == "pending_confirmation"
    assert result["action"] == "expose_port"
    assert result["port"] == 3000
    assert result["token"]
    assert app.state.tunnel_registry.tunnels_for(subject_a) == []


async def test_expose_port_confirm_binds_owner_subject(client, monkeypatch):
    """B confirming A's staged token sees not-found (indistinguishable
    from a wrong guess); the token is NOT consumed — A confirms after."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    headers_a, subject_a = await _user(client)
    headers_b, _subject_b = await _user(client)
    staged = await _call(client, headers_a, "expose_port", {"port": 3000})
    token = await _pending_token(staged)

    foreign = await _call(client, headers_b, "confirm_action",
                          {"token": token, "approve": True})
    assert _text(foreign) == "Unknown or expired confirmation token."

    agent = AutoAgent(app.state.tunnel_registry)
    app.state.agent_registry.attach_ws(subject_a, agent)
    own = await _call(client, headers_a, "confirm_action",
                      {"token": token, "approve": True})
    assert _payload(own)["status"] == "tunnel_ready"


async def test_expose_port_denied_port_never_stages(client):
    """Infrastructure ports are refused pre-stage (no token, no record)."""
    headers_a, subject_a = await _user(client)
    for port in (22, 3389, 5432, 5433, 3306, 1433, 6379, 27017, 25, 587):
        refused = await _call(client, headers_a, "expose_port",
                              {"port": port})
        assert refused.json()["result"]["isError"] is True, port
        assert _text(refused).startswith("Blocked:"), port
    assert app.state.tunnel_registry.tunnels_for(subject_a) == []


# --- relay integrity + orphans ----------------------------------------------------

async def test_tunnel_frames_ignore_unknown_without_breaking_relay(client):
    """Garbage tunnel frames never raise and never disturb job flow."""
    tunnels = app.state.tunnel_registry
    registry = app.state.agent_registry
    # unknown tunnel, bad base64, job-keyed forgery, empty dict
    _route_tunnel_frame(tunnels, {
        "type": "tunnel_data", "tunnel_id": "nope", "stream_id": "s",
        "seq": 0,
        "payload_b64": "aGk="})
    _route_tunnel_frame(tunnels, {
        "type": "tunnel_data", "tunnel_id": "nope", "stream_id": "s",
        "seq": 0, "payload_b64": "!!!bad!!!"})
    _route_tunnel_frame(tunnels, {
        "type": "tunnel_response_head", "tunnel_id": "nope",
        "stream_id": "s", "status": 200, "headers": {}})
    _route_tunnel_frame(tunnels, {"type": "tunnel_close",
                                  "tunnel_id": "nope", "stream_id": "s"})
    # the relay still dispatches jobs afterwards
    async def _dispatcher():
        return await registry.dispatch(
            4242, "execute_bash", {"command": "echo hi"}, timeout=5)

    async def _agent():
        job = await asyncio.wait_for(registry.poll(4242, hold=1),
                                     timeout=2)
        assert registry.submit_result(
            4242, job["job_id"], {"stdout": "hi"})

    result, _ = await asyncio.gather(_dispatcher(), _agent())
    assert result == {"stdout": "hi"}


async def test_tunnel_agent_disconnect_closes_streams(client, monkeypatch):
    """C2: socket teardown hard-closes its tunnels — tokens drop,
    stream queues seal, nothing dangles for a reconnect to resume."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    headers_a, subject_a = await _user(client)
    token, agent, _result = await _open_tunnel(
        client, headers_a, subject_a)
    tunnels = app.state.tunnel_registry
    queue = tunnels.stream_queue(token, "orphan")
    assert queue is not None

    doomed = tunnels.detach_ws(agent)  # what agents.py finally: does
    assert doomed == 1
    assert tunnels.lookup(token) is None
    assert tunnels.tunnels_for(subject_a) == []
    assert queue.get_nowait() is not None  # sealed (STREAM_CLOSED)
    gone = await client.get(f"/t/{token}/")
    assert gone.status_code == 404


# --- routing-off + audit + hygiene + round trip -------------------------------------

async def test_tunnel_routing_off_refuses_with_clear_error(client):
    """Default (INVINCIBLE_AGENT_ROUTING unset): confirm refuses with
    tunnel_unavailable — never server-host fallback (there is no local
    port to expose to)."""
    headers_a, subject_a = await _user(client)
    staged = await _call(client, headers_a, "expose_port", {"port": 3000})
    token = await _pending_token(staged)
    refused = await _call(client, headers_a, "confirm_action",
                          {"token": token, "approve": True})
    assert refused.json()["result"]["isError"] is True
    assert "INVINCIBLE_AGENT_ROUTING" in _text(refused)
    assert app.state.tunnel_registry.tunnels_for(subject_a) == []


async def test_tunnel_audit_rows_metadata_only(client, monkeypatch):
    """Open + close audit rows carry statuses, never the token."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    headers_a, _subject_a = await _user(client)
    token, _agent, _result = await _open_tunnel(client, headers_a,
                                                _subject_a)
    await _call(client, headers_a, "tunnel_close", {"token": token})

    rows = await app.state.audit_log.recent(limit=50)
    blob = json.dumps(rows, default=str)
    assert token not in blob  # tokens never logged
    actions = [row.get("action", "") for row in rows]
    assert "mcp.confirm_action.tunnel_ready" in actions
    assert "mcp.tunnel_close.closed" in actions


async def test_tunnel_auth_headers_stripped_before_forward(client,
                                                           monkeypatch):
    """Authorization/Cookie/Host never reach the origin: the tunnel must
    not become a credential-forwarding oracle against local services."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    headers_a, subject_a = await _user(client)
    token, agent, _result = await _open_tunnel(
        client, headers_a, subject_a)
    response = await client.get(
        f"/t/{token}/admin",
        headers={"Authorization": "Bearer zzz", "Cookie": "s=1"})
    assert response.status_code == 200
    heads = agent.heads("tunnel_request_head")
    assert len(heads) == 1
    forwarded = {key.lower(): value
                 for key, value in heads[0]["headers"].items()}
    assert "authorization" not in forwarded
    assert "cookie" not in forwarded
    assert "host" not in forwarded
    assert heads[0]["method"] == "GET"
    assert heads[0]["path"] == "/admin"


async def test_tunnel_open_full_round_trip(client, monkeypatch):
    """Stage → confirm (agent acks) → public bytes → close → 404."""
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    headers_a, _subject_a = await _user(client)
    token, agent, result = await _open_tunnel(
        client, headers_a, _subject_a, port=5173,
        body=b"round-trip-ok", status=201)
    assert result["port"] == 5173
    assert result["url"].endswith(f"/t/{token}/")
    assert result["expires_in_seconds"] > 0

    response = await client.get(f"/t/{token}/app")
    assert response.status_code == 201
    assert response.content == b"round-trip-ok"
    assert response.headers["content-type"] == "text/plain"

    assert _payload(await _call(client, headers_a, "tunnel_close",
                                {"token": token})) == {"status": "closed"}
    assert (await client.get(f"/t/{token}/app")).status_code == 404
    assert agent.heads("tunnel_request_head")[0]["path"] == "/app"


async def test_tunnel_close_is_idempotent_and_owner_bound(client,
                                                          monkeypatch):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROUTING", "1")
    headers_a, _subject_a = await _user(client)
    token, _agent, _result = await _open_tunnel(client, headers_a,
                                                _subject_a)
    assert _payload(await _call(client, headers_a, "tunnel_close",
                                {"token": token})) == {"status": "closed"}
    repeat = await _call(client, headers_a, "tunnel_close",
                         {"token": token})
    assert repeat.json()["result"]["isError"] is True
    assert _text(repeat) == "Unknown or expired tunnel token."
    # closing with no token arg answers the same way
    missing = await _call(client, headers_a, "tunnel_close", {})
    assert _text(missing) == "Unknown or expired tunnel token."
