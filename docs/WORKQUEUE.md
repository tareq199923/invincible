# Work Queue — Invincible

The single ordered list of actionable work. Everything to do, in
priority order, with detail. Strategic context (phases, direction,
what's implemented) lives in [ROADMAP.md](ROADMAP.md).

Last updated: 2026-09-21.

---

## Open work — in fix order

### 1. Phase 7 wrap-up — host migration before the trial ends

**The deployment is live and on its own domain** (2026-09-02): Neon
Postgres (ap-southeast-1, least-privilege roles verified by probe) +
the app on Railway's one-month trial, serving
`invincible-ai.me` (Cloudflare CNAME → Railway, DNS-only; verified
end-to-end including a chat round-trip). Fresh-start database; full
acceptance journey smoke-tested. Details in
[ROADMAP.md](ROADMAP.md) §Phase 7. Remaining:

- **Host migration before the trial ends (~2026-10-02):** move the
  container to Azure for Students (no card needed, $100 credit; a
  reminder is set for 2026-09-25). The Neon DB is host-agnostic —
  nothing changes there. Then update the DNS record to the new host.
- The Temp-folder portable PG is now dev/test-only (it holds no live
  data; live data lives on Neon). It stays for the local test suite;
  moving it out of Temp before a disk cleanup eats it remains a nice-
  to-have, no longer urgent.

---

## Recently completed

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
  a dated post-audit note in MULTI-TENANT-AUDIT.md; and MIGRATION-AZURE.md,
  which still told the operator to use the removed `GATEWAY_API_KEY`.

---

## Decisions pending

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
- **Publishing — DONE 2026-09-23: `invincible-ai` 0.3.0 is live on PyPI.**
  The upload claimed the name (it was unclaimed until then); the one-file
  `.exe` stays deferred. Packaging, metadata, the wheel smoke test, a
  tag-triggered release workflow, and trusted publishing are all in place
  ([RELEASING.md](RELEASING.md)).
- **The database:** remote-first is now the setup story (Neon etc.),
  matching the roadmap's hosted direction. Remaining question: is a
  bundled/local option still worth offering for offline users?

### Housekeeping (10 min, whenever)

- Move the dev database out of the Temp folder before a disk cleanup
  eats it (portable PG on 5433, manual start, dies on reboot) —
  superseded by the Phase 7 deployment item above, which retires this
  PG entirely.
- Revoke stale Claude/Grok OAuth connectors (`invincible oauth list`).
