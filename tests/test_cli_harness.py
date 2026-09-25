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
