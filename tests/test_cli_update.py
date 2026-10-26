"""Hermetic tests for `invincible update` (no network, no real pip)."""

import json
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


# --- install kind + channel-aware upgrade guidance ---------------------------


def _kind(kind: str, upgrade: str | None) -> object:
    return cli_mod._InstallKind(kind, kind, upgrade)


def test_install_kind_editable(monkeypatch):
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: True)
    kind = cli_mod._install_kind()
    assert kind.kind == "editable"
    assert "git pull" in kind.upgrade


def test_install_kind_pipx(monkeypatch):
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli_mod.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(
        cli_mod.sys, "prefix",
        "/home/u/.local/pipx/venvs/invincible-ai")
    kind = cli_mod._install_kind()
    assert kind.kind == "pipx"
    assert kind.upgrade == "pipx upgrade invincible-ai"


def test_install_kind_uv_tool(monkeypatch):
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli_mod.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(
        cli_mod.sys, "prefix",
        r"C:\Users\me\AppData\Local\uv\tools\invincible-ai")
    kind = cli_mod._install_kind()
    assert kind.kind == "uv"
    assert kind.upgrade == "uv tool upgrade invincible-ai"


def test_install_kind_container(monkeypatch):
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: False)
    monkeypatch.setattr(
        cli_mod.os.path, "exists", lambda path: path == "/.dockerenv")
    kind = cli_mod._install_kind()
    assert kind.kind == "container"
    assert kind.upgrade is None


def test_install_kind_pip_points_at_this_interpreter(monkeypatch):
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli_mod.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(cli_mod.sys, "prefix", r"C:\Python314")
    monkeypatch.setattr(
        cli_mod.sys, "executable", r"C:\Python314\python.exe")
    kind = cli_mod._install_kind()
    assert kind.kind == "pip"
    assert kind.upgrade is not None
    assert r'"C:\Python314\python.exe" -m pip install -U invincible-ai' \
        in kind.upgrade


def test_update_refuses_pipx_and_names_the_tool(monkeypatch):
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=False)
    monkeypatch.setattr(
        cli_mod, "_install_kind",
        lambda: _kind("pipx", "pipx upgrade invincible-ai"))
    result = CliRunner().invoke(cli, ["update", "--yes"])
    assert result.exit_code != 0
    assert "managed by pipx" in result.output
    assert "pipx upgrade invincible-ai" in result.output


def test_update_refuses_uv_tool(monkeypatch):
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=False)
    monkeypatch.setattr(
        cli_mod, "_install_kind",
        lambda: _kind("uv", "uv tool upgrade invincible-ai"))
    result = CliRunner().invoke(cli, ["update", "--yes"])
    assert result.exit_code != 0
    assert "uv tool upgrade invincible-ai" in result.output


def test_update_refuses_container(monkeypatch):
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=False)
    monkeypatch.setattr(cli_mod, "_install_kind", lambda: _kind("container", None))
    result = CliRunner().invoke(cli, ["update", "--yes"])
    assert result.exit_code != 0
    assert "Rebuild the image" in result.output


def test_update_check_still_reports_for_managed_installs(monkeypatch):
    """--check is read-only: it reports even when in-place upgrade is refused."""
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=False)
    monkeypatch.setattr(
        cli_mod, "_install_kind",
        lambda: _kind("pipx", "pipx upgrade invincible-ai"))
    result = CliRunner().invoke(cli, ["update", "--check"])
    assert result.exit_code == 1
    assert "Update available" in result.output


def test_update_success_prints_next_steps_and_caches(monkeypatch, tmp_path):
    _patch(monkeypatch, payload=_payload(__version__, "99.0.0"), editable=False)
    monkeypatch.setattr(
        cli_mod, "_install_kind",
        lambda: _kind("pip", '"py" -m pip install -U invincible-ai'))
    cache = tmp_path / "update-check.json"
    monkeypatch.setattr(cli_mod, "_update_cache_path", lambda: str(cache))

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(cli_mod.subprocess, "run", lambda *a, **k: _Proc())
    result = CliRunner().invoke(cli, ["update", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Installed invincible-ai 99.0.0" in result.output
    assert "harness service restart" in result.output
    assert "db upgrade" in result.output
    assert json.loads(cache.read_text(encoding="utf-8"))["latest"] == "99.0.0"


# --- proactive notice (cached, opt-out, silent on failure) -------------------


class _TtyStringIO:
    """StringIO that pretends to be a terminal for the notice gate."""

    def __init__(self):
        import io

        self._stream = io.StringIO()

    def isatty(self):
        return True

    def write(self, text):
        return self._stream.write(text)

    def flush(self):
        return self._stream.flush()

    def getvalue(self):
        return self._stream.getvalue()


def _allow_notice(monkeypatch, stream):
    monkeypatch.delenv(cli_mod.NO_UPDATE_CHECK_ENV, raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(cli_mod.sys, "stderr", stream)
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: False)
    monkeypatch.setattr(
        cli_mod, "_install_kind",
        lambda: _kind("pip", '"py" -m pip install -U invincible-ai'))


def test_notice_prints_actionable_line(monkeypatch, tmp_path):
    stream = _TtyStringIO()
    _allow_notice(monkeypatch, stream)
    monkeypatch.setattr(
        cli_mod, "_update_cache_path",
        lambda: str(tmp_path / "update-check.json"))
    monkeypatch.setattr(
        cli_mod, "_fetch_pypi_payload",
        lambda timeout=10.0: _payload(__version__, "99.0.0"))
    cli_mod._maybe_notify_update()
    output = stream.getvalue()
    assert "Update available" in output
    assert "99.0.0" in output
    assert "pip install -U invincible-ai" in output


def test_notice_is_silent_on_fetch_failure(monkeypatch, tmp_path):
    stream = _TtyStringIO()
    _allow_notice(monkeypatch, stream)
    monkeypatch.setattr(
        cli_mod, "_update_cache_path",
        lambda: str(tmp_path / "update-check.json"))

    def _fail(timeout=10.0):
        raise RuntimeError("network down")

    monkeypatch.setattr(cli_mod, "_fetch_pypi_payload", _fail)
    cli_mod._maybe_notify_update()
    assert stream.getvalue() == ""


def test_notice_respects_opt_out(monkeypatch):
    stream = _TtyStringIO()
    _allow_notice(monkeypatch, stream)
    monkeypatch.setenv(cli_mod.NO_UPDATE_CHECK_ENV, "1")
    assert cli_mod._update_checks_enabled() is False


def test_notice_skips_ci(monkeypatch):
    stream = _TtyStringIO()
    _allow_notice(monkeypatch, stream)
    monkeypatch.setenv("CI", "1")
    assert cli_mod._update_checks_enabled() is False


def test_notice_skips_non_tty(monkeypatch):
    monkeypatch.delenv(cli_mod.NO_UPDATE_CHECK_ENV, raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: False)
    import io

    monkeypatch.setattr(cli_mod.sys, "stderr", io.StringIO())
    assert cli_mod._update_checks_enabled() is False


def test_notice_skips_editable(monkeypatch):
    stream = _TtyStringIO()
    _allow_notice(monkeypatch, stream)
    monkeypatch.setattr(cli_mod, "_is_editable_install", lambda: True)
    assert cli_mod._update_checks_enabled() is False


def test_client_status_block_reports_cached_update(monkeypatch, tmp_path):
    cache = tmp_path / "update-check.json"
    monkeypatch.setattr(cli_mod, "_update_cache_path", lambda: str(cache))
    monkeypatch.setattr(
        cli_mod, "_install_kind",
        lambda: _kind("pip", '"py" -m pip install -U invincible-ai'))
    cli_mod.update_core.write_cache(str(cache), latest="99.0.0")
    block = cli_mod._client_status_block()
    assert block["version"] == __version__
    assert block["install"] == "pip"
    assert block["update"] == "99.0.0"
    assert "pip install -U" in block["upgrade"]

