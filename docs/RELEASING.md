# Releasing invincible-ai

How to cut a release. Everything is verifiable locally; the actual
pypi.org upload is deliberately a human decision.

## Version — one source of truth

The version lives **only** in `invincible/__init__.py`
(`__version__ = "0.2.0"`). `pyproject.toml` reads it dynamically
(`[tool.setuptools.dynamic]`); never edit a version anywhere else.

## Build + verify locally

```bash
pip install -e ".[dev]"        # build, twine, setuptools>=77 come with dev extras
python -m build                # isolated build env; produces dist/
twine check dist/*             # must PASS before anything else
```

The full correctness gate is the packaging smoke test (slow-marked, so it
does not run in the default suite):

```bash
pytest -q -m slow tests/test_wheel_contents.py
```

It builds the real sdist + wheel, installs the wheel into a scratch venv,
and asserts — from inside that venv — the version, the packaged
`providers.yaml`, the migration head (wheel == source), every dashboard
template + static asset, and that `invincible --version` /
`invincible agent --help` work with no database and no `.env`.

## Release flow (CI)

1. Bump `invincible/__init__.py`, commit.
2. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.
3. The `Release` workflow (`.github/workflows/release.yml`) builds and
   `twine check`s the distributions and uploads `dist/` as an artifact.
4. Publish is **opt-in**: run the workflow manually with
   `publish = true`. The `publish` job uses PyPI OIDC (trusted
   publishing) — no API token is stored in the repo.

### Arming trusted publishing (one time, before the first publish)

On pypi.org → your account → *Publishing* → add a **Pending Publisher**
for GitHub:

- Owner/repository: `tareq199923/invincible`
- Workflow name: `release.yml`
- Environment name: `pypi`

(Also add the same entry on test.pypi.org if you want CI dry-runs there.)

## Publish dry run (recommended before the first real upload)

```bash
twine upload --repository testpypi dist/*
# in a throwaway venv:
pip install --index-url https://test.pypi.org/simple/ invincible-ai
invincible --version && invincible agent --help
```

## ⚠️ The PyPI name is currently unclaimed

`invincible-ai` had no project on pypi.org as of 2026-09-21. Until this
package is uploaded, **anyone can claim the name** and the README's
`pip install invincible-ai` journey stays false. Publish promptly after
this pass; the metadata here is complete and validated (`twine check`
PASS) so the upload is ready when you are.
