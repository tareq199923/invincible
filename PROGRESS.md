# PROGRESS.md — session diary (committed, kept short)

> Read at clock-in, update at clock-out. This is the cross-session memory.
> Active feature lives in `feature_list.json` (WIP=1: only one `active`).

## Current State

- Last commit: `4599a6d` (main) — F07 Baymax B0.1+B0.2 committed.
- F07 done (`passing`, uncommitted): Baymax B0 browser-first — B0.1 voice
  prefix (`BAYMAX_VOICE` on all modes) + B0.2 avatar (`bubble-baymax`
  dot/name, streaming-safe, DOM-only) + B0.3 simulated pointing
  (`_screenshot_image` allowlist png/cap, `image` on `tool_result`
  events, `shot-preview` in chat log). New: `tests/test_baymax_prompt.py`
  (6 tests), `tests/test_baymax_screenshot.py` (6 tests).
- Ruff/pytest status: `ruff check .` clean; baymax 12 passed;
  `test_webchat.py` + `test_webchat_agent.py` 58 passed; goldens +
  registry green (87 total with B0.3).
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

- None — F07 complete (`passing`). Zero `active`. Next: tool pack 1
  (`web_research`) or F05 vector seam at next clock-in.

## Next Steps

1. Commit F03 on user's word (message: "F03: ..."); verify
   `git status` shows only the intended files first.
2. F05 vector seam remains `planned` — next candidate; set `active`
   only at next clock-in.
3. F04 fan-out (`harness_fanout.py` + 18 tests) stays green, untouched.

## Blockers

- None.
