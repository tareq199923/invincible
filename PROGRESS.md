# PROGRESS.md — session diary (committed, kept short)

> Read at clock-in, update at clock-out. This is the cross-session memory.
> Active feature lives in `feature_list.json` (WIP=1: only one `active`).

## Current State

- Last commit: `27f9892` (main) — F08 web_research committed.
- F07 done (`passing`): Baymax B0 browser-first (voice + avatar +
  screenshot preview).
- F08 done (`passing`): web_research tool (Bluey steal pack 1,
  Responses web_search via caller's BYOK credential).
- F09 done (`passing`, uncommitted): look_at_screen controls grounding
  (Bluey steal pack 2). `tool_executor._look_at_screen` + UIA walk
  (verified live: 112 controls, menus+buttons on 0-1000 grid); walk runs
  in a child process (raw COM can fail fatal - crash becomes an error
  result, agent survives); runner case, registry + order (agent-only,
  all modes incl plan), dispatch trio, summary previews, goldens, drift
  pins (27 tools), `MCP_PROTOCOL.md`. New:
  `tests/test_look_at_screen.py` (6 tests).
- Ruff/pytest status: `ruff check .` clean; FULL suite 1797 passed,
  11 skipped, 1 deselected. Clean-state checklist 5/5 ticked.
  Uncommitted — commit on user's word.
- F04 done (`passing`): read-only fan-out reframed + built as
  `core/harness_fanout.py` (planner + parallel scout/librarian steps,
  main loop synthesizes; trigger ≥6 reads/≥2 planes, once per turn
  shared with explore) + `tests/test_harness_fanout.py` (18 tests).
- F03 done (`passing`, uncommitted): H6b tunneling — `expose_port`
  staged + confirmed, WS-multiplexed bytes, `/t/{token}/` capability
  URLs, caps + audit. New: `core/harness_tunnel.py`,
  `endpoints/tunnel.py`, `tests/test_tunnel_registry.py` (25 tests) +
  `tests/test_tunnel_route.py` (18 tests); edited: settings, policy,
  tools registry (+2 goldens, additive), `mcp.py`, `agents.py`,
  `runner.py`, `sandbox.py`, `tool_executor.py` (`peek_type`), main +
  conftest wiring; docs: SECURITY §11, DEPLOYMENT posture, HARNESS_PLAN
  H6b, ROADMAP Phase 10, MCP_PROTOCOL entries.
- Ruff/pytest status: `ruff check .` clean; audit 5/5; tunnel suites
  43 passed; FULL suite 1768 passed, 11 skipped, 1 deselected.
  Clean-state checklist 5/5 ticked. Uncommitted — commit on user's word.

## In Progress

- None — F09 complete (`passing`). Zero `active`. Next: tool pack 3
  (computer-use) or F05 vector seam at next clock-in.

## Next Steps

1. Commit F09 on user's word; verify `git status` shows only the
   intended files first.
2. F05 vector seam remains `planned` — next candidate; set `active`
   only at next clock-in.
3. F04 fan-out (`harness_fanout.py` + 18 tests) stays green, untouched.

## Blockers

- None.
