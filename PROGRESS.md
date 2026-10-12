# PROGRESS.md — session diary (committed, kept short)

> Read at clock-in, update at clock-out. This is the cross-session memory.
> Active feature lives in `feature_list.json` (WIP=1: only one `active`).

## Current State

- Last commit: `fe9b04b` (main) — F07 Baymax B0.3 committed.
- F07 done (`passing`): Baymax B0 browser-first (voice + avatar +
  screenshot preview).
- F08 done (`passing`, uncommitted): web_research tool (Bluey steal pack
  1). New: `core/web_research.py` (Responses web_search via caller's own
  BYOK credential, question-text-only, SSRF-safe; soft unavailable),
  `tests/test_web_research.py` (11 tests). Edited: registry + order (all
  modes incl plan), `tool_dispatch` + `webchat_agent` pre-read-only
  branch + summary, `endpoints/mcp.py` branch, goldens, drift pins
  (26 tools, passthrough), explore exclusion, `MCP_PROTOCOL.md`.
  Server-side always; never agent-routed.
- Ruff/pytest status: `ruff check .` clean; FULL suite 1791 passed,
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

- None — F08 complete (`passing`). Zero `active`. Next: tool pack 2
  (`look_at_screen` grounding) or F05 vector seam at next clock-in.

## Next Steps

1. Commit F08 on user's word; verify `git status` shows only the
   intended files first.
2. F05 vector seam remains `planned` — next candidate; set `active`
   only at next clock-in.
3. F04 fan-out (`harness_fanout.py` + 18 tests) stays green, untouched.

## Blockers

- None.
