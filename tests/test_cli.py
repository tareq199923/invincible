import io
import os
import subprocess
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from invincible import __version__
from invincible.cli import cli
from invincible.core.oauth_store import OAuthStore
from invincible.core.router import load_providers_config
from tests.conftest import TEST_DB_URL


def _env_dict(text):
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return values


# --- CLI registration ---

def test_cli_help():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output
    assert "setup" in result.output
    assert "start" in result.output
    assert "secret" in result.output
    assert "oauth" in result.output


def test_cli_version():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_subcommand_help():
    for sub in ("setup", "start", "secret", "oauth"):
        result = CliRunner().invoke(cli, [sub, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_pyproject_declares_both_console_scripts():
    tomllib = pytest.importorskip("tomllib")
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["invincible"] == "invincible.cli:cli"
    assert scripts["inv"] == "invincible.cli:cli"


# --- setup behavior ---

def test_setup_creates_env_file(tmp_path):
    target = tmp_path / ".env"
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)],
        input="nim-key\ngroq-key\nor-key\ngem-key\ntok-key\nagent-key\nskip\n",
    )
    assert result.exit_code == 0
    assert str(target) in result.output
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert set(values) == {
        "GATEWAY_API_KEY", "INVINCIBLE_OWNER_SECRET", "NVIDIA_API_KEY",
        "GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
        "TOKENROUTER_API_KEY", "AGENTROUTER_API_KEY",
        "INVINCIBLE_CREDENTIAL_KEY",
    }
    assert values["NVIDIA_API_KEY"] == "nim-key"
    assert values["GROQ_API_KEY"] == "groq-key"
    assert values["OPENROUTER_API_KEY"] == "or-key"
    assert values["GEMINI_API_KEY"] == "gem-key"
    assert values["TOKENROUTER_API_KEY"] == "tok-key"
    assert values["AGENTROUTER_API_KEY"] == "agent-key"


def test_setup_generates_gateway_and_owner_secrets_without_printing(tmp_path):
    target = tmp_path / ".env"
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="\n" * 6 + "skip\n"
    )
    assert result.exit_code == 0
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["GATEWAY_API_KEY"]
    assert values["INVINCIBLE_OWNER_SECRET"]
    assert values["GATEWAY_API_KEY"] != values["INVINCIBLE_OWNER_SECRET"]
    # Empty input skips the provider keys...
    assert "NVIDIA_API_KEY" not in values
    assert "GEMINI_API_KEY" not in values
    assert "AGENTROUTER_API_KEY" not in values
    # ...and the generated secrets never reach the terminal.
    assert values["GATEWAY_API_KEY"] not in result.output
    assert values["INVINCIBLE_OWNER_SECRET"] not in result.output


def test_setup_preserves_existing_values(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "GATEWAY_API_KEY=gw-1\nINVINCIBLE_OWNER_SECRET=owner-1\nNVIDIA_API_KEY=nim-1\n"
        "GROQ_API_KEY=groq-1\nOPENROUTER_API_KEY=or-1\nGEMINI_API_KEY=gem-1\n"
        "TOKENROUTER_API_KEY=tok-1\nAGENTROUTER_API_KEY=agent-1\n"
        "INVINCIBLE_CREDENTIAL_KEY=cred-1\n",
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="skip\n"
    )
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8") == before


def test_setup_carries_legacy_mcp_shared_secret_into_new_key(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "GATEWAY_API_KEY=gw-1\nMCP_SHARED_SECRET=old-mcp\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="\n" * 6 + "skip\n"
    )
    assert result.exit_code == 0
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["INVINCIBLE_OWNER_SECRET"] == "old-mcp"
    assert "Carried MCP_SHARED_SECRET over" in result.output


def test_setup_preserves_unrelated_vars_comments_and_blank_lines(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        'CUSTOM_SETTING=value\n# Existing comment\nANOTHER_VARIABLE=test\n'
        'QUOTED="keep me"\n\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)],
        input="k1\nk2\nk3\nk4\nk5\nk6\nskip\n",
    )
    assert result.exit_code == 0
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "CUSTOM_SETTING=value"
    assert lines[1] == "# Existing comment"
    assert lines[2] == "ANOTHER_VARIABLE=test"
    assert lines[3] == 'QUOTED="keep me"'
    values = _env_dict("\n".join(lines))
    assert values["CUSTOM_SETTING"] == "value"
    assert values["ANOTHER_VARIABLE"] == "test"
    assert values["QUOTED"] == '"keep me"'
    assert values["GATEWAY_API_KEY"] and values["INVINCIBLE_OWNER_SECRET"]


def test_setup_preserves_unicode_comments_and_values(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "# notes in 中文 and emoji 🚀\nCUSTOM_SETTING=café\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)],
        input="k1\nk2\nk3\nk4\nk5\nk6\nskip\n",
    )
    assert result.exit_code == 0
    text = target.read_text(encoding="utf-8")
    assert "# notes in 中文 and emoji 🚀" in text
    assert "CUSTOM_SETTING=café" in text


def test_setup_repeated_runs_do_not_duplicate_keys(tmp_path):
    target = tmp_path / ".env"
    first = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)],
        input="k1\nk2\nk3\nk4\nk5\nk6\nskip\n",
    )
    assert first.exit_code == 0
    second = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="skip\n"
    )
    assert second.exit_code == 0
    text = target.read_text(encoding="utf-8")
    for key in ("GATEWAY_API_KEY", "INVINCIBLE_OWNER_SECRET", "NVIDIA_API_KEY",
                "GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
                "TOKENROUTER_API_KEY", "AGENTROUTER_API_KEY"):
        assert text.count(f"{key}=") == 1


def test_setup_force_updates_values(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "GATEWAY_API_KEY=old-gw\nINVINCIBLE_OWNER_SECRET=old-owner\nNVIDIA_API_KEY=old-nim\n"
        "GROQ_API_KEY=old-groq\nOPENROUTER_API_KEY=old-or\nGEMINI_API_KEY=old-gem\n"
        "TOKENROUTER_API_KEY=old-tok\nAGENTROUTER_API_KEY=old-agent\n"
        "INVINCIBLE_CREDENTIAL_KEY=old-cred\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target), "--force"],
        input=(
            "new-gw\nnew-owner\nnew-nim\nnew-groq\nnew-or\nnew-gem\nnew-tok\n"
            "new-agent\nskip\n"
        ),
    )
    assert result.exit_code == 0
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values == {
        "GATEWAY_API_KEY": "new-gw",
        "INVINCIBLE_OWNER_SECRET": "new-owner",
        "NVIDIA_API_KEY": "new-nim",
        "GROQ_API_KEY": "new-groq",
        "OPENROUTER_API_KEY": "new-or",
        "GEMINI_API_KEY": "new-gem",
        "TOKENROUTER_API_KEY": "new-tok",
        "AGENTROUTER_API_KEY": "new-agent",
        # Rotation would orphan every stored BYOK credential - even
        # --force must keep the credential key exactly as it was.
        "INVINCIBLE_CREDENTIAL_KEY": "old-cred",
    }


def test_setup_force_empty_input_preserves_existing(tmp_path):
    target = tmp_path / ".env"
    target.write_text("GEMINI_API_KEY=keep-me\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target), "--force"], input="\n" * 6 + "skip\n"
    )
    assert result.exit_code == 0
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["GEMINI_API_KEY"] == "keep-me"
    assert values["GATEWAY_API_KEY"] and values["INVINCIBLE_OWNER_SECRET"]


def test_setup_write_failure_returns_nonzero(tmp_path):
    target = tmp_path / "missing-dir" / ".env"
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)],
        input="k1\nk2\nk3\nk4\nk5\nk6\nskip\n",
    )
    assert result.exit_code == 1
    assert "Could not write env file" in result.output


# --- secret rotate behavior ---

def test_secret_rotate_replaces_owner_secret_with_new_value(tmp_path):
    target = tmp_path / ".env"
    target.write_text("INVINCIBLE_OWNER_SECRET=old-owner\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["secret", "rotate", "--env-file", str(target)])
    assert result.exit_code == 0
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["INVINCIBLE_OWNER_SECRET"]
    assert values["INVINCIBLE_OWNER_SECRET"] != "old-owner"
    assert "New owner secret generated and saved" in result.output


def test_secret_rotate_preserves_other_lines_comments_and_order(tmp_path):
    target = tmp_path / ".env"
    before = (
        "# Top comment\n"
        "GATEWAY_API_KEY=gw-1\n"
        "CUSTOM_SETTING=value\n"
        "INVINCIBLE_OWNER_SECRET=old-owner\n"
        "# trailing note\n"
        'QUOTED="keep me"\n'
        "\n"
    )
    target.write_text(before, encoding="utf-8")
    result = CliRunner().invoke(cli, ["secret", "rotate", "--env-file", str(target)])
    assert result.exit_code == 0
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# Top comment"
    assert lines[1] == "GATEWAY_API_KEY=gw-1"
    assert lines[2] == "CUSTOM_SETTING=value"
    assert lines[3].startswith("INVINCIBLE_OWNER_SECRET=")
    assert lines[3] != "INVINCIBLE_OWNER_SECRET=old-owner"
    assert lines[4] == "# trailing note"
    assert lines[5] == 'QUOTED="keep me"'
    assert lines[6] == ""
    assert target.read_text(encoding="utf-8") != before


def test_secret_rotate_migrates_legacy_mcp_shared_secret(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "GATEWAY_API_KEY=gw-1\nMCP_SHARED_SECRET=old-mcp\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli, ["secret", "rotate", "--env-file", str(target)])
    assert result.exit_code == 0
    text = target.read_text(encoding="utf-8")
    assert "INVINCIBLE_OWNER_SECRET=" in text
    assert "MCP_SHARED_SECRET" not in text
    values = _env_dict(text)
    assert values["INVINCIBLE_OWNER_SECRET"] != "old-mcp"
    assert values["GATEWAY_API_KEY"] == "gw-1"


def test_secret_rotate_missing_env_file_guides_to_setup(tmp_path):
    target = tmp_path / ".env"
    result = CliRunner().invoke(cli, ["secret", "rotate", "--env-file", str(target)])
    assert result.exit_code == 1
    assert "invincible setup" in result.output
    assert not target.exists()


def test_secret_rotate_env_file_without_owner_secret_guides_to_setup(tmp_path):
    target = tmp_path / ".env"
    target.write_text("GATEWAY_API_KEY=gw-1\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["secret", "rotate", "--env-file", str(target)])
    assert result.exit_code == 1
    assert "invincible setup" in result.output
    assert target.read_text(encoding="utf-8") == "GATEWAY_API_KEY=gw-1\n"


def test_secret_rotate_hides_value_unless_show_flag(tmp_path):
    target = tmp_path / ".env"
    target.write_text("INVINCIBLE_OWNER_SECRET=old-owner\n", encoding="utf-8")

    hidden = CliRunner().invoke(cli, ["secret", "rotate", "--env-file", str(target)])
    assert hidden.exit_code == 0
    assert "old-owner" not in hidden.output
    values = _env_dict(target.read_text(encoding="utf-8"))
    first_value = values["INVINCIBLE_OWNER_SECRET"]
    assert first_value not in hidden.output

    shown = CliRunner().invoke(
        cli, ["secret", "rotate", "--env-file", str(target), "--show"]
    )
    assert shown.exit_code == 0
    values = _env_dict(target.read_text(encoding="utf-8"))
    second_value = values["INVINCIBLE_OWNER_SECRET"]
    assert second_value != first_value
    assert f"INVINCIBLE_OWNER_SECRET={second_value}" in shown.output


# --- start behavior ---

def _fake_run(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("invincible.cli.uvicorn.run", fake_run)
    return calls


class _FakeProc:
    """Fake subprocess.Popen result: controllable poll/wait/terminate/kill."""

    def __init__(self, stdout_text="", exit_code=0, running=True):
        self.stdout = io.StringIO(stdout_text)
        self.exit_code = exit_code
        self.running = running
        self.terminate_called = False
        self.kill_called = False
        self.waits = []

    def poll(self):
        return None if self.running else self.exit_code

    def terminate(self):
        self.terminate_called = True
        self.running = False

    def kill(self):
        self.kill_called = True
        self.running = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return None if self.running else self.exit_code


class _HungProc(_FakeProc):
    """Fake proc whose wait() always times out and whose kill() raises:
    exercises every exception guard in _stop_tunnel."""

    def wait(self, timeout=None):
        self.waits.append(timeout)
        raise subprocess.TimeoutExpired(cmd="cloudflared", timeout=timeout)

    def kill(self):
        self.kill_called = True
        raise OSError("kill raced with process exit")


def _fake_cloudflared(monkeypatch, proc_factory=_FakeProc):
    """Point `inv start` at a fake cloudflared binary and record spawns.

    Returns (created, procs) where `created` is the list of recording
    threading.Thread instances (join to synchronize reader output) and
    `procs` is a list of (args, kwargs, proc) tuples from Popen.
    """
    created = []
    real_thread = threading.Thread

    class RecordingThread(real_thread):
        def __init__(self, *args, **kwargs):
            created.append(self)
            super().__init__(*args, **kwargs)

    procs = []

    def fake_popen(args, **kwargs):
        proc = proc_factory()
        procs.append((args, kwargs, proc))
        return proc

    monkeypatch.setattr("invincible.cli.shutil.which", lambda name: "cloudflared")
    monkeypatch.setattr("invincible.cli.subprocess.Popen", fake_popen)
    monkeypatch.setattr("invincible.cli.threading.Thread", RecordingThread)
    return created, procs


def test_start_spawns_and_stops_tunnel(monkeypatch, tmp_path):
    calls = _fake_run(monkeypatch)
    _, procs = _fake_cloudflared(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    assert calls
    assert len(procs) == 1
    args, kwargs, proc = procs[0]
    assert args == ["cloudflared", "tunnel", "run", "invincible"]
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["text"] is True
    assert "Starting Cloudflare tunnel 'invincible'..." in result.output
    assert "Cloudflare tunnel stopped." in result.output
    assert proc.terminate_called
    assert not proc.kill_called
    assert proc.waits == [5]


def test_start_stops_tunnel_on_keyboard_interrupt(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr("invincible.cli.uvicorn.run", fake_run)
    _, procs = _fake_cloudflared(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    assert "Shutting down" in result.output
    assert procs[0][2].terminate_called


def test_start_stops_tunnel_on_server_crash(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise SystemExit(1)

    monkeypatch.setattr("invincible.cli.uvicorn.run", fake_run)
    _, procs = _fake_cloudflared(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 1
    assert procs[0][2].terminate_called
    assert not procs[0][2].kill_called


def test_start_stops_hung_tunnel_without_raising(monkeypatch, tmp_path):
    """A tunnel that never exits cleanly must not crash or hang shutdown:
    wait() times out, kill() races the exit - _stop_tunnel still survives
    and the command exits 0."""
    _fake_run(monkeypatch)
    _, procs = _fake_cloudflared(monkeypatch, proc_factory=_HungProc)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    _, _, proc = procs[0]
    assert proc.terminate_called
    assert proc.kill_called
    assert "Cloudflare tunnel stopped." in result.output


def test_start_warns_when_tunnel_exits_prematurely(monkeypatch, tmp_path):
    """A tunnel that dies at startup (bad credentials, tunnel deleted)
    must be reported immediately instead of surfacing as a 502 later."""
    calls = _fake_run(monkeypatch)
    error_text = "2026-08-18T10:00:00Z ERR unable to find tunnel 'invincible'\n"
    created, procs = _fake_cloudflared(
        monkeypatch,
        proc_factory=lambda: _FakeProc(
            stdout_text=error_text, exit_code=1, running=False
        ),
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    for thread in created:
        thread.join()
    assert result.exit_code == 0
    assert calls
    assert procs[0][2].terminate_called is False
    assert "[tunnel] 2026-08-18T10:00:00Z ERR unable to find tunnel" in result.output
    assert "Cloudflare tunnel exited (code 1)" in result.output
    assert "the server is still running locally" in result.output
    assert "Cloudflare tunnel stopped." not in result.output


def test_start_reports_tunnel_connected(monkeypatch, tmp_path):
    """The reader thread echoes cloudflared's registration line so the
    operator can see the tunnel is live (and its URL)."""
    _fake_run(monkeypatch)
    stdout_text = (
        "2026-08-18T10:00:00Z INF Registered tunnel connection "
        "connIndex=0 originUrl=https://invincible.example.com\n"
        "You can visit your tunnel at: https://invincible.example.com\n"
    )
    created, procs = _fake_cloudflared(
        monkeypatch, proc_factory=lambda: _FakeProc(stdout_text=stdout_text)
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    for thread in created:
        thread.join()
    assert result.exit_code == 0
    assert procs[0][2].terminate_called
    assert "[tunnel] Cloudflare tunnel connected." in result.output
    assert "Tunnel URL: You can visit your tunnel at:" in result.output
    assert "https://invincible.example.com" in result.output


def test_start_no_tunnel_skips_spawn(monkeypatch, tmp_path):
    calls = _fake_run(monkeypatch)
    created, procs = _fake_cloudflared(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start", "--no-tunnel"])
    assert result.exit_code == 0
    assert calls
    assert procs == []
    assert created == []
    assert "cloudflared" not in result.output


def test_start_tunnel_custom_name(monkeypatch, tmp_path):
    _fake_run(monkeypatch)
    _, procs = _fake_cloudflared(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start", "--tunnel-name", "my-tunnel"])
    assert result.exit_code == 0
    assert procs[0][0] == ["cloudflared", "tunnel", "run", "my-tunnel"]


def test_start_tunnel_name_env_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("INVINCIBLE_TUNNEL_NAME", "env-tunnel")
    _fake_run(monkeypatch)
    _, procs = _fake_cloudflared(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    assert procs[0][0] == ["cloudflared", "tunnel", "run", "env-tunnel"]


def test_start_warns_when_cloudflared_missing(monkeypatch, tmp_path):
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr("invincible.cli.shutil.which", lambda name: None)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    assert calls
    assert "cloudflared not found on PATH" in result.output
    assert "--no-tunnel" in result.output


def test_start_warns_when_popen_fails(monkeypatch, tmp_path):
    """Popen raising OSError (TOCTOU race after which()) must degrade to a
    warning instead of crashing `inv start` before cleanup exists."""
    calls = _fake_run(monkeypatch)
    monkeypatch.setattr("invincible.cli.shutil.which", lambda name: "cloudflared")

    def boom(*args, **kwargs):
        raise OSError("binary vanished")

    monkeypatch.setattr("invincible.cli.subprocess.Popen", boom)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    assert calls
    assert "could not start cloudflared" in result.output
    assert "binary vanished" in result.output
    assert "Cloudflare tunnel stopped." not in result.output


def test_start_invokes_uvicorn_run_with_defaults(monkeypatch, tmp_path):
    calls = _fake_run(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "invincible.main:app"
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 8000,
        "reload": False,
        "log_level": "info",
    }


def test_start_passes_custom_options(monkeypatch, tmp_path):
    calls = _fake_run(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "start",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--reload",
            "--log-level",
            "debug",
        ],
    )
    assert result.exit_code == 0
    args, kwargs = calls[0]
    assert args[0] == "invincible.main:app"
    assert kwargs == {
        "host": "0.0.0.0",
        "port": 8080,
        "reload": True,
        "log_level": "debug",
    }


@pytest.mark.parametrize("port", ["0", "-1", "70000"])
def test_start_invalid_port_fails_cleanly(monkeypatch, port):
    calls = _fake_run(monkeypatch)
    result = CliRunner().invoke(cli, ["start", "--port", port])
    assert result.exit_code == 1
    assert "between 1 and 65535" in result.output
    assert calls == []


def test_start_loads_selected_env_file_before_startup(monkeypatch, tmp_path):
    marker = "INVINCIBLE_CLI_TEST_MARKER"
    monkeypatch.delenv(marker, raising=False)
    env_file = tmp_path / ".env.test"
    env_file.write_text(f"{marker}=loaded\n", encoding="utf-8")
    seen = {}

    def fake_run(*args, **kwargs):
        seen["marker"] = os.environ.get(marker)

    monkeypatch.setattr("invincible.cli.uvicorn.run", fake_run)
    result = CliRunner().invoke(cli, ["start", "--env-file", str(env_file)])
    assert result.exit_code == 0
    assert seen["marker"] == "loaded"
    assert "Loaded environment from" in result.output


def test_start_missing_env_file_warns_without_secrets(monkeypatch, tmp_path):
    calls = _fake_run(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    assert calls
    assert "Warning" in result.output
    assert "not found" in result.output
    assert "API_KEY=" not in result.output


def test_start_propagates_custom_config(monkeypatch, tmp_path):
    config = tmp_path / "custom.yaml"
    config.write_text(
        "providers:\n  - name: solo\n    tier: 1\n    base_url: https://solo.example.com/v1\n"
        "    api_key_env: SOLO_API_KEY\n    model_id: solo-model\n",
        encoding="utf-8",
    )
    seen = {}

    def fake_run(*args, **kwargs):
        seen["config"] = os.environ.get("INVINCIBLE_CONFIG_PATH")

    monkeypatch.setattr("invincible.cli.uvicorn.run", fake_run)
    monkeypatch.delenv("INVINCIBLE_CONFIG_PATH", raising=False)
    result = CliRunner().invoke(cli, ["start", "--config", str(config)])
    assert result.exit_code == 0
    assert seen["config"] == str(config)
    # `start` writes this straight into the process environment; monkeypatch
    # cannot restore it (delenv on an absent var records nothing), so clean
    # up explicitly to avoid leaking into later tests.
    os.environ.pop("INVINCIBLE_CONFIG_PATH", None)


def test_start_has_no_db_path_option():
    """Phase 16: INVINCIBLE_DB_URL (env/.env) is the only database source -
    the DSN is secret-bearing, so there is deliberately no --db-path flag."""
    result = CliRunner().invoke(cli, ["start", "--help"])
    assert result.exit_code == 0
    assert "--db-path" not in result.output


def test_start_missing_config_fails_cleanly(monkeypatch, tmp_path):
    calls = _fake_run(monkeypatch)
    result = CliRunner().invoke(cli, ["start", "--config", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 1
    assert "not found" in result.output
    assert calls == []


def test_start_keyboard_interrupt_is_clean(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr("invincible.cli.uvicorn.run", fake_run)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["start"])
    assert result.exit_code == 0
    assert "Shutting down" in result.output


# --- package resource behavior ---

def test_packaged_providers_config_loads_from_any_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_providers_config()
    providers = config["providers"]
    # Structural contract keyed by stable identifiers (api_key_env/tier);
    # names and model_ids rotate with the free-tier lineup.
    assert {p["api_key_env"] for p in providers} == {
        "AGENTROUTER_API_KEY", "TOKENROUTER_API_KEY", "NVIDIA_API_KEY",
        "GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
    }
    assert [p["tier"] for p in sorted(providers, key=lambda p: p["tier"])] == [
        1, 2, 3, 4, 5, 6,
    ]
    agentrouter = next(
        p for p in providers if p["api_key_env"] == "AGENTROUTER_API_KEY"
    )
    assert agentrouter["base_url"] == "https://agentrouter.org/v1"
    assert agentrouter["tier"] == 1
    tokenrouter = next(
        p for p in providers if p["api_key_env"] == "TOKENROUTER_API_KEY"
    )
    assert tokenrouter["base_url"] == "https://api.tokenrouter.com/v1"
    assert tokenrouter["tier"] == 2
    nim = next(p for p in providers if p["api_key_env"] == "NVIDIA_API_KEY")
    assert nim["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert nim["aliases"] == ["strong"]
    assert nim["tier"] == 3
    assert next(p for p in providers if p["name"] == "gemini-flash")["tier"] == 6


def test_custom_provider_config_path_still_works(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text(
        "providers:\n  - name: solo\n    tier: 1\n    base_url: https://solo.example.com/v1\n"
        "    api_key_env: SOLO_API_KEY\n    model_id: solo-model\n",
        encoding="utf-8",
    )
    config = load_providers_config(str(path))
    assert [p["name"] for p in config["providers"]] == ["solo"]


def test_missing_custom_config_raises_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        load_providers_config(str(tmp_path / "nope.yaml"))


def test_malformed_custom_config_raises_clear_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("key: [1, 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed provider configuration"):
        load_providers_config(str(path))


def test_default_db_url_resolution_comes_from_env(monkeypatch):
    """The store layer takes an engine; the CLI resolves INVINCIBLE_DB_URL
    from the environment (or .env) - there is no path default anymore."""
    from invincible.cli import _resolve_db_url

    monkeypatch.delenv("INVINCIBLE_DB_URL", raising=False)
    with pytest.raises(Exception, match="invincible dev-db"):
        _resolve_db_url()
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    assert _resolve_db_url() == TEST_DB_URL


# --- setup database-backend step (Phase 16) ---
#
# Prompt-count notes: of SUPPORTED_ENV_KEYS only the six provider keys
# prompt when unset (GATEWAY_API_KEY / INVINCIBLE_OWNER_SECRET are
# auto-generated), so a fresh .env consumes exactly six input lines before
# the database-backend choice.


def test_setup_paste_branch_writes_validated_url(tmp_path):
    target = tmp_path / ".env"
    result = CliRunner().invoke(
        cli,
        ["setup", "--env-file", str(target)],
        input=(
            "\n" * 6
            + "paste\npostgresql+asyncpg://invincible:pw@db.example:5432/inv\n"
        ),
    )
    assert result.exit_code == 0, result.output
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert (
        values["INVINCIBLE_DB_URL"]
        == "postgresql+asyncpg://invincible:pw@db.example:5432/inv"
    )


def test_setup_paste_branch_normalizes_plain_postgres_scheme(tmp_path):
    target = tmp_path / ".env"
    result = CliRunner().invoke(
        cli,
        ["setup", "--env-file", str(target)],
        input=(
            "\n" * 6
            + "paste\npostgresql://invincible@127.0.0.1:5433/invincible\n"
        ),
    )
    assert result.exit_code == 0, result.output
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["INVINCIBLE_DB_URL"] == (
        "postgresql+asyncpg://invincible@127.0.0.1:5433/invincible"
    )
    assert "Normalized postgresql://" in result.output


def test_setup_paste_branch_rejects_unsupported_driver(tmp_path):
    target = tmp_path / ".env"
    result = CliRunner().invoke(
        cli,
        ["setup", "--env-file", str(target)],
        input=(
            "\n" * 6
            + "paste\npostgresql+psycopg2://invincible@db/inv\n"
              "postgresql+asyncpg://invincible@db/inv\n"
        ),
    )
    assert result.exit_code == 0, result.output
    assert "Unsupported driver 'postgresql+psycopg2'" in result.output
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["INVINCIBLE_DB_URL"] == (
        "postgresql+asyncpg://invincible@db/inv"
    )


def test_setup_skip_branch_omits_db_url(tmp_path):
    target = tmp_path / ".env"
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="\n" * 6 + "skip\n"
    )
    assert result.exit_code == 0
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert "INVINCIBLE_DB_URL" not in values


def test_setup_existing_db_url_not_reprompted(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    before = "GATEWAY_API_KEY=gw\nINVINCIBLE_DB_URL=postgresql+asyncpg://keep@db/x\n"
    target.write_text(before, encoding="utf-8")
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    # Gateway + DB URL exist and are kept verbatim; owner secret is newly
    # generated (absent from the file); provider keys prompt-skip; the DB
    # step must NOT appear.
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="\n" * 6
    )
    assert result.exit_code == 0, result.output
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["GATEWAY_API_KEY"] == "gw"
    assert values["INVINCIBLE_DB_URL"] == (
        "postgresql+asyncpg://keep@db/x"
    )
    assert values["INVINCIBLE_OWNER_SECRET"]


def test_setup_force_reprompts_db_choice(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "INVINCIBLE_DB_URL=postgresql+asyncpg://old@db/x\n",
        encoding="utf-8",
    )
    # --force: gateway + five provider keys prompt (owner regenerates
    # silently); empty answers keep/regenerate as usual. Then the DB step
    # reappears; 'skip' leaves the existing URL untouched.
    result = CliRunner().invoke(
        cli,
        ["setup", "--env-file", str(target), "--force"],
        input="\n" * 6 + "skip\n",
    )
    assert result.exit_code == 0, result.output
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["INVINCIBLE_DB_URL"] == "postgresql+asyncpg://old@db/x"


def test_setup_dev_db_branch_provisions_and_writes(tmp_path, monkeypatch):
    target = tmp_path / ".env"

    def fake_provision(port=5433):
        return (
            "postgresql+asyncpg://invincible@127.0.0.1:5433/invincible",
            ["found local Postgres", "database already present: 'invincible'"],
        )

    monkeypatch.setattr("invincible.cli._provision_dev_db", fake_provision)
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="\n" * 6 + "dev-db\n"
    )
    assert result.exit_code == 0, result.output
    assert "Provisioning a local development database..." in result.output
    assert "dev-db: found local Postgres" in result.output
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["INVINCIBLE_DB_URL"] == (
        "postgresql+asyncpg://invincible@127.0.0.1:5433/invincible"
    )


def test_setup_dev_db_failure_skips_url_gracefully(tmp_path, monkeypatch):
    from invincible.cli import DevDbError

    def boom(port=5433):
        raise DevDbError("no server, no docker")

    monkeypatch.setattr("invincible.cli._provision_dev_db", boom)
    target = tmp_path / ".env"
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="\n" * 6 + "dev-db\n"
    )
    assert result.exit_code == 0, result.output
    assert "Could not provision locally (no server, no docker)" in result.output
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert "INVINCIBLE_DB_URL" not in values


# --- oauth administration ---


@pytest.fixture
async def seeded_client_id(pg_engine):
    """Register a client + token pair on the shared test database."""
    store = OAuthStore(engine=pg_engine)
    registration = await store.register_client(
        ["http://localhost:9999/callback"], "seed-client"
    )
    await store.issue_token_pair(registration["client_id"])
    return registration["client_id"]


async def test_oauth_list_shows_clients_and_grants(
    seeded_client_id, monkeypatch
):
    client_id = seeded_client_id
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    result = CliRunner().invoke(cli, ["oauth", "list"])
    assert result.exit_code == 0, result.output
    # The shared test DB may hold clients from other tests; this one must
    # appear with its grants.
    assert client_id in result.output
    assert "seed-client" in result.output
    assert "http://localhost:9999/callback" in result.output
    assert "active access: expires" in result.output
    assert "active refresh: expires" in result.output


async def test_oauth_list_empty_db_is_quiet(pg_engine, monkeypatch):
    from sqlalchemy import delete

    from invincible.core.db import oauth_clients, oauth_codes, oauth_tokens

    async with pg_engine.begin() as conn:
        for table in (oauth_tokens, oauth_codes, oauth_clients):
            await conn.execute(delete(table))
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    result = CliRunner().invoke(cli, ["oauth", "list"])
    assert result.exit_code == 0
    assert "No registered OAuth clients." in result.output


def test_oauth_revoke_unknown_client_fails(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    result = CliRunner().invoke(cli, ["oauth", "revoke", "nope"])
    assert result.exit_code == 1
    assert "Unknown client id" in result.output


async def test_oauth_revoke_revokes_all_tokens(seeded_client_id, monkeypatch):
    client_id = seeded_client_id
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    result = CliRunner().invoke(cli, ["oauth", "revoke", "--", client_id])
    assert result.exit_code == 0
    assert f"Revoked 2 token(s) for client {client_id}." in result.output

    listed = CliRunner().invoke(cli, ["oauth", "list"])
    assert "revoked: 2" in listed.output


async def test_oauth_revoke_accepts_dash_prefixed_client_id(
    pg_engine, monkeypatch
):
    """Client ids are random URL-safe strings, so a generated id can start
    with '-' - which Click would otherwise parse as an option ("No such
    option", exit code 2). The id must follow a '--' separator.
    Deterministic regression for the CI flake that hit a random dashed id."""
    from sqlalchemy import insert

    from invincible.core.db import oauth_clients, oauth_tokens
    from invincible.core.oauth_store import _now

    dashed_id = "-dash-prefixed-id"
    async with pg_engine.begin() as conn:
        await conn.execute(
            insert(oauth_clients).values(
                client_id=dashed_id,
                client_name="dashed-client",
                redirect_uris=["http://localhost:9999/callback"],
                created_at=_now(),
            )
        )
        for token_type, ttl in (("access", 3600), ("refresh", 30 * 24 * 3600)):
            await conn.execute(
                insert(oauth_tokens).values(
                    token_hash=f"hash-{token_type}-dashed",
                    token_type=token_type,
                    client_id=dashed_id,
                    expires_at=_now() + ttl,
                    revoked=False,
                    created_at=_now(),
                )
            )
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)

    result = CliRunner().invoke(cli, ["oauth", "revoke", "--", dashed_id])
    assert result.exit_code == 0, result.output
    assert f"Revoked 2 token(s) for client {dashed_id}." in result.output

    listed = CliRunner().invoke(cli, ["oauth", "list"])
    assert "revoked: 2" in listed.output


def test_oauth_test_client_prints_bearer_curl(pg_engine, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_OWNER_SECRET", "owner-secret")
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    result = CliRunner().invoke(cli, ["oauth", "test-client"])
    assert result.exit_code == 0, result.output
    assert "client_id:" in result.output
    assert "access token expires in 3600s" in result.output
    assert 'curl -X POST http://127.0.0.1:8000/mcp' in result.output
    assert "Authorization: Bearer" in result.output
    assert "Full OAuth response" in result.output


def test_oauth_test_client_requires_owner_secret(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # isolate from any repo-root .env
    monkeypatch.delenv("INVINCIBLE_OWNER_SECRET", raising=False)
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    monkeypatch.setenv("INVINCIBLE_DB_URL", TEST_DB_URL)
    result = CliRunner().invoke(cli, ["oauth", "test-client"])
    assert result.exit_code == 1
    assert "INVINCIBLE_OWNER_SECRET is not set" in result.output


def test_oauth_test_client_requires_db_url(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # isolate from any repo-root .env
    monkeypatch.setenv("INVINCIBLE_OWNER_SECRET", "owner-secret")
    monkeypatch.delenv("INVINCIBLE_DB_URL", raising=False)
    result = CliRunner().invoke(cli, ["oauth", "test-client"])
    assert result.exit_code == 1
    assert "invincible dev-db" in result.output
