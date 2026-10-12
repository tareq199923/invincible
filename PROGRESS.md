# PROGRESS.md — session diary (committed, kept short)

> Read at clock-in, update at clock-out. This is the cross-session memory.
> Active feature lives in `feature_list.json` (WIP=1: only one `active`).

## Current State

- Last commit: `bce3a7c` (main) — Dockerfile base from ECR Public mirror.
- F04 done (`passing`): read-only fan-out reframed + built as
  `core/harness_fanout.py` (planner + parallel scout/librarian steps,
  main loop synthesizes; trigger ≥6 reads/≥2 planes, once per turn
  shared with explore) + `tests/test_harness_fanout.py` (18 tests).
- Ruff/pytest status: `ruff check .` clean; F04 gate 35 passed
  (fanout+router+explore); full suite 1725 passed, 11 skipped.

## In Progress

- None — F04 complete (`passing`). All of F01/F02/F04/F06 passing,
  zero `active`. Next: pick F03/F05 or new work at next clock-in.

## Next Steps

1. Finish F01: verify JSON + ruff, flip F01 to `passing`, commit.
2. Pick next feature from `feature_list.json` (F02 Phase 6 CLI polish is the
   ROADMAP In-progress candidate) — set it `active` only after F01 passes.
3. Keep H6b / supervisor-revisit / vector items `planned` until activated.
4. F04 scoped: REFRAME (conditional rebuild) — DONE as read-only
   fan-out (`harness_fanout.py` + 18 tests, full suite green).
5. Pick next feature from `feature_list.json` (F03 H6b tunneling or
   F05 vector seam) — set it `active` only at next clock-in.

## Blockers

- None.
