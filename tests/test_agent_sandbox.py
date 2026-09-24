import contextlib
import os
import subprocess
import tempfile
from functools import lru_cache

import pytest

from invincible.agent import sandbox
from invincible.core.tool_executor import ToolBlocked


def _make_dir_link(link: str, target: str) -> bool:
    """Create a DIRECTORY link at ``link`` pointing at ``target``.

    Tries a symlink, then a Windows directory JUNCTION. The junction
    fallback is not a convenience: a junction needs neither admin rights
    nor Developer Mode, which makes it the likelier link to exist on a
    real Windows machine - so it is the likelier attack too.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            capture_output=True,
        )
        return result.returncode == 0 and os.path.exists(link)
    return False


@lru_cache(maxsize=1)
def _dir_links_supported() -> bool:
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "t")
        os.makedirs(target)
        return _make_dir_link(os.path.join(d, "l"), target)


@lru_cache(maxsize=1)
def _symlinks_supported() -> bool:
    """FILE symlinks. Windows refuses these without Developer Mode or
    admin, where junctions are only ever directories."""
    try:
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "t")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x")
            os.symlink(target, os.path.join(d, "l"))
        return True
    except (OSError, NotImplementedError):
        return False


requires_dir_links = pytest.mark.skipif(
    not _dir_links_supported(),
    reason="this OS/user cannot create directory links",
)
requires_file_symlinks = pytest.mark.skipif(
    not _symlinks_supported(),
    reason="this OS/user cannot create file symlinks",
)


@pytest.fixture(autouse=True)
def real_home_root(monkeypatch, tmp_path):
    """Point the agent sandbox at a temp home so tests never touch the
    real one, and drop any INVINCIBLE_AGENT_ROOT override."""
    monkeypatch.delenv("INVINCIBLE_AGENT_ROOT", raising=False)
    monkeypatch.setenv("INVINCIBLE_AGENT_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def dir_link(real_home_root):
    """Build directory links under the sandbox root, removing every one
    again afterwards.

    The removal is safety, not tidiness: ``os.path.islink()`` is False
    for a Windows junction, so pytest's tmp_path cleanup treats one as an
    ordinary directory and would delete the TARGET's contents through it.
    ``os.rmdir`` removes the link and leaves the target alone.
    """
    created = []

    def _make(target: str, name: str) -> str:
        link = _in(real_home_root, name)
        if not _make_dir_link(link, target):
            pytest.skip("this OS/user cannot create directory links")
        created.append(link)
        return link

    yield _make
    for link in created:
        try:
            os.unlink(link)  # a symlink
        except OSError:
            # A Windows junction: os.rmdir removes the link and leaves the
            # target untouched (unlike rmtree, which follows it).
            with contextlib.suppress(OSError):
                os.rmdir(link)


def _in(root, *parts):
    return os.path.join(str(root), *parts)


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


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


# --- link escapes (deep code review 2026-09-24, finding 3) ------------------
#
# Both checks used to run on os.path.abspath(), which collapses ".." but
# does NOT follow links. A link inside the sandbox was therefore a doorway
# out of it, and a link named innocently was a doorway to an excluded
# file. Every case below passed before the fix.


@requires_dir_links
def test_directory_link_out_of_root_is_not_a_doorway(real_home_root, dir_link):
    """An innocently-named directory link inside the sandbox pointing
    outside it - addressed through, to a child that does not exist yet,
    which is what a write target looks like."""
    outside = tempfile.mkdtemp()
    _write(os.path.join(outside, "elsewhere.txt"))
    link = dir_link(outside, "escape-dir")

    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(os.path.join(link, "elsewhere.txt"))
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_write(os.path.join(link, "brand-new.txt"))


@requires_dir_links
def test_directory_link_to_denylisted_name_is_blocked(real_home_root, dir_link):
    """`escape-dir` -> `.ssh`: the components checked were the LINK's own
    path, so `escape-dir/authorized_keys` matched nothing and open()
    followed the link to the real key file."""
    _write(_in(real_home_root, ".ssh", "authorized_keys"))
    link = dir_link(_in(real_home_root, ".ssh"), "escape-dir")

    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(os.path.join(link, "authorized_keys"))
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_write(os.path.join(link, "authorized_keys"))


@requires_dir_links
def test_directory_link_inside_root_is_still_allowed(real_home_root, dir_link):
    """The fix must not become 'refuse every link': one that stays inside
    the sandbox and targets nothing excluded is ordinary use."""
    target = _in(real_home_root, "real-dir")
    _write(os.path.join(target, "notes.txt"), "fine")
    link = dir_link(target, "shortcut-dir")

    sandbox.check_agent_read(os.path.join(link, "notes.txt"))  # no raise
    sandbox.check_agent_write(os.path.join(link, "new.txt"))  # no raise


@requires_file_symlinks
def test_file_symlink_out_of_root_is_not_a_doorway(real_home_root):
    """A file link inside the sandbox pointing outside it."""
    outside = tempfile.mkdtemp()
    secret = _write(os.path.join(outside, "elsewhere.txt"))
    link = _in(real_home_root, "escape")
    os.symlink(secret, link)

    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(link)
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_write(link)


@requires_file_symlinks
def test_file_symlink_to_denylisted_name_is_blocked(real_home_root):
    """The basename denylist saw the LINK's name, not the target's, so
    `notes.txt` -> `.env` passed both checks and open() followed it."""
    env = _write(_in(real_home_root, ".env"), "SECRET=hunter2")
    link = _in(real_home_root, "notes.txt")
    os.symlink(env, link)

    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(link)
    with pytest.raises(ToolBlocked):
        sandbox.check_agent_write(link)


@requires_file_symlinks
def test_file_symlink_to_ssh_key_is_blocked(real_home_root):
    key = _write(_in(real_home_root, ".ssh", "id_rsa"), "PRIVATE KEY")
    link = _in(real_home_root, "harmless-config.txt")
    os.symlink(key, link)

    with pytest.raises(ToolBlocked):
        sandbox.check_agent_read(link)


@requires_file_symlinks
def test_file_symlink_inside_root_is_still_allowed(real_home_root):
    target = _write(_in(real_home_root, "real-notes.txt"), "fine")
    link = _in(real_home_root, "shortcut.txt")
    os.symlink(target, link)

    sandbox.check_agent_read(link)  # should not raise
    sandbox.check_agent_write(link)  # should not raise
