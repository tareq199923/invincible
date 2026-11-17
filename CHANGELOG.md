# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is `0.x`, minor releases may contain breaking changes; they
are called out under **Changed** and **Removed**.

## [Unreleased]

## [0.6.2] - 2026-11-16

### Added

- Background agent tasks (Steps 1-3 of 5). A logged-in user can start a
  **plan-mode** task with no browser attached
  (`POST /dashboard/background-tasks`): it runs the same
  `prepare_chat` + `run_agent_turn(mode="plan")` pipeline as the
  dashboard webchat — hence the single router failover loop — with a
  30-minute wall-clock timeout, a per-user concurrency cap of 2, and
  metadata-only `workflow.started`/`completed`/`failed` events. The
  answer is stored in the chat session whose id is the workflow id.
- Background tasks dashboard UI: a start form on the Workflows page, a
  task status column, a polling result page with a safely rendered final
  answer, and an owner-scoped Stop action
  (`POST /dashboard/background-tasks/{workflow_id}/cancel`).
- Durable per-tool-call checkpoints for background tasks (migration
  `0018`, table `background_task_checkpoints`): one metadata-only row
  per successful tool call (tool name, call id, SHA-256 of the args,
  result size — never args, paths, commands, or result content).
  Progress tracking only; nothing replays a tool call.
- `CONTRIBUTING.md` — development setup, project invariants, and PR process.
- `CODE_OF_CONDUCT.md` — Contributor Covenant 2.1.
- `SECURITY.md` — private vulnerability reporting, scope, and safe harbor.
- This changelog.

### Notes

- Background tasks are **plan mode only** in this release: no mutating
  tools and no approvals (an unexpected approval event is denied).
  Self-hosters must run `invincible db upgrade` before upgrading to it
  (migration `0018`).

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

[Unreleased]: https://github.com/tareq199923/invincible/compare/v0.6.2...HEAD
[0.6.2]: https://github.com/tareq199923/invincible/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/tareq199923/invincible/compare/v0.6.0...v0.6.1
