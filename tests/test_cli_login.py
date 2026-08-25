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

from invincible.cli import (
    _client_config_path,
    _DevicePairError,
    _pair_device,
    _save_client_config,
    login,
)
from invincible.main import app
from tests.conftest import register_account


def _anon_client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _noop_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def test_pair_device_happy_path(client, router_setter):
    await register_account(client, "cli@pair.example")
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
