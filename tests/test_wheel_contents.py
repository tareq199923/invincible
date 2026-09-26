# tests/test_wheel_contents.py
"""Packaging smoke test: the wheel/sdist a release actually ships.

The README promises `pip install invincible-ai` -> `invincible harness
connect` with no database and no .env. That promise is only true if the built
distribution carries everything: providers.yaml (importlib.resources is
the only lookup path), the full migrations package (db upgrade / doctor
resolve it through core.db.migrations_config), every dashboard template,
and the static assets. This test builds the real sdist + wheel, installs
the wheel into a scratch venv, and asserts the promises from *inside*
that venv — the exact way a user's machine receives the package.

Slow and network-using (a scratch venv pip-installs the wheel's runtime
dependencies), so it is marked `slow` and excluded from the default run;
the release workflow's `build` job runs it explicitly with `-m slow`,
before the `publish` job that depends on that job can upload anything.
"""
import json
import os
import re
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("build", reason="packaging smoke test needs `build` (dev extra)")

REPO_ROOT = Path(__file__).resolve().parent.parent

# The known-good packaged surface (source of truth — the pyproject globs
# are deliberately not asserted; the built artifact is).
EXPECTED_TEMPLATES = {
    "account.html", "base.html", "dashboard.html", "device.html",
    "device_result.html", "landing.html", "login.html", "mcp.html",
    "machines.html",
    "memory.html", "providers.html", "register.html", "sessions.html",
    "session_detail.html", "settings.html", "setup.html", "tasks.html",
    "usage.html", "_memory_table.html", "_provider_row.html",
    "_provider_rows.html",
}
EXPECTED_STATIC = {"graph.js", "htmx.min.js"}


def _source_version() -> str:
    text = (REPO_ROOT / "invincible" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    assert match, "invincible/__init__.py must define a literal __version__"
    return match.group(1)


def _venv_python(env_dir: Path) -> Path:
    return (env_dir / "Scripts" / "python.exe" if os.name == "nt"
            else env_dir / "bin" / "python")


@pytest.mark.slow
def test_wheel_is_complete_and_installable(tmp_path: Path):
    dist = tmp_path / "dist"

    # 1. Build the real sdist + wheel (no-isolation: setuptools>=77 is a
    #    dev dependency; isolated builds would re-download it every run).
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation",
         "--outdir", str(dist)],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    )
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    version = _source_version()
    assert f"invincible_ai-{version}-py3-none-any.whl" == wheel.name
    assert f"invincible_ai-{version}.tar.gz" == sdist.name

    # 2. Wheel contents: metadata, templates, static assets, migrations.
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        metadata = zf.read(
            next(n for n in names if n.endswith(".dist-info/METADATA"))
        ).decode("utf-8")
    assert f"Version: {version}" in metadata
    assert "License-Expression: MIT" in metadata  # PEP 639 form

    packaged_templates = {
        n.split("invincible/templates/", 1)[1]
        for n in names
        if "invincible/templates/" in n and n.endswith(".html")
    }
    assert packaged_templates == EXPECTED_TEMPLATES, (
        "Wheel templates drifted from the expected set - update "
        "EXPECTED_TEMPLATES together with the dashboard change."
    )
    static = {
        n.rsplit("/", 1)[1]
        for n in names if "invincible/templates/static/" in n
    }
    assert static >= EXPECTED_STATIC
    assert any("invincible/migrations/script.py.mako" in n for n in names)
    assert any("invincible/migrations/versions/20260915_0009_user_settings.py"
               in n for n in names)

    # 3. Sdist carries the files only an sdist can (repo-root level).
    with tarfile.open(sdist) as tf:
        sdist_names = set(tf.getnames())
    for expected in ("LICENSE", "README.md", "providers.yaml.example"):
        assert any(n.endswith(expected) for n in sdist_names), expected

    # 4. Install the wheel into a scratch venv - the user's-machine path.
    venv.EnvBuilder(with_pip=True, clear=True).create(tmp_path / "venv")
    py = _venv_python(tmp_path / "venv")
    subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", "--prefer-binary",
         str(wheel)],
        check=True, capture_output=True, text=True,
    )

    # 5. Probe from inside that venv: version, providers.yaml through
    #    importlib.resources, and migration heads through the real
    #    core.db loader (the call that breaks a partially-packaged wheel).
    #    The wheel's head must equal the source tree's head.
    from invincible.core.db import migration_heads as source_heads_fn

    source_heads = list(source_heads_fn() or [])
    assert source_heads, "source tree must expose a migration head"
    probe = (
        "import importlib.resources, json\n"
        "import invincible\n"
        "from invincible.core.db import migration_heads\n"
        "providers = importlib.resources.files('invincible')."
        "joinpath('providers.yaml')\n"
        "print(json.dumps({'version': invincible.__version__,"
        " 'providers_yaml': providers.is_file(),"
        " 'heads': list(migration_heads() or [])}))\n"
    )
    result = subprocess.run(
        [str(py), "-c", probe], check=True, capture_output=True, text=True
    )
    info = json.loads(result.stdout.strip().splitlines()[-1])
    assert info["version"] == version
    assert info["providers_yaml"], "packaged providers.yaml missing/unresolvable"
    assert info["heads"], "wheel exposes no migration head"
    assert info["heads"] == source_heads, (
        f"wheel migration head {info['heads']} != source head {source_heads} "
        "- a revision file is missing from the package data"
    )

    # 6. Entry points work from the bare install: no DB, no .env needed.
    invincible_exe = py.with_name(
        "invincible.exe" if os.name == "nt" else "invincible")
    for argv in (["--version"], ["harness", "connect", "--help"]):
        subprocess.run(
            [str(invincible_exe), *argv],
            check=True, capture_output=True, text=True,
        )
