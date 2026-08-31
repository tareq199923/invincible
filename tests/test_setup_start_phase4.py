# tests/test_setup_start_phase4.py
"""Phase 4: setup auto-generates the BYOK credential key (Q2: yes) and
`start` opens the dashboard (headless-guarded).

Gates: setup writes a valid Fernet INVINCIBLE_CREDENTIAL_KEY when absent
without echoing it; --force NEVER rotates an existing value (rotation
would orphan every stored BYOK credential); start schedules a browser
open only in attached sessions and honors --no-open-browser.
"""
from click.testing import CliRunner
from cryptography.fernet import Fernet

from invincible.cli import cli
from tests.test_cli import _fake_cloudflared, _fake_run


def _parse_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def test_setup_generates_credential_key_when_absent(tmp_path):
    target = tmp_path / ".env"
    result = CliRunner().invoke(
        cli,
        ["setup", "--env-file", str(target), "--skip-db-check",
         "--db-url", "postgresql+asyncpg://invincible:pw@db.example:5432/inv"])
    assert result.exit_code == 0, result.output

    values = _parse_env(target)
    # Valid Fernet key: usable as the BYOK master key immediately.
    Fernet(values["INVINCIBLE_CREDENTIAL_KEY"].encode("ascii"))
    # Secrets discipline: generated, never echoed.
    assert values["INVINCIBLE_CREDENTIAL_KEY"] not in result.output
    assert "BACK IT UP" in result.output


def test_setup_never_rotates_existing_credential_key_even_with_force(
    tmp_path,
):
    target = tmp_path / ".env"
    target.write_text(
        "INVINCIBLE_CREDENTIAL_KEY=keep-this-exact-value\n"
        "GATEWAY_API_KEY=gw\nINVINCIBLE_OWNER_SECRET=owner\n"
        "INVINCIBLE_DB_URL=postgresql+asyncpg://keep@db/x\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target), "--force"])
    assert result.exit_code == 0, result.output

    values = _parse_env(target)
    assert values["INVINCIBLE_CREDENTIAL_KEY"] == "keep-this-exact-value"


def test_start_opens_browser_in_attached_session(monkeypatch, tmp_path):
    calls = _fake_run(monkeypatch)
    _fake_cloudflared(monkeypatch)
    opened = []
    monkeypatch.setattr(
        "invincible.cli._browser_session_available", lambda: True)
    monkeypatch.setattr(
        "invincible.cli.webbrowser.open", lambda url: opened.append(url))

    class ImmediateTimer:
        def __init__(self, delay, fn, args=()):
            self._fn, self._args = fn, args

        def start(self):
            self._fn(*self._args)

    monkeypatch.setattr("invincible.cli.threading.Timer", ImmediateTimer)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    assert calls  # server actually ran
    # /dashboard, not the health-JSON root: anonymous browsers get the
    # login page instead of raw JSON.
    assert opened == ["http://127.0.0.1:8000/dashboard"]
    assert "Opening http://127.0.0.1:8000/dashboard" in result.output


def test_start_headless_session_prints_url_instead(monkeypatch, tmp_path):
    calls = _fake_run(monkeypatch)
    _fake_cloudflared(monkeypatch)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    assert calls
    assert "Headless session: open http://127.0.0.1:8000/dashboard" in result.output
    assert "Opening http://" not in result.output


def test_start_no_open_browser_flag_skips_entirely(monkeypatch, tmp_path):
    _fake_run(monkeypatch)
    _fake_cloudflared(monkeypatch)
    monkeypatch.setattr(
        "invincible.cli._browser_session_available", lambda: True)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["start", "--no-open-browser"])
    assert result.exit_code == 0
    assert "Opening http://" not in result.output
    assert "Headless session" not in result.output
