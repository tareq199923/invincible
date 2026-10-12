# PROGRESS.md — session diary (committed, kept short)

> Read at clock-in, update at clock-out. This is the cross-session memory.
> Active feature lives in `feature_list.json` (WIP=1: only one `active`).

## Current State

- Last commit: `f8fd273` (main) — F03 H6b tunneling committed.
- F07 active (B0.1+B0.2): Baymax voice prefix (`BAYMAX_VOICE`) prepended to
  all webchat modes in `core/webchat_agent.py` + `tests/test_baymax_prompt.py`
  (6 tests). Original wording, no Disney script; existing pins intact.
  B0.2 avatar: `bubble-baymax` + dot/name marker in `templates/chat.html`
  (server history + JS `bubble()`/`ensureBaymaxMark`, streaming-safe) +
  CSS in `templates/static/app.css`. DOM-only, no new innerHTML surface.
- Ruff/pytest status: `ruff check .` clean; baymax 6 passed;
  `test_webchat.py` + `test_webchat_agent.py` 58 passed;
  `test_harness_router.py` 6 passed.
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

- F07 Baymax B0.1+B0.2 done (`active`). Next: B0.3 simulated pointing
  (screenshot preview in chat log), then tool packs (web_research first).

## Next Steps

1. Commit F03 on user's word (message: "F03: ..."); verify
   `git status` shows only the intended files first.
2. F05 vector seam remains `planned` — next candidate; set `active`
   only at next clock-in.
3. F04 fan-out (`harness_fanout.py` + 18 tests) stays green, untouched.

## Blockers

- None.
