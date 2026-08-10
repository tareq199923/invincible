import os
from pathlib import Path

import pytest
from click.testing import CliRunner

import invincible
from invincible import __version__
from invincible.cli import cli
from invincible.core.router import load_providers_config
from invincible.core.session_store import SessionStore


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


def test_cli_version():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_subcommand_help():
    for sub in ("setup", "start"):
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
        input="nim-key\ngroq-key\nor-key\ngem-key\n",
    )
    assert result.exit_code == 0
    assert str(target) in result.output
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert set(values) == {
        "GATEWAY_API_KEY", "MCP_SHARED_SECRET", "NVIDIA_API_KEY",
        "GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
    }
    assert values["NVIDIA_API_KEY"] == "nim-key"
    assert values["GROQ_API_KEY"] == "groq-key"
    assert values["OPENROUTER_API_KEY"] == "or-key"
    assert values["GEMINI_API_KEY"] == "gem-key"


def test_setup_generates_gateway_and_mcp_secrets_without_printing(tmp_path):
    target = tmp_path / ".env"
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="\n\n\n\n"
    )
    assert result.exit_code == 0
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["GATEWAY_API_KEY"]
    assert values["MCP_SHARED_SECRET"]
    assert values["GATEWAY_API_KEY"] != values["MCP_SHARED_SECRET"]
    # Empty input skips the provider keys...
    assert "NVIDIA_API_KEY" not in values
    assert "GEMINI_API_KEY" not in values
    # ...and the generated secrets never reach the terminal.
    assert values["GATEWAY_API_KEY"] not in result.output
    assert values["MCP_SHARED_SECRET"] not in result.output


def test_setup_preserves_existing_values(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "GATEWAY_API_KEY=gw-1\nMCP_SHARED_SECRET=mcp-1\nNVIDIA_API_KEY=nim-1\n"
        "GROQ_API_KEY=groq-1\nOPENROUTER_API_KEY=or-1\nGEMINI_API_KEY=gem-1\n",
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")
    result = CliRunner().invoke(cli, ["setup", "--env-file", str(target)], input="")
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8") == before


def test_setup_preserves_unrelated_vars_comments_and_blank_lines(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        'CUSTOM_SETTING=value\n# Existing comment\nANOTHER_VARIABLE=test\n'
        'QUOTED="keep me"\n\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="k1\nk2\nk3\nk4\n"
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
    assert values["GATEWAY_API_KEY"] and values["MCP_SHARED_SECRET"]


def test_setup_preserves_unicode_comments_and_values(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "# notes in 中文 and emoji 🚀\nCUSTOM_SETTING=café\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="k1\nk2\nk3\nk4\n"
    )
    assert result.exit_code == 0
    text = target.read_text(encoding="utf-8")
    assert "# notes in 中文 and emoji 🚀" in text
    assert "CUSTOM_SETTING=café" in text


def test_setup_repeated_runs_do_not_duplicate_keys(tmp_path):
    target = tmp_path / ".env"
    first = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="k1\nk2\nk3\nk4\n"
    )
    assert first.exit_code == 0
    second = CliRunner().invoke(cli, ["setup", "--env-file", str(target)], input="")
    assert second.exit_code == 0
    text = target.read_text(encoding="utf-8")
    for key in ("GATEWAY_API_KEY", "MCP_SHARED_SECRET", "NVIDIA_API_KEY",
                "GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY"):
        assert text.count(f"{key}=") == 1


def test_setup_force_updates_values(tmp_path):
    target = tmp_path / ".env"
    target.write_text(
        "GATEWAY_API_KEY=old-gw\nMCP_SHARED_SECRET=old-mcp\nNVIDIA_API_KEY=old-nim\n"
        "GROQ_API_KEY=old-groq\nOPENROUTER_API_KEY=old-or\nGEMINI_API_KEY=old-gem\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target), "--force"],
        input="new-gw\nnew-mcp\nnew-nim\nnew-groq\nnew-or\nnew-gem\n",
    )
    assert result.exit_code == 0
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values == {
        "GATEWAY_API_KEY": "new-gw",
        "MCP_SHARED_SECRET": "new-mcp",
        "NVIDIA_API_KEY": "new-nim",
        "GROQ_API_KEY": "new-groq",
        "OPENROUTER_API_KEY": "new-or",
        "GEMINI_API_KEY": "new-gem",
    }


def test_setup_force_empty_input_preserves_existing(tmp_path):
    target = tmp_path / ".env"
    target.write_text("GEMINI_API_KEY=keep-me\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target), "--force"], input="\n" * 5
    )
    assert result.exit_code == 0
    values = _env_dict(target.read_text(encoding="utf-8"))
    assert values["GEMINI_API_KEY"] == "keep-me"
    assert values["GATEWAY_API_KEY"] and values["MCP_SHARED_SECRET"]


def test_setup_write_failure_returns_nonzero(tmp_path):
    target = tmp_path / "missing-dir" / ".env"
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(target)], input="k1\nk2\nk3\nk4\n"
    )
    assert result.exit_code == 1
    assert "Could not write env file" in result.output


# --- start behavior ---

def _fake_run(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("invincible.cli.uvicorn.run", fake_run)
    return calls


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


def test_start_propagates_config_and_db_path(monkeypatch, tmp_path):
    config = tmp_path / "custom.yaml"
    config.write_text(
        "providers:\n  - name: solo\n    tier: 1\n    base_url: https://solo.example.com/v1\n"
        "    api_key_env: SOLO_API_KEY\n    model_id: solo-model\n",
        encoding="utf-8",
    )
    seen = {}

    def fake_run(*args, **kwargs):
        seen["config"] = os.environ.get("INVINCIBLE_CONFIG_PATH")
        seen["db"] = os.environ.get("INVINCIBLE_DB_PATH")

    monkeypatch.setattr("invincible.cli.uvicorn.run", fake_run)
    monkeypatch.delenv("INVINCIBLE_CONFIG_PATH", raising=False)
    monkeypatch.delenv("INVINCIBLE_DB_PATH", raising=False)
    db_target = tmp_path / "data" / "sessions.db"
    result = CliRunner().invoke(
        cli, ["start", "--config", str(config), "--db-path", str(db_target)]
    )
    assert result.exit_code == 0
    assert seen["config"] == str(config)
    assert seen["db"] == str(db_target)
    # `start` writes these straight into the process environment; monkeypatch
    # cannot restore them (delenv on an absent var records nothing), so clean
    # up explicitly to avoid leaking into later tests.
    os.environ.pop("INVINCIBLE_CONFIG_PATH", None)
    os.environ.pop("INVINCIBLE_DB_PATH", None)


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
    assert {p["name"] for p in providers} == {
        "nim-glm", "groq-llama", "openrouter-fallback", "gemini-flash",
    }
    assert [p["name"] for p in sorted(providers, key=lambda p: p["tier"])] == [
        "nim-glm", "groq-llama", "openrouter-fallback", "gemini-flash",
    ]
    nim = next(p for p in providers if p["name"] == "nim-glm")
    assert nim["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert nim["api_key_env"] == "NVIDIA_API_KEY"
    assert nim["model_id"] == "z-ai/glm-5.2"
    assert nim["tier"] == 1
    assert next(p for p in providers if p["name"] == "gemini-flash")["tier"] == 4


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


def test_default_db_path_is_cwd_and_not_inside_package(tmp_path, monkeypatch):
    monkeypatch.delenv("INVINCIBLE_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    store = SessionStore()
    assert store.db_path == str(tmp_path / "sessions.db")
    package_dir = os.path.dirname(os.path.abspath(invincible.__file__))
    assert package_dir not in store.db_path

    custom = tmp_path / "custom" / "sessions.db"
    monkeypatch.setenv("INVINCIBLE_DB_PATH", str(custom))
    assert SessionStore().db_path == str(custom)
    assert SessionStore(db_path=":memory:").db_path == ":memory:"
