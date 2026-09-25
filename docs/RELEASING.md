# Releasing invincible-ai

How to cut a release. Everything is verifiable locally; the actual
pypi.org upload is deliberately a human decision.

## Version — one source of truth

The version lives **only** in `invincible/__init__.py`
(the `__version__` literal). `pyproject.toml` reads it dynamically
(`[tool.setuptools.dynamic]`); never edit a version anywhere else.

**Never move an existing tag.** `v0.1.0` and `v0.2.0` are taken by
early-history commits (`v0.2.0` sits 202 commits behind the packaging
work) and were never uploaded to PyPI. `v0.3.0` is taken too — it was the
first PyPI release, published 2026-09-23. Each release takes the next
unused number.

## Build + verify locally

```bash
pip install -e ".[dev]"        # build, twine, setuptools>=77 come with dev extras
rm -rf dist/                   # stale artifacts here WILL be uploaded by twine
python -m build                # isolated build env; produces dist/
twine check dist/*             # must PASS before anything else
```

Clearing `dist/` is not optional: `twine upload dist/*` globs whatever is
in the directory, so a leftover wheel from an older version gets published
alongside the intended one.

The full correctness gate is the packaging smoke test (slow-marked, so it
does not run in the default suite — an explicit `-m slow` overrides that):

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
2. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`, where
   `vX.Y.Z` is the next **unused** number (see the version note above;
   `v0.3.0` is already published).
3. The `Release` workflow (`.github/workflows/release.yml`) builds the
   distributions, `twine check`s them, runs the packaging smoke test, and
   uploads `dist/` as an artifact.
4. Publish is **opt-in**: run the workflow manually with
   `publish = true`. The `publish` job uses PyPI OIDC (trusted
   publishing) — no API token is stored in the repo.

Step 4 rebuilds from the ref you dispatch against, so dispatch against the
**tag**, never a branch. Either route works — the web UI needs nothing
installed:

**Via the web UI**

Repo → *Actions* → *Release* → *Run workflow*. In the panel that opens,
switch the ref dropdown from `main` to the tag, tick `publish`, then click
*Run workflow*. If the `publish` job never appears in the run, the
checkbox did not register.

**Via the GitHub CLI** (`gh`, if you have it)

```bash
TAG=v0.3.0        # the tag pushed in step 2
gh workflow run release.yml --ref "$TAG" -f publish=true
```

`workflow_dispatch` accepts a tag ref — *"once a workflow has run at least
once, you can dispatch it against any branch or tag"*, and the tag push in
step 2 is that first run. (The workflow file has to live on the default
branch to be *dispatchable* at all; that is the only thing `main` is needed
for.) `GITHUB_REF` is then the tag, so `actions/checkout` builds the tagged
commit. Dispatching from `main` instead builds whatever `main`'s head is at
that moment — if anything landed after the tag, you publish untagged code
under a version number PyPI will never let you reuse.

### Arming trusted publishing (one time — done before the 0.3.0 publish)

Two halves, both required:

**On pypi.org** → your account → *Publishing* → add a **Pending Publisher**
for GitHub:

- Owner/repository: `tareq199923/invincible`
- Workflow name: `release.yml`
- Environment name: `pypi`

**On GitHub** → repo *Settings* → *Environments* → *New environment* named
exactly `pypi`. No secrets go in it — OIDC carries the auth.

Creating it is housekeeping, not a gate: GitHub auto-creates any
environment a workflow references, so the `publish` job
(`environment: pypi`) runs either way and its OIDC claim still carries
`environment: pypi`. Create it anyway so the protection rules are a
deliberate choice — and if you add a deployment branch/tag rule, it must
allow `v*`, or the tag dispatch above never reaches PyPI. When a first
publish is rejected, the cause is almost always the pypi.org half:
owner/repository, workflow filename and environment name must match the
Pending Publisher exactly.

(Also add the same entry on test.pypi.org if you want CI dry-runs there.)

## Publish dry run (optional — a rehearsal against test.pypi.org)

```bash
twine upload --repository testpypi dist/*
# in a throwaway venv:
pip install --index-url https://test.pypi.org/simple/ invincible-ai
invincible --version && invincible agent --help
```

## After the upload — verify from the outside

```bash
# in a throwaway venv, NOT the repo checkout:
pip install invincible-ai
invincible --version && invincible agent --help
```

Installing from inside the repo directory can shadow the installed
package with the source tree, so the check must run elsewhere.

PyPI versions are **immutable** — a broken `0.3.0` cannot be replaced,
only yanked and superseded by `0.3.1`. The release workflow's `build` job
runs the smoke test above for exactly that reason, and `publish` needs
`build`, so a broken wheel fails the run instead of reaching PyPI.

## PyPI release status

`invincible-ai` **0.3.0 was published on 2026-09-23** — the first upload,
which claimed the name (it was unclaimed until then, so anyone could have
taken it) — followed by **0.3.1** (serving-model fix for `/v1/messages`).
The README's `pip install invincible-ai` journey is real now,
verified by installing from PyPI into a scratch venv outside the repo:
`invincible --version`, the packaged `providers.yaml` and `templates/`,
and a migration head matching the source tree.

**0.4.0 shipped 2026-09-25:** minor bump, not a patch — it removes the
`invincible db import` legacy SQLite importer that 0.3.1 shipped as a
documented command (replacement: direct hosted signup/onboarding).
Published from tag `v0.4.0` via the trusted-publisher dispatch and
verified serving in production (`/health` → 0.4.0).

Everything from here is immutable: `0.3.0` can never be replaced or
re-uploaded, only yanked and superseded. Bump `invincible/__init__.py` to
the next unused number before tagging the next release.
