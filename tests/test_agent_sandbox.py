import os

import pytest

from invincible.agent import sandbox
from invincible.core.tool_executor import ToolBlocked


@pytest.fixture(autouse=True)
def real_home_root(monkeypatch, tmp_path):
    """Point the agent sandbox at a temp home so tests never touch the
    real one, and drop any INVINCIBLE_AGENT_ROOT override."""
    monkeypatch.delenv("INVINCIBLE_AGENT_ROOT", raising=False)
    monkeypatch.setenv("INVINCIBLE_AGENT_ROOT", str(tmp_path))
    return tmp_path


def _in(root, *parts):
    return os.path.join(str(root), *parts)


def test_read_inside_root_allowed(real_home_root):
    path = _in(real_home_root, "notes.txt")
    sandbox.check_agent_read(path)


def test_write_inside_root_allowed(real_home_root):
    sandbox.check_agent_write(_in(real_home_root, "project", "main.py"))


@pytest.mark.parametrize("name", [
    ".env", ".env.local", ".ENV", ".env.production",
])
def test_env_blocked_for_read_and_write(real_home_root, name):
    """Dot-prefixed .env* only - same anchoring as the server's
    WRITE_DENYLIST_PATTERNS (tool_executor). A file literally named
    prod.env is not a dotfile and is left to the user's judgment."""
    path = _in(real_home_root, name)
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(path)
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_write(path)


@pytest.mark.parametrize("parts", [
    (".ssh", "authorized_keys"),
    (".git", "config"),
    ("id_rsa",),
    ("id_ed25519",),
    ("server.pem",),
])
def test_sensitive_paths_blocked_both_verbs(real_home_root, parts):
    path = _in(real_home_root, *parts)
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(path)
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_write(path)


def test_basename_denylist_matches_any_component(real_home_root):
    """A .git inside a subdirectory, not just at the root."""
    path = _in(real_home_root, "work", "repo", ".git", "config")
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(path)


def test_credentials_name_blocked(real_home_root):
    path = _in(real_home_root, "aws", "credentials")
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(path)


def test_path_outside_root_blocked(real_home_root):
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(
            os.path.join(os.path.dirname(str(real_home_root)), "elsewhere.txt")
        )
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_write("C:/Windows/system32/evil.dll")


def test_agent_root_env_override(tmp_path, monkeypatch):
    other = tmp_path / "other-root"
    other.mkdir()
    monkeypatch.setenv("INVINCIBLE_AGENT_ROOT", str(other))
    sandbox.check_agent_read(str(other / "ok.txt"))
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(str(tmp_path / "outside.txt"))
