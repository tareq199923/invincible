# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is `0.x`, minor releases may contain breaking changes; they
are called out under **Changed** and **Removed**.

## [Unreleased]

### Added

- `CONTRIBUTING.md` — development setup, project invariants, and PR process.
- `CODE_OF_CONDUCT.md` — Contributor Covenant 2.1.
- `SECURITY.md` — private vulnerability reporting, scope, and safe harbor.
- This changelog.

## [0.6.1] - 2026-10-27

### Added

- Update awareness in `invincible login`, `invincible connect`, and
  `invincible doctor`: a one-line notice when a newer release is on PyPI
  (checked at most once per 24h, cached under `~/.invincible/`, disabled with
  `INVINCIBLE_NO_UPDATE_CHECK=1`). The check is a read-only GET to pypi.org.
- `invincible update` installs the latest `invincible-ai` from PyPI, with
  `--check` for a dry run and `--yes` for scripts. It refuses pipx, uv,
  editable, and container installs and prints the correct command instead of
  corrupting a managed environment.

### Changed

- Documentation and landing page now recommend `pip install -U invincible-ai`
  as the single install/upgrade command.
- The post-upgrade step for always-on agents is documented as
  `invincible harness service restart`.

### Fixed

- Wheel-contents tests now probe `connect --help` and `status --help`, catching
  packaging regressions in the CLI surface.

## Earlier releases

Releases `0.1.0` through `0.6.0` predate this changelog. Their tags and commit
history are available in the repository:

```bash
git tag --sort=v:refname
git log --oneline v0.6.0
```

[Unreleased]: https://github.com/tareq199923/invincible/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/tareq199923/invincible/compare/v0.6.0...v0.6.1
