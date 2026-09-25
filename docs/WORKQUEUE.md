# Work Queue — Invincible

The single ordered list of actionable work. Everything to do, in
priority order, with detail. Strategic context (phases, direction,
what's implemented) lives in [ROADMAP.md](ROADMAP.md).

Last updated: 2026-09-25.

---

## Open work — in fix order

### 1. Phase 7 wrap-up — transfer production ownership to the destination Railway account

**The deployment remains live and on its own domain** (2026-09-02): Neon
Postgres (ap-southeast-1, least-privilege roles verified by probe) + the app
on Railway, serving `invincible-ai.me` (Cloudflare CNAME → Railway, DNS-only;
verified end-to-end including a chat round-trip). Neon has a two-year term;
there is no current Azure or AWS migration. The remaining operational task is
to transfer Railway project ownership to a different Railway account. Details
in [ROADMAP.md](ROADMAP.md) §Phase 7 and the
[RAILWAY-ACCOUNT-TRANSFER.md](RAILWAY-ACCOUNT-TRANSFER.md) runbook.

- **Pending Railway ownership transfer:** keep the application, Neon database,
  database roles, and public domain in place while transferring Railway
  project ownership. Verify the destination deployment and environment before
  removing access from the current account.
- The dev/test Postgres is **already out of `%TEMP%`** — verified
  2026-09-23. Two clusters live in home directories:
  `C:\Users\SARK\pgdev` (PG 18, port 5433 — the one the suite runs
  against) and `C:\Users\SARK\inv-pg-portable` (PG 17). Only stray
  `pgctl.out`/`pgerr.txt` logs remain in Temp. It holds no live data
  (live data is on Neon), is manual-start and does not automatically restart
  after a reboot. Nothing to do here.

### 2. Phase 8 — retire the superseded local-era pieces

The hosted flow is stable (0.3.1 live on PyPI and in production, suite
green at 1135), so the deprecations whose trigger was *"after hosted
launch stabilizes"* are now actionable. The table in
[ROADMAP.md](ROADMAP.md) §Deprecated is the source of truth; what it
still owes:

- **Owner-secret-only MCP consent** — `INVINCIBLE_OWNER_SECRET` as a
  *sole identity* is superseded by user-bound OAuth subjects; listed as
  Phase 2+, so it is overdue. The env var itself **stays** — it still
  signs sessions. Only the identity path retires.
- **`facts` triple store** — **DECIDED 2026-09-24: keep temporarily while
  production data is audited.** No request-serving code reads or writes the
  table, and no backfill was performed. The legacy importer is now removed,
  so the table has no supported writer; it still occupies schema/storage.
  After checking and backing up production data, decide whether to drop it
  in a future migration.
- **Client-supplied `session_id` as storage identity** — relational
  session identity landed in Phase 1 and the transitional helper was
  only meant to be retained briefly. Check whether it is still there.

Local/self-hosted mode itself is **not** deprecated and stays.

---

## Recently completed

### Legacy SQLite importer retired — 2026-09-24

The deprecated `invincible db import` command and its SQLite importer module
were removed. The legacy PostgreSQL `facts` table remains temporarily while
production data is audited and backed up; no request-serving code uses it.

**Release note for whoever publishes next:** 0.3.1 shipped `db import` as a
documented command, so its removal is a breaking CLI change — the next PyPI
release must be a **minor** bump (`0.4.0`), not a patch, and must say the
command is gone. `invincible/__init__.py` now reads `0.4.0` (bumped,
unreleased — tag `v0.4.0` + publish still pending).

### 0.3.1 released — Anthropic messages report the serving model (2026-09-23)

`invincible-ai` **0.3.1** is on PyPI, published from tag `v0.3.1`
(`1bb027a`) via the trusted-publisher dispatch, and production is
serving it (`/health` → 0.3.1). It carries `f26eca4`, which makes
`/v1/messages` report the model that actually served the request
instead of echoing the requested one — mirroring `0f4e094` for
`/v1/responses`, so a cross-model fallback is now visible to Claude
Code and Codex in both APIs. That closes the last status-line
follow-up.

Verified: full suite 1136 passed; packaging smoke test passed; install
from PyPI into a scratch venv outside the repo reports 0.3.1 with
`providers.yaml` and all 20 templates packaged.

Two traps worth remembering, both of which cost time this round:
a Railway build+swap takes ~7 min, so `/health` serves the OLD version
right after a push (do not read that as a broken deploy); and
`pypi.org/pypi/<name>/json` is CDN-cached after a publish, so read
`pypi.org/simple/<name>/` instead.

Also retired: the stale `feature/inv-doctor` branch (merged via PR #8
on 2026-08-05, no unique commits) — deleted locally, remote ref was
already gone.

### Remote-first documentation pass (2026-09-21)

The product was already multi-user and remote (accounts, isolation, BYOK,
dashboard, agent, container/`$PORT` deploy files) but the entry docs still
taught a one-machine local setup, and several pages described surfaces that
had been deleted. Fixed:

- **New [docs/DEPLOYMENT.md](DEPLOYMENT.md)** — host-agnostic remote runbook:
  required env vars, ports/TLS/proxy headers, the two-role database, the
  single-instance constraint, operations, and a go-live checklist.
- **README** — remote-first description, hosted-service + self-host quick
  starts, `$INVINCIBLE_BASE` in every example, `INVINCIBLE_AGENT_ROUTING=1`
  marked as required on public deployments, `INVINCIBLE_MIGRATE_DB_URL`
  documented, dev-db marked dev-only.
- **CONFIGURATION.md** — managed-Postgres-first database guidance, agent env
  vars added, accurate CLI surface, remote `start` notes.
- **MCP_PROTOCOL.md** — discovery/metadata examples now use the hosted URL
  (they are request-derived), §7 is hosted-first with the tunnel as the
  self-host path.
- **Stale facts corrected** — ROADMAP auth/control-plane/CLI/packaging rows
  and Phase 6 status; ARCHITECTURE module map + migration range;
  AGENTS.md module map, docs index, and a new deployment-posture convention;
  a dated post-audit note in MULTI-TENANT-AUDIT.md; and the then-current
  migration runbook, which still told the operator to use the removed
  `GATEWAY_API_KEY`.

---

## Decisions pending

### Should assistant/tool text be able to mint auto-memories? (2026-09-24)

`core/memory.py::extract_facts` has **no role filter** — it scans every
message in the persisted turn, so the assistant reply and any tool-result
content reach the regex patterns and can write durable `auto` memories at
confidence 0.6, which `RetrievalService` then injects into later prompts for
that user. `extract_explicit` deliberately does the opposite
(`if m.get("role") != "user": continue`, with a comment saying an assistant
echoing "remember that…" must never mint a memory on its own).

**This is documented intent, not drift:** `record_memories`' docstring says
auto-extraction runs "over every message". So it is a design call, not a bug
to quietly patch — recorded here rather than changed.

What makes it worth revisiting: several patterns are phrasings an assistant
or a tool result emits routinely, not just user voice —
`\bremember that\s+(.+)`, `\bthe next step is\s+(.+)`,
`\bwe decided(?:\s+to)?\s+(.+)`, and `\b(?:I'?m )?(?:currently )?working
on\s+(.+)` (whose prefix is optional, so a bare "working on …" anywhere
matches). Attacker-influenced content — a fetched page, a file read through
`read_file`, a provider reply — therefore becomes self-reinforcing context.

Options: (a) leave as designed; (b) filter `extract_facts` to
`role == "user"` like the explicit extractor — behaviour change, needs test
updates; (c) keep assistant text eligible but exclude `role == "tool"`,
closing the attacker-controlled path only. Source: finding 2 of
[DEEP-CODE-REVIEW-2026-09-24.md](DEEP-CODE-REVIEW-2026-09-24.md).

### `invincible start` remote ergonomics (small, optional)

`start` still defaults to `--host 127.0.0.1`, spawns a Cloudflare tunnel,
and opens a browser — right for a laptop, irrelevant on a host (deployments
use the container command). Options if it becomes annoying: a `--remote`
preset (`0.0.0.0`, `--no-tunnel`, `--no-open-browser`) and honoring `$PORT`.
Touches pinned CLI tests, so it is deliberately not part of the doc pass.

### Distribution (#2) — the strategic frontier

The **user** journey against the hosted service is already one command:
`pip install invincible-ai` + `invincible agent` (device-flow pairing with
`https://invincible-ai.me` — no database, no `.env`, no provider setup on
the user's machine). The **operator** journey is
`install → setup --db-url <DSN> → start` — one command, one argument.

Open decisions (multi-day project, fold into Phase 6/7 planning):
- **Publishing — DONE 2026-09-23: `invincible-ai` 0.3.0 and 0.3.1 are
  live on PyPI.** The first upload claimed the name (it was unclaimed
  until then); the one-file `.exe` stays deferred. Packaging, metadata,
  the wheel smoke test, a tag-triggered release workflow, and trusted
  publishing are all in place and now exercised twice
  ([RELEASING.md](RELEASING.md)).
- **The database:** remote-first is now the setup story (Neon etc.),
  matching the roadmap's hosted direction. Remaining question: is a
  bundled/local option still worth offering for offline users?

### Housekeeping (5 min, whenever)

- Revoke stale Claude/Grok OAuth connectors (`invincible oauth list`).
