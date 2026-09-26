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

The hosted flow is stable (0.4.0 live on PyPI and in production, suite
green at 1175), so the deprecations whose trigger was *"after hosted
launch stabilizes"* are now actionable. The table in
[ROADMAP.md](ROADMAP.md) §Deprecated is the source of truth; what it
still owes:

- **Owner-secret-only MCP consent** — `INVINCIBLE_OWNER_SECRET` as a
  *sole identity* is superseded by user-bound OAuth subjects; listed as
  Phase 2+, so it is overdue. The env var itself **stays** — it still
  signs sessions. Only the identity path retires. **RETIRED 2026-09-25:**
  `OAuthStore.create_code` / `issue_token_pair` now require a subject,
  the legacy subject-less `consume_code` variant is deleted, and
  `rotate_refresh` refuses pre-subject rows; leftover NULL-subject rows
  fail closed at `require_mcp_auth` (pinned by
  `test_mcp_subject_less_token_returns_401` and
  `test_refresh_without_subject_is_refused`). **Companion 2026-09-25:**
  the pre-rename `MCP_SHARED_SECRET` session-signing fallback is deleted
  too (`settings.legacy_owner_secret` gone; setup/rotate no longer
  migrate it; doctor fails the owner check on the alias alone) — stale
  values are inert, rename to `INVINCIBLE_OWNER_SECRET`.
- **`facts` triple store** — **DROPPED 2026-09-25** (revision `0013`):
  audited empty on production (0 rows; backed up to
  `invincible-facts-backup-20260925.csv`), metadata no longer declares
  it, scratch-DB upgrade/downgrade/rerun pinned by
  `tests/test_migration_drop_facts.py`. No backfill was ever performed;
  the `extract_facts` memory extractor (feeds `memories`) is unrelated
  and stays.
- **Client-supplied `session_id` as storage identity** — relational
  session identity landed in Phase 1 and the transitional helper was
  only meant to be retained briefly. **AUDITED 2026-09-25 (retained,
  justified):** the remaining legacy path is the `UNSCOPED` default in
  `core/scope.py`, and no request path relies on it — all three chat
  endpoints resolve-or-create the principal's surrogate session up front;
  graph/dashboard 404 before projecting on failed lookups; MCP tools
  scope under the caller subject. It stays for unit tests and local
  tooling only (recorded in the `scope.py` docstring); the `None`
  fail-closed contract is pinned by `tests/test_scope_contract.py` +
  `tests/test_isolation.py`.

Local/self-hosted mode itself is **not** deprecated and stays.

---

## Recently completed

### 0.5.0 released — harness surface + Phase 8 retirements (2026-09-26)

`invincible-ai` **0.5.0** is on PyPI, published from tag `v0.5.0` via the
trusted-publisher dispatch. Minor bump carrying: the harness surface
(WS-first agent relay, machine inventory, `harness` CLI group), the
`invincible agent` removal (`harness connect` is the only entry point),
the `facts` table drop (revision `0013`, audited empty), user-voice-only
auto memories, the owner-secret consent retirement, the
`MCP_SHARED_SECRET` fallback removal, and the remote-first CLI help.

Verified: `ruff check` clean; packaging smoke test passed in CI;
install from PyPI into a scratch venv outside the repo reports 0.5.0.
Production cutover pending: Railway redeploy picks up the tag and its
startup `db upgrade` runs through `0013`; confirm `/health` → 0.5.0
after the swap (~7 min, it serves the old version until then).

### 0.4.0 released — `db import` removal ships as a minor bump (2026-09-25)

`invincible-ai` **0.4.0** is on PyPI, published from tag `v0.4.0` via the
trusted-publisher dispatch, and production is serving it (`/health` →
0.4.0). It carries the 2026-09-24 removal of `invincible db import` and
its SQLite importer module — a breaking CLI change, hence the minor bump
rather than a patch. Replacement path: direct hosted signup/onboarding.

Verified: `ruff check` clean; full suite 1175 passed; `twine check`
passed; packaging smoke test passed; install from PyPI into a scratch
venv outside the repo reports 0.4.0 with `invincible --version` /
`invincible harness connect --help` working with no database and no `.env`.

### Legacy SQLite importer retired — 2026-09-24

The deprecated `invincible db import` command and its SQLite importer module
were removed. The legacy PostgreSQL `facts` table was audited empty,
backed up, and dropped by revision `0013` on 2026-09-25; no
request-serving code used it.

Shipped in **0.4.0** (2026-09-25) — see above.

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

`core/memory.py::extract_facts` **had no role filter** — it scanned every
message in the persisted turn, so the assistant reply and any tool-result
content reached the regex patterns and could write durable `auto` memories
at confidence 0.6.

**DECIDED 2026-09-25: option (b) — user voice only.** `extract_facts`
now skips every non-`user` message, matching `extract_explicit`'s
long-standing rule. Assistant replies and tool results can no longer mint
memories, closing the attacker-influenced-content → durable-memory chain
(a fetched page, a file read, a provider reply). Pinned by
`test_auto_extraction_ignores_assistant_voice` and
`test_auto_extraction_ignores_tool_results` in `tests/test_memory.py`.
Source: finding 2 of
[DEEP-CODE-REVIEW-2026-09-24.md](DEEP-CODE-REVIEW-2026-09-24.md).

### `invincible start` remote ergonomics (small, optional)

`start` still defaults to `--host 127.0.0.1`, spawns a Cloudflare tunnel,
and opens a browser — right for a laptop, irrelevant on a host (deployments
use the container command). Options if it becomes annoying: a `--remote`
preset (`0.0.0.0`, `--no-tunnel`, `--no-open-browser`) and honoring `$PORT`.
Touches pinned CLI tests, so it is deliberately not part of the doc pass.

### Distribution (#2) — the strategic frontier

The **user** journey against the hosted service is already one command:
`pip install invincible-ai` + `invincible harness connect` (device-flow pairing with
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
