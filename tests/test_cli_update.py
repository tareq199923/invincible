"""Hermetic tests for `invincible update` (no network, no real pip)."""

import subprocess

import pytest
from click.testing import CliRunner

import invincible.cli as cli_mod
from invincible import __version__
from invincible.cli import cli


def _payload(*versions):
    return {
        "info": {"version": sorted(versions)[-1] if versions else __version__},
        "releases": {v: [] for v in versions},
    }


def _patch(monkeypatch, *, payload=None, editable=False):
    if payload is not None:
        monkeypatch.setattr(cli_mod, "_fetch_pypi_payload", lambda: payload)
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: editable)


def test_update_help_lists_options():
    result = CliRunner().invoke(cli, ["update", "--help"])
    assert result.exit_code == 0
    assert "--check" in result.output
    assert "--yes" in result.output
    assert "--target" in result.output


def test_update_up_to_date(monkeypatch):
    _patch(monkeypatch, payload=_payload(__version__), editable=False)
    result = CliRunner().invoke(cli, ["update", "--check"])
    assert result.exit_code == 0
    assert "Up to date" in result.output


def test_update_check_reports_behind(monkeypatch):
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=False)
    result = CliRunner().invoke(cli, ["update", "--check"])
    assert result.exit_code == 1
    assert "Update available" in result.output
    assert "99.0.0" in result.output


def test_update_check_ignores_prerelease_by_default(monkeypatch):
    _patch(
        monkeypatch,
        payload=_payload(__version__, "99.0.0a1"),
        editable=False,
    )
    result = CliRunner().invoke(cli, ["update", "--check"])
    assert result.exit_code == 0
    assert "Up to date" in result.output


def test_update_check_with_pre_sees_prerelease(monkeypatch):
    _patch(
        monkeypatch,
        payload=_payload(__version__, "99.0.0a1"),
        editable=False,
    )
    result = CliRunner().invoke(cli, ["update", "--check", "--pre"])
    assert result.exit_code == 1
    assert "99.0.0a1" in result.output


def test_update_yes_runs_pip(monkeypatch):
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=False)
    calls = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(argv, **kwargs):
        calls.append(argv)
        assert argv[:4] == [cli_mod.sys.executable, "-m", "pip", "install"]
        assert argv[-1] == "invincible-ai==99.0.0"
        assert kwargs.get("capture_output") is True
        return _Proc()

    monkeypatch.setattr(cli_mod.subprocess, "run", _run)
    result = CliRunner().invoke(cli, ["update", "--yes"])
    assert result.exit_code == 0, result.output
    assert calls, "pip upgrade was not invoked"
    assert "Installed invincible-ai 99.0.0" in result.output


def test_update_target_installs_exact(monkeypatch):
    # _fetch must NOT be consulted when --target is given.
    def _boom():
        raise AssertionError("network should not be used with --target")

    monkeypatch.setattr(cli_mod, "_fetch_pypi_payload", _boom)
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: False)

    seen = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        cli_mod.subprocess, "run",
        lambda argv, **kw: (seen.append(argv), _Proc())[1],
    )
    result = CliRunner().invoke(cli, ["update", "--yes", "--target", "99.0.0"])
    assert result.exit_code == 0, result.output
    assert seen[0][-1] == "invincible-ai==99.0.0"


def test_update_invalid_target(monkeypatch):
    _patch(monkeypatch, editable=False)
    result = CliRunner().invoke(cli, ["update", "--yes", "--target", "not-a-ver!!"])
    assert result.exit_code != 0
    assert "Invalid --target" in result.output


def test_update_refuses_editable(monkeypatch):
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=True)
    result = CliRunner().invoke(cli, ["update", "--yes"])
    assert result.exit_code != 0
    assert "editable install" in result.output


def test_update_non_interactive_needs_yes(monkeypatch):
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=False)
    monkeypatch.setattr(cli_mod.sys.stdin, "isatty", lambda: False)
    result = CliRunner().invoke(cli, ["update"])
    assert result.exit_code != 0
    assert "--yes" in result.output


def test_update_pip_failure(monkeypatch):
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=False)

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "ERROR: no matching distribution"

    monkeypatch.setattr(cli_mod.subprocess, "run", lambda *a, **k: _Proc())
    result = CliRunner().invoke(cli, ["update", "--yes"])
    assert result.exit_code != 0
    assert "pip upgrade failed" in result.output


def test_update_pip_exception(monkeypatch):
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=False)

    def _raise(*a, **k):
        raise subprocess.SubprocessError("boom")

    monkeypatch.setattr(cli_mod.subprocess, "run", _raise)
    result = CliRunner().invoke(cli, ["update", "--yes"])
    assert result.exit_code != 0
    assert "pip upgrade failed" in result.output


def test_update_fetch_failure(monkeypatch):
    def _fail():
        raise cli_mod.click.ClickException("Could not check PyPI for updates: down")

    monkeypatch.setattr(cli_mod, "_fetch_pypi_payload", _fail)
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: False)
    result = CliRunner().invoke(cli, ["update", "--check"])
    assert result.exit_code != 0
    assert "Could not check PyPI" in result.output


def test_select_desired_version_pure():
    payload = _payload("0.4.0", "0.5.0", "0.6.0b1")
    assert cli_mod._select_desired_version(payload, pre=False) == "0.5.0"
    assert cli_mod._select_desired_version(payload, pre=True) == "0.6.0b1"


def test_select_desired_version_no_releases():
    with pytest.raises(cli_mod.click.ClickException):
        cli_mod._select_desired_version({"info": {}, "releases": {}}, pre=False)
