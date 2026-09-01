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

Self-contained by design: the leaky layout (a package whose parent tree
holds a decoy .env) is MANUFACTURED in tmp_path - a copy of the
invincible/ package under a scratch root with its own .env - so the
test never depends on a developer checkout containing a real repo-root
.env (CI checkouts never have one; .env is gitignored).
"""
import os
import subprocess
import sys
import textwrap

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


def _base_env():
    """A copy of the ambient env, stripped of anything that could mask the
    assertion (the marker itself and every INVINCIBLE_/provider key)."""
    return {
        k: v for k, v in os.environ.items()
        if k != SCRATCH_MARKER
        and not k.startswith("INVINCIBLE_")
        and not k.endswith("_API_KEY")
    }


def _run_probe(script_dir, env):
    """Run the leak probe as a REAL script file (``__main__.__file__`` set,
    like a console entry point) from the given cwd."""
    script = script_dir / "_probe.py"
    script.write_text(_LEAK_PROBE, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, env=env, cwd=str(script_dir),
        timeout=60,
    )


def _make_decoy_package(tmp_path, marker_value):
    """Manufacture the leaky layout: a scratch root containing .env and a
    copy of the invincible/ package. The real main.py resolves there via
    PYTHONPATH, and its parent dir (the scratch root) holds the decoy
    .env - exactly what the pre-fix frame-walk used to pick up.

    A physical copy, not a symlink: Windows needs developer mode for
    directory symlinks, and a test that silently skips where the bug was
    found is no regression armor at all. The package is a few dozen small
    files; copying is cheap."""
    import shutil

    decoy_root = tmp_path / "decoy-root"
    decoy_root.mkdir()
    (decoy_root / ".env").write_text(
        f"{SCRATCH_MARKER}={marker_value}\n", encoding="utf-8")
    shutil.copytree(
        os.path.dirname(invincible_main.__file__),
        decoy_root / "invincible",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return decoy_root


def test_app_env_loads_from_cwd_not_package_tree(tmp_path):
    """A real-script launch from a scratch cwd loads ITS .env - the fix
    keeps the direct-uvicorn-from-project-folder convenience working."""
    scratch = tmp_path / "scratch-launch"
    scratch.mkdir()
    (scratch / ".env").write_text(
        f"{SCRATCH_MARKER}=from-scratch-env\n", encoding="utf-8")

    result = _run_probe(scratch, _base_env())
    assert result.returncode == 0, result.stderr
    assert "MARKER: from-scratch-env" in result.stdout


def test_app_env_does_not_leak_package_tree_dotenv(tmp_path):
    """The regression: importing invincible.main with __main__.__file__ set
    (the console-entry-point case) must NOT backfill variables from a .env
    sitting at the package's parent - even when that is the ONLY .env in
    reach and the cwd has none."""
    decoy_root = _make_decoy_package(tmp_path, "from-package-tree")
    clean_cwd = tmp_path / "clean-launch"
    clean_cwd.mkdir()

    env = _base_env()
    # The package resolves from the decoy root, NOT the normal sys.path.
    env["PYTHONPATH"] = str(decoy_root)
    result = _run_probe(clean_cwd, env)
    assert result.returncode == 0, result.stderr
    # The decoy .env was the only one findable by the old frame-walk; with
    # usecwd=True the cwd has no .env, so nothing loads at all.
    assert "MARKER: <unset>" in result.stdout, (
        f"package-tree .env leaked into the process: {result.stdout!r}"
    )
