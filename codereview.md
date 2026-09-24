# Targeted Documentation Review — Invincible (`ai-gateway`)

**Date:** 2026-09-24
**Scope:** The uncommitted `facts`-retention decision and related handoff,
configuration, testing, and review documentation. This was not a full
codebase review.
**Mode:** Static review followed by documentation-only remediation; no runtime
code, schema, or migration was changed.

## Findings addressed

### MAJOR — The handoff overstated that `facts` was completely unused

- `TASKS-TASK-STT.md` called the table unused by all code and the importer
  branch dead.
- `invincible/core/db_import.py:156-165` remains the PostgreSQL `facts`
  writer, exposed by the supported `invincible db import` CLI path and
  covered by `tests/test_cli_db.py:446-480`.
- The documentation now says **no request-serving code** reads or writes the
  table and the legacy importer remains its only production writer.

### MAJOR — The handoff snapshot and storage wording were misleading

- The handoff originally described the working tree as clean even though the
  decision was uncommitted. It now labels that state as the handoff-time
  snapshot and directs the reader to recheck `git status`.
- `docs/WORKQUEUE.md` no longer says retention costs nothing. It now
  distinguishes zero request-path cost from retained schema, storage, and
  possible importer writes.

### MINOR — Related documentation was stale or ambiguous

- `docs/ROADMAP.md` and `docs/CONFIGURATION.md` now consistently describe
  `facts` as legacy-import history, not active request-serving state.
- `docs/TESTING.md` now names the shared `v1_user` fixture correctly.
- The handoff now describes the importer as resynchronizing the `facts`
  identity sequence, distinguishes live `extract_facts()` code from the
  unused `memory_max_facts` accessor, and provides a reproducible reference
  sweep.

## Verified clean

- `invincible/core/db.py` still defines the retained `facts` table.
- `invincible/core/memory.py::extract_facts()` is live, but its output feeds
  the `memories` table rather than the legacy `facts` table.
- Keeping the table and importer path together is consistent with current
  code. No schema or code removal is needed for this decision.
- No authentication, MCP safety-gate, provider-routing, or secret-handling
  behavior changed in this documentation-only pass.

## Validation

- `git diff --check` — passed; Git emitted only the existing Windows
  LF-to-CRLF warning for `docs/WORKQUEUE.md`.
- `ruff check .` — passed; the scan also reported the existing access-denied
  warning for `.tmptest/`.
- Tests were not required for this documentation-only change.
