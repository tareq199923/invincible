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
    assert "connecting (WS-first)" in result.output


def test_format_status_empty():
    text = _format_harness_status(
        {"agent_online": False, "machines": []})
    assert "Agent online: no" in text
    assert "harness connect" in text


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
