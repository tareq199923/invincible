# PROGRESS.md — session diary (committed, kept short)

> Read at clock-in, update at clock-out. This is the cross-session memory.
> Active feature lives in `feature_list.json` (WIP=1: only one `active`).

## Current State

- Last commit: `bce3a7c` (main) — Dockerfile base from ECR Public mirror.
- Working tree at Step A start: 6 modified (`docs/CONFIGURATION.md`,
  `invincible/core/harness_runtime.py`, `invincible/core/settings.py`,
  `invincible/core/webchat_agent.py`, `tests/test_webchat_agent.py`,
  `tools/eval/runner.py`) + untracked `.vscode/`.
- Ruff/pytest status: F02 gate green — `test_cli_harness.py` 20 passed,
  `test_cli/login/agent` 95 passed, `ruff check .` clean. Docs already in
  sync (CONFIGURATION.md §4 matches `--help`; no text churn needed).

## In Progress

- None — F02 Phase 6 CLI polish complete (`passing`). Next candidate: F03
  H6b tunneling (deferred, needs isolated threat-model PR) or new work.

## Next Steps

1. Finish F01: verify JSON + ruff, flip F01 to `passing`, commit.
2. Pick next feature from `feature_list.json` (F02 Phase 6 CLI polish is the
   ROADMAP In-progress candidate) — set it `active` only after F01 passes.
3. Keep H6b / supervisor-revisit / vector items `planned` until activated.

## Blockers

- None.
