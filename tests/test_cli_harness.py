# tests/test_cli_harness.py
"""H6c: harness CLI — pure-function pins (no network, no pairing).

Network/pairing flows reuse _ensure_paired (the `agent`-discipline:
only a missing file pairs); these tests pin the rendering helpers and
group wiring instead.
"""
from click.testing import CliRunner

from invincible.cli import _format_harness_status, _render_agent_service, cli


def test_harness_group_registered():
    assert "harness" in cli.commands
    group = cli.commands["harness"]
    assert set(group.commands) >= {
        "setup", "connect", "status", "service"}


def test_connect_alias_delegates_to_harness_connect(monkeypatch, tmp_path):
    """Top-level `connect` is `harness connect` with a shorter spelling:
    same pairing discipline, same loop, same teaching line."""
    from invincible.cli import _save_client_config
    from invincible.cli import connect as connect_cmd

    config_target = tmp_path / "config.json"
    _save_client_config(server="https://selfhost.example",
                        api_key="inv_saved", path=str(config_target))
    captured: dict = {}

    async def _must_not_pair(base_url, **kwargs):
        raise AssertionError("must not pair when credentials exist")

    async def _fake_run(server, api_key, **kwargs):
        captured.update(server=server, api_key=api_key)

    monkeypatch.setattr("invincible.cli._pair_device", _must_not_pair)
    monkeypatch.setattr("invincible.agent.runner.run_harness", _fake_run)
    result = CliRunner().invoke(
        connect_cmd, ["--config", str(config_target)])
    assert result.exit_code == 0, result.output
    assert captured == {"server": "https://selfhost.example",
                        "api_key": "inv_saved"}
    assert "now online" in result.output


def test_format_status_empty():
    text = _format_harness_status(
        {"agent_online": False, "machines": []})
    assert "Agent online: no" in text
    assert "invincible connect" in text


def test_format_status_lists_machines():
    text = _format_harness_status({
        "agent_online": True,
        "machines": [
            {"machine_id": "m1", "machine_name": "laptop",
             "platform": "win", "online": True,
             "capabilities": {"chrome": True, "docker": False}},
            {"machine_id": "m2", "machine_name": "",
             "platform": "", "online": False, "capabilities": {}},
        ],
    })
    assert "Agent online: yes" in text
    assert "laptop" in text and "[m1]" in text and "online" in text
    assert "[m2]" in text and "offline" in text
    assert "chrome" in text and "caps: none" in text


def test_render_systemd_unit(tmp_path, monkeypatch):
    import os
    import sys

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    name, content = _render_agent_service(
        config_path=str(tmp_path / "config.json"))
    assert name == "invincible-agent.service"
    assert "harness" in content and "connect" in content
    assert "Restart=on-failure" in content
    assert str(tmp_path / "config.json") in content


def test_render_launchd_plist(tmp_path, monkeypatch):
    import os
    import sys

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    name, content = _render_agent_service(
        config_path=str(tmp_path / "config.json"))
    assert name == "me.invincible.agent.plist"
    assert "<key>RunAtLoad</key>" in content


def test_render_windows_schtasks(monkeypatch):
    import os

    monkeypatch.setattr(os, "name", "nt")
    name, content = _render_agent_service(config_path="C:\\c.json")
    assert name == "invincible-agent.xml"
    assert "schtasks" in content


def test_service_install_dry_run(tmp_path):
    result = CliRunner().invoke(cli, [
        "harness", "service", "install", "--dry-run",
        "--config", str(tmp_path / "config.json"),
    ])
    assert result.exit_code == 0, result.output
    assert "harness" in result.output


def test_setup_reprints_config_without_pairing(monkeypatch, tmp_path):
    """Already paired: `harness setup` re-prints the MCP block without
    pairing again and without starting any loop."""
    from invincible.cli import _save_client_config

    config_target = tmp_path / "config.json"
    _save_client_config(server="https://paired.example",
                        api_key="inv_saved", path=str(config_target))

    async def _must_not_pair(base_url, **kwargs):
        raise AssertionError("must not pair when credentials exist")

    monkeypatch.setattr("invincible.cli._pair_device", _must_not_pair)
    result = CliRunner().invoke(
        cli, ["harness", "setup", "--config", str(config_target)])
    assert result.exit_code == 0, result.output
    assert "mcpServers" in result.output
    assert "https://paired.example/mcp" in result.output


def test_format_status_includes_account_when_supplied():
    """The account block is additive: it renders above the machine list
    and only when the caller supplied it (pre-whoami payloads unchanged)."""
    text = _format_harness_status({
        "account": {"user_id": 7, "email": "me@example.com",
                    "project_id": 3, "key_prefix": "inv_abcdef12"},
        "agent_online": True,
        "machines": [],
    })
    assert "Account: me@example.com (user 7)" in text
    assert "Key: inv_abcdef12..." in text
    assert "Project: 3" in text
    assert text.index("Account:") < text.index("Agent online:")


def test_format_status_account_degrades_without_email():
    """A missing email or project never breaks the render."""
    text = _format_harness_status({
        "account": {"user_id": 7, "email": None, "project_id": None,
                    "key_prefix": ""},
        "agent_online": False,
        "machines": [],
    })
    assert "Account: unknown (user 7)" in text
    assert "Key:" not in text and "Project:" not in text


def _paired_config(tmp_path):
    from invincible.cli import _save_client_config

    target = tmp_path / "config.json"
    _save_client_config(server="https://paired.example",
                        api_key="inv_saved", path=str(target))
    return target


def test_status_command_renders_account_above_machines(monkeypatch, tmp_path):
    """`harness status` asks whoami + machines and prints the account
    line above the machine list (hermetic: no server, no DB)."""
    import httpx

    seen: list[str] = []

    def _fake_get(url, **kwargs):
        seen.append(url)
        if url.endswith("/agent/whoami"):
            return httpx.Response(200, request=httpx.Request("GET", url),
                                  json={"user_id": 7,
                                        "email": "me@example.com",
                                        "project_id": 3,
                                        "key_prefix": "inv_abcdef12"})
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "machines": [{"machine_id": "m1", "machine_name": "laptop",
                          "platform": "win", "online": True,
                          "capabilities": {"chrome": True}}]})

    monkeypatch.setattr(httpx, "get", _fake_get)
    result = CliRunner().invoke(cli, [
        "harness", "status", "--config", str(_paired_config(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "Account: me@example.com (user 7)" in result.output
    assert "inv_abcdef12" in result.output
    assert "laptop" in result.output
    assert seen == ["https://paired.example/agent/whoami",
                    "https://paired.example/agent/machines"]


def test_status_command_tolerates_server_without_whoami(monkeypatch, tmp_path):
    """An older server (no /agent/whoami -> 404) still yields the machine
    list instead of failing the whole command."""
    import httpx

    def _fake_get(url, **kwargs):
        if url.endswith("/agent/whoami"):
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "machines": [{"machine_id": "m1", "machine_name": "laptop",
                          "platform": "win", "online": True,
                          "capabilities": {}}]})

    monkeypatch.setattr(httpx, "get", _fake_get)
    result = CliRunner().invoke(cli, [
        "harness", "status", "--config", str(_paired_config(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "Account:" not in result.output
    assert "laptop" in result.output


def test_status_command_401_teaches_re_pairing(monkeypatch, tmp_path):
    """A rejected pairing key is a loud, actionable failure - the same
    message for the whoami and machines calls."""
    import httpx

    def _fake_get(url, **kwargs):
        return httpx.Response(401, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _fake_get)
    result = CliRunner().invoke(cli, [
        "harness", "status", "--config", str(_paired_config(tmp_path))])
    assert result.exit_code != 0
    assert "invincible login" in result.output
