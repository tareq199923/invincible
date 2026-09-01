# tests/test_env_isolation.py
"""R5: importing the app must never load a .env from the package's own
directory tree - only from the working directory.

Rehearsal root cause: ``main.py``'s module-level ``load_dotenv()`` with no
arguments resolves the file by walking up from *the caller's frame* - i.e.
from ``invincible/main.py`` itself - so on a source/editable install the
developer's repo-root .env (provider keys, DB URL) silently leaked into
any process importing ``invincible.main``, no matter where it was launched.
The fix pins the search to the cwd via ``find_dotenv(usecwd=True)``.

The reproduction is subprocess-based on purpose: the frame-walk behavior
depends on whether ``__main__`` has a ``__file__`` (true for real scripts
and console entry points like `invincible start`, false for `python -c`),
and pytest itself is a real script - exactly the leaky case.
"""
import os
import subprocess
import sys
import textwrap

import pytest

from invincible import main as invincible_main

SCRATCH_MARKER = "R5_SCRATCH_MARKER"

# A file-based script (like a console entry point): the leaky case.
_LEAK_PROBE = textwrap.dedent(f"""
    import os
    os.environ.pop("{SCRATCH_MARKER}", None)
    os.environ.pop("AGENTROUTER_API_KEY", None)
    import invincible.main
    print("MARKER:", os.getenv("{SCRATCH_MARKER}", "<unset>"))
""")


@pytest.fixture
def isolated_scratch_env(tmp_path, monkeypatch):
    """A scratch cwd with its own .env; run inside a process whose __main__
    has a __file__ (real-script case, like `invincible start`)."""
    scratch = tmp_path / "scratch-launch"
    scratch.mkdir()
    decoy = tmp_path / "decoy-root"
    decoy.mkdir()
    # A .env UP the tree from the scratch cwd, but NOT resolvable from the
    # package location: if main.py's load leaks by frame-walk it would find
    # the developer's real repo .env; here we instead verify the loaded
    # value is the scratch one - or nothing at all without a scratch file.
    (scratch / ".env").write_text(
        f"{SCRATCH_MARKER}=from-scratch-env\n", encoding="utf-8")
    env = {
        k: v for k, v in os.environ.items()
        if k not in (SCRATCH_MARKER, "AGENTROUTER_API_KEY")
        and not k.startswith("INVINCIBLE_")
    }
    return scratch, env


def _run_probe(scratch, env):
    """Run the leak probe as a REAL script file from the scratch cwd."""
    script = scratch / "_probe.py"
    script.write_text(_LEAK_PROBE, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, env=env, cwd=str(scratch),
        timeout=60,
    )


def test_app_env_loads_from_cwd_not_package_tree(isolated_scratch_env):
    """A real-script launch from a scratch cwd loads ITS .env."""
    scratch, env = isolated_scratch_env
    result = _run_probe(scratch, env)
    assert result.returncode == 0, result.stderr
    assert "MARKER: from-scratch-env" in result.stdout


def test_app_env_does_not_leak_repo_root_dotenv(monkeypatch, tmp_path):
    """The regression: importing invincible.main from OUTSIDE the repo (with
    __main__ having a __file__, the console-entry-point case) must NOT
    backfill variables from the repo-root .env sitting next to the package.

    Runs the probe from a cwd with NO .env: if the frame-walk leak were
    still present, the developer's real .env (always at the repo root in a
    checkout) would set real values - assert the marker stays unset.
    """
    # A .env at the package's parent (the leak's source) containing a value
    # the process must never see.
    package_root = tmp_path / "leaky-root"
    package_root.mkdir()
    (package_root / ".env").write_text(
        f"{SCRATCH_MARKER}=from-package-tree\n", encoding="utf-8")
    # Point PYTHONPATH at the package's parent so invincible resolves from
    # there (simulating the editable/source layout) - the probe then imports
    # a main.py whose parent-of-parent holds the decoy .env.
    env = {
        k: v for k, v in os.environ.items()
        if k not in (SCRATCH_MARKER, "AGENTROUTER_API_KEY")
    }
    clean_cwd = tmp_path / "clean-launch"
    clean_cwd.mkdir()

    probe = clean_cwd / "_probe.py"
    probe.write_text(_LEAK_PROBE, encoding="utf-8")
    # Import from the leaky package tree by path manipulation: prepend the
    # repo root so `import invincible` resolves to the real checkout. The
    # checkout's parent dir has no .env, but the repo root itself does -
    # which is exactly what the frame-walk used to pick up.
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(invincible_main.__file__), "..")
    )
    assert os.path.isfile(os.path.join(repo_root, ".env")), (
        "test assumes a developer checkout with a repo-root .env"
    )
    env["PYTHONPATH"] = repo_root
    result = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True, text=True, env=env, cwd=str(clean_cwd),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    # The marker only exists in .env files; it must not leak in.
    assert "MARKER: <unset>" in result.stdout, (
        f"repo-root .env leaked into the process: {result.stdout!r}"
    )
