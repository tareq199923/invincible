# tests/test_cli_login.py
"""``invincible login`` device pairing.

The pairing loop runs hermetically against the ASGI app (injectable
transport + sleep), covering the happy path end-to-end through a real
minted key, the deny path, and the config-file persistence the command
performs.
"""
import asyncio
import json

import httpx
import pytest
from click.testing import CliRunner
from cryptography.fernet import Fernet
from sqlalchemy import text

from invincible.cli import (
    _client_config_path,
    _DevicePairError,
    _pair_device,
    _save_client_config,
    login,
)
from invincible.cli import (
    agent as agent_command,
)
from invincible.core.credential_store import ByokCredentialStore
from invincible.main import app
from tests.conftest import register_account


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


async def _connect_byok_for(email: str) -> None:
    """Connect one BYOK credential on the standard mock host for an
    existing account, so its keyed principal can chat (Phase 9)."""
    engine = app.state.engine
    async with engine.connect() as conn:
        uid = (await conn.execute(text(
            "SELECT id FROM users WHERE email = :e"
        ), {"e": email})).scalar_one()
    await ByokCredentialStore(engine).create(
        user_id=int(uid), provider_name="Test Pool",
        model_id="alpha-model",
        base_url="https://alpha.example.com/v1",
        api_key="user-key",
    )


def _anon_client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _noop_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def test_pair_device_happy_path(client, router_setter):
    await register_account(client, "cli@pair.example")
    await _connect_byok_for("cli@pair.example")
    anon = _anon_client()
    try:
        seen_codes: list[str] = []
        approved = False
        code_seen = asyncio.Event()

        async def _on_code(url: str, code: str) -> None:
            seen_codes.append(code)
            code_seen.set()

        async def _tick(_seconds: float) -> None:
            # approve from the logged-in browser exactly once, on the
            # first pending poll
            if not seen_codes:
                return
            nonlocal approved
            if approved:
                return
            approved = True
            response = await client.post(
                f"/auth/devices/{seen_codes[0]}/approve")
            assert response.status_code == 200

        pair_task = asyncio.ensure_future(_pair_device(
            "http://test", client=anon, sleep=_tick, on_code=_on_code))
        result = await asyncio.wait_for(pair_task, 10)

        assert result["access_token"].startswith("inv_")
        assert result["token_type"] == "invincible_api_key"

        # the minted key actually works on chat
        router_setter({
            "alpha.example.com": httpx.Response(200, json={
                "id": "cmpl-1", "model": "alpha-model",
                "choices": [{"message": {"role": "assistant",
                                         "content": "paired"}}],
            }),
        })
        chat = await client.post(
            "/v1/chat/completions",
            headers={"Authorization":
                     f"Bearer {result['access_token']}"},
            json={"model": "alpha-model",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert chat.status_code == 200
    finally:
        await anon.aclose()


async def test_pair_device_denied(client):
    await register_account(client, "deny@pair.example")
    anon = _anon_client()
    try:
        seen_codes: list[str] = []

        async def _deny_then_finish(_seconds: float) -> None:
            if seen_codes and len(seen_codes) == 1:
                await client.post(f"/auth/devices/{seen_codes[0]}/deny")

        with pytest.raises(_DevicePairError) as excinfo:
            await _pair_device(
                "http://test", client=anon, sleep=_deny_then_finish,
                on_code=lambda url, code: seen_codes.append(code))
        assert "denied" in str(excinfo.value).lower()
    finally:
        await anon.aclose()


def test_save_client_config_roundtrip(tmp_path):
    target = str(tmp_path / "cfg.json")
    saved = _save_client_config(server="https://inv.example",
                                api_key="inv_abc", path=target)
    assert saved == target
    with open(saved, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data == {"server": "https://inv.example", "api_key": "inv_abc"}


def test_client_config_path_default_under_home():
    import os

    path = _client_config_path(None)
    assert path.endswith(os.path.join(".invincible", "config.json"))


def test_login_command_wiring(monkeypatch, tmp_path):
    """Click layer: prints URL/code, saves the token, reports the path."""
    config_target = tmp_path / "config.json"

    captured: dict = {}

    async def _fake_pair(base_url, **kwargs):
        captured["base_url"] = base_url

        async def _on_code(url, code):
            kwargs_present = True  # noqa: F841 - signature parity only

        # emulate what the real helper reports to click via on_code
        class _Fake:
            pass

        return {"access_token": "inv_rawrawraw", "prefix": "inv_rawrawra"}

    monkeypatch.setattr("invincible.cli._pair_device", _fake_pair)
    runner = CliRunner()
    result = runner.invoke(
        login, ["--server", "http://example.test:8000/",
                "--config", str(config_target)])
    assert result.exit_code == 0, result.output
    assert captured["base_url"] == "http://example.test:8000"
    assert "Paired" in result.output
    assert "inv_rawrawra" in result.output
    with open(config_target, encoding="utf-8") as handle:
        assert json.load(handle)["api_key"] == "inv_rawrawraw"


def test_login_command_reports_failure(monkeypatch):
    async def _failing(base_url, **kwargs):
        raise _DevicePairError("the request was denied.")

    monkeypatch.setattr("invincible.cli._pair_device", _failing)
    runner = CliRunner()
    result = runner.invoke(login, ["--server", "http://example.test"])
    assert result.exit_code != 0
    assert "denied" in result.output


def test_login_defaults_to_hosted_service(monkeypatch):
    """Phase 11: the flexx-style default - plain `invincible login`
    pairs with the hosted service, no URL, no questions. Self-hosters
    opt out with --server (pinned separately by every other test)."""
    captured: dict = {}

    async def _fake_pair(base_url, **kwargs):
        captured["base_url"] = base_url
        return {"access_token": "inv_x", "prefix": "inv_x"}

    monkeypatch.setattr("invincible.cli._pair_device", _fake_pair)
    monkeypatch.setattr("invincible.cli._open_browser", lambda url: None)
    runner = CliRunner()
    result = runner.invoke(login, [])
    assert result.exit_code == 0, result.output
    assert captured["base_url"] == "https://invincible-ai.me"


def test_login_opens_the_approval_page(monkeypatch, tmp_path):
    """The one-click flow: the browser is opened on the URL the pairing
    handshake reported (the code-embedded approval page)."""
    opened: list = []

    async def _fake_pair(base_url, **kwargs):
        # reproduce the real helper's behavior: call on_code with the
        # complete verification URL, then succeed
        on_code = kwargs.get("on_code")
        if on_code is not None:
            result = on_code("https://sv.test/auth/devices/ABCD1234",
                             "ABCD1234")
            if hasattr(result, "__await__"):
                await result
        return {"access_token": "inv_y", "prefix": "inv_y"}

    monkeypatch.setattr("invincible.cli._pair_device", _fake_pair)
    monkeypatch.setattr("invincible.cli._open_browser",
                        lambda url: opened.append(url))
    runner = CliRunner()
    config_target = tmp_path / "config.json"
    result = runner.invoke(login, ["--server", "https://sv.test",
                                   "--config", str(config_target)])
    assert result.exit_code == 0, result.output
    assert opened == ["https://sv.test/auth/devices/ABCD1234"]
    assert "Approval page: https://sv.test/auth/devices/ABCD1234" \
        in result.output


# --- Phase 11: one-click pairing ------------------------------------------


async def test_device_code_returns_complete_uri(client):
    """The pairing handshake includes RFC 8628 verification_uri_complete:
    the code embedded in the approval URL, so the CLI can open one link
    instead of printing a dead-end /login and a code nobody can enter."""
    response = await client.post("/auth/device/code")
    assert response.status_code == 200
    payload = response.json()
    assert payload["verification_uri_complete"] == (
        f"http://test/auth/devices/{payload['user_code']}"
    )


async def test_pair_device_prefers_complete_uri(client):
    """_pair_device hands on_code the complete URL (code inside), not
    the bare /login that redirects signed-in users to /account."""
    await register_account(client, "complete-uri@example.com")
    seen: dict = {}
    approved = False

    async def _on_code(url: str, code: str) -> None:
        seen["url"], seen["code"] = url, code

    async def _tick(_seconds: float) -> None:
        # approve from the logged-in browser once a code was seen
        nonlocal approved
        if seen and not approved:
            approved = True
            response = await client.post(
                f"/auth/devices/{seen['code']}/approve")
            assert response.status_code == 200

    anon = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        pair_task = asyncio.ensure_future(
            _pair_device("http://test", client=anon,
                         sleep=_tick, on_code=_on_code))
        result = await asyncio.wait_for(pair_task, 10)
        assert result["access_token"].startswith("inv_")
        assert "/auth/devices/" in seen["url"]
        assert seen["code"] in seen["url"]
    finally:
        await anon.aclose()


async def test_device_lookup_redirects_to_approval_page(client):
    """The Account page's Pair-a-device form target: ?code= becomes a
    redirect to /auth/devices/<CODE> (uppercased, like the page does)."""
    await register_account(client, "lookup@example.com")
    login_response = await client.post(
        "/auth/login",
        json={"email": "lookup@example.com", "password": "longenough1"})
    assert login_response.status_code == 200

    response = await client.get("/auth/devices", params={"code": "xk7m49qp"},
                                follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/devices/XK7M49QP"

    empty = await client.get("/auth/devices", follow_redirects=False)
    assert empty.status_code == 303
    assert empty.headers["location"] == "/account"


async def test_account_page_has_pair_a_device_box(client):
    await register_account(client, "pairbox@example.com")
    await client.post("/auth/login",
                      json={"email": "pairbox@example.com",
                            "password": "longenough1"})
    page = await client.get("/account")
    assert page.status_code == 200
    assert "Pair a device" in page.text
    assert 'action="/auth/devices"' in page.text


# --- first-run agent self-pairing (one command, zero prior steps) ----------


def test_agent_self_pairs_on_first_run(monkeypatch, tmp_path):
    """No saved credentials: `invincible agent` pairs with the hosted
    default, saves the token, teaches the MCP-connect step, and starts
    the loop with the minted key."""
    config_target = tmp_path / "config.json"
    captured: dict = {}

    async def _fake_pair(base_url, **kwargs):
        captured["base_url"] = base_url
        return {"access_token": "inv_selfpair", "prefix": "inv_selfpa"}

    async def _fake_run_agent(server, api_key, **kwargs):
        captured.update(agent_server=server, agent_key=api_key)

    monkeypatch.setattr("invincible.cli._pair_device", _fake_pair)
    monkeypatch.setattr("invincible.agent.runner.run_agent",
                        _fake_run_agent)
    result = CliRunner().invoke(
        agent_command, ["--config", str(config_target)])
    assert result.exit_code == 0, result.output
    assert captured == {
        "base_url": "https://invincible-ai.me",
        "agent_server": "https://invincible-ai.me",
        "agent_key": "inv_selfpair",
    }
    with open(config_target, encoding="utf-8") as handle:
        assert json.load(handle)["api_key"] == "inv_selfpair"
    assert "isn't paired yet" in result.output
    assert "Paired" in result.output
    assert "Next: connect your AI" in result.output


def test_agent_first_run_honors_server_flag(monkeypatch, tmp_path):
    config_target = tmp_path / "config.json"
    captured: dict = {}

    async def _fake_pair(base_url, **kwargs):
        captured["base_url"] = base_url
        return {"access_token": "inv_x", "prefix": "inv_x"}

    async def _fake_run_agent(server, api_key, **kwargs):
        captured["agent_server"] = server

    monkeypatch.setattr("invincible.cli._pair_device", _fake_pair)
    monkeypatch.setattr("invincible.agent.runner.run_agent",
                        _fake_run_agent)
    result = CliRunner().invoke(
        agent_command, ["--server", "http://local.test:8000",
                        "--config", str(config_target)])
    assert result.exit_code == 0, result.output
    assert captured["base_url"] == "http://local.test:8000"
    assert captured["agent_server"] == "http://local.test:8000"


def test_agent_uses_saved_config_without_pairing(monkeypatch, tmp_path):
    """Saved credentials win: no pairing attempt, no first-run banner -
    the loop runs against the saved server with the saved key."""
    config_target = tmp_path / "config.json"
    _save_client_config(server="https://selfhost.example",
                        api_key="inv_saved", path=str(config_target))
    captured: dict = {}

    async def _must_not_pair(base_url, **kwargs):
        raise AssertionError("must not pair when credentials exist")

    async def _fake_run_agent(server, api_key, **kwargs):
        captured.update(agent_server=server, agent_key=api_key)

    monkeypatch.setattr("invincible.cli._pair_device", _must_not_pair)
    monkeypatch.setattr("invincible.agent.runner.run_agent",
                        _fake_run_agent)
    result = CliRunner().invoke(
        agent_command, ["--config", str(config_target)])
    assert result.exit_code == 0, result.output
    assert captured == {"agent_server": "https://selfhost.example",
                        "agent_key": "inv_saved"}
    assert "isn't paired yet" not in result.output
    assert "Next: connect your AI" not in result.output


def test_agent_pairing_failure_exits_cleanly(monkeypatch, tmp_path):
    """Denied/expired pairing stops the command; the agent loop never
    starts with no credentials."""
    async def _failing(base_url, **kwargs):
        raise _DevicePairError("the request was denied.")

    ran: list = []

    async def _fake_run_agent(server, api_key, **kwargs):
        ran.append((server, api_key))

    monkeypatch.setattr("invincible.cli._pair_device", _failing)
    monkeypatch.setattr("invincible.agent.runner.run_agent",
                        _fake_run_agent)
    result = CliRunner().invoke(
        agent_command, ["--config", str(tmp_path / "config.json")])
    assert result.exit_code != 0
    assert "Device pairing failed" in result.output
    assert not ran


def test_agent_corrupt_config_errors_instead_of_repairing(monkeypatch,
                                                          tmp_path):
    """A corrupt config file is surfaced, not silently re-paired over:
    only a MISSING file triggers self-pairing."""
    config_target = tmp_path / "config.json"
    config_target.write_text("{ not json", encoding="utf-8")

    async def _must_not_pair(base_url, **kwargs):
        raise AssertionError("must not pair over a corrupt config")

    monkeypatch.setattr("invincible.cli._pair_device", _must_not_pair)
    result = CliRunner().invoke(
        agent_command, ["--config", str(config_target)])
    assert result.exit_code != 0
    assert "Corrupt config" in result.output
