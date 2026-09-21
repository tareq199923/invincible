# Deployment — running Invincible remotely (multi-user)

How to put Invincible on a host so many users reach it over HTTPS. This is
the remote path; a laptop with `invincible start` is the local dev path and
is covered by [CONFIGURATION.md](CONFIGURATION.md).

Deployment shape: **one app process + one PostgreSQL database**.

```
        users / clients (HTTPS)
                 │
        ┌────────▼─────────┐        ┌──────────────────────┐
        │  Invincible app  │───────►│  Managed PostgreSQL  │
        │  uvicorn :$PORT  │        │  runtime + migrate   │
        └──────────────────┘        └──────────────────────┘
```

There is **no server-side provider pool**: every request routes through the
caller's own BYOK credentials, so a deployment needs **no provider API
keys**. Users connect their own keys on the dashboard's Providers page.

---

## 1. Prerequisites

| Need | Notes |
|---|---|
| A host that runs a container (or a Python process) | Railway, Azure Container Apps/App Service, Fly, Render, a VPS — anything that can run the shipped `Dockerfile` and expose a port |
| A **persistent, managed** PostgreSQL | Neon, RDS, Azure Database for PostgreSQL, or your own cluster. Never a temp-directory or otherwise ephemeral cluster — all state lives here |
| A domain + TLS | The platform's default domain is fine; a custom domain is a DNS record away (`invincible-ai.me` is a Cloudflare CNAME in DNS-only mode pointing at the app host) |
| A secret store for env vars | The platform's environment-variable UI/CLI. Never commit them |

---

## 2. Required environment variables

Generate every secret **per environment** — never reuse a dev value.

| Variable | Required | Purpose |
|---|---|---|
| `INVINCIBLE_DB_URL` | **yes** | Runtime DSN. Connect as the CRUD-only role (§4). The server refuses to start without it |
| `INVINCIBLE_MIGRATE_DB_URL` | recommended | Schema-owner DSN used **only** by the one `db upgrade` the image runs at startup. Without it that migration runs as the runtime role (fine for single-role/self-host deploys, wrong for the two-role model) |
| `INVINCIBLE_OWNER_SECRET` | **yes** | Signs account browser sessions (dashboard login, OAuth consent). Unset ⇒ every browser surface fails closed: no login, no consent |
| `INVINCIBLE_CREDENTIAL_KEY` | **yes** | Fernet key that encrypts stored BYOK provider credentials. Generate with `invincible secret credential-key`, **back it up**: losing it makes every saved provider key undecryptable, and startup warns loudly when it is missing |
| `INVINCIBLE_AGENT_ROUTING` | **yes on any public deployment** | `1` routes confirmed tool execution to each user's own paired agent. Unset, tools execute on the **server host** under the server's privileges — correct only for a one-person self-host, indefensible when strangers can register ([SECURITY.md](SECURITY.md) §10) |
| `INVINCIBLE_PERSIST_PENDING_ACTIONS` | optional | Set it so staged `execute_bash`/`write_file` approvals survive a restart instead of being orphaned (memory-only by default) |
| `INVINCIBLE_GITHUB_CLIENT_ID` / `_SECRET` | optional | Enables GitHub login. Register `<public base URL>/auth/github/callback` as the app's callback URL; both must be set |
| `PORT` | platform-defined | Railway/Heroku-style platforms inject it; the container command binds it |

Everything else (`INVINCIBLE_MEMORY*`, `INVINCIBLE_RELAY*`,
`INVINCIBLE_COMPRESSION*`, `INVINCIBLE_CONTINUITY`, …) is an optional
behavior toggle with sane defaults — see [CONFIGURATION.md](CONFIGURATION.md).

---

## 3. Start command (ports, proxy headers, health)

The shipped commands are the reference; use them verbatim unless the
platform requires otherwise.

- **`Dockerfile`** (generic container / Azure Container Apps):
  `invincible db upgrade` (against `INVINCIBLE_MIGRATE_DB_URL` when set),
  then
  `uvicorn invincible.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'`.
- **`railway.json`**: `startCommand` uses `--port $PORT` plus the same
  proxy flags, with `healthcheckPath: /health` and a 120s healthcheck
  timeout.
- **`Procfile`**: the same uvicorn line for Heroku-style platforms.

Why those flags matter:

- `--host 0.0.0.0` — the container must accept traffic from the platform's
  proxy, not just its own loopback.
- `--proxy-headers --forwarded-allow-ips '*'` — the platform terminates
  TLS, so the app has to trust `X-Forwarded-Proto` to know the request
  arrived over HTTPS. That is what makes the account session cookie
  `Secure` (and GitHub's state cookie too): without it, the scheme reads
  `http` behind the proxy.
- **No tunnel, no browser auto-open** on a deployed host — those
  `invincible start` flags are laptop conveniences; `INVINCIBLE_TUNNEL_NAME`
  is irrelevant remotely.

Health endpoints (unauthenticated, cheap): `GET /health` →
`{"service": "Invincible", "status": "ok", "version": …}`, plus `HEAD /`
(the base-URL probe some clients send first).

---

## 4. Database (the two-role model)

Required for anything beyond an isolated dev loopback; the full statement of
the model is [SECURITY.md](SECURITY.md) §8.

1. **Two non-superuser roles.** `invincible_migrate` owns the database and
   schema and is the only role that may run `invincible db upgrade`;
   `invincible_app` is the runtime role with SELECT/INSERT/UPDATE/DELETE +
   sequence `USAGE` only — DDL is denied. The bootstrap superuser is used
   once, at provisioning, and never again. Reference grants ship in
   `docker/db-init/01-roles.sh`.
2. **Password auth enforced** (`scram-sha-256`). `trust` auth is acceptable
   only on an isolated dev loopback; on a shared host it makes the DSN
   password decorative.
3. **Persistent storage with backups.** Use the platform's automated
   backups and confirm they restore. Conversation history is stored in
   **plaintext**, so database access control *is* the protection — see
   [SECURITY.md](SECURITY.md).
4. **Migrations are explicit.** `invincible db upgrade` is the only path
   (the image runs exactly one at startup). The app never auto-migrates;
   startup logs a loud warning when `alembic_version` is missing or behind
   head, and `invincible doctor` fails on a stale or unmanaged schema.

`invincible dev-db` is not a provisioning path here — it is loopback-only
with dev-credential roles, meant for laptops and the test suite.

---

## 5. Run it as a single instance

These live in **process memory**, by design:

- provider failure counters and cooldowns,
- staged MCP approvals — unless `INVINCIBLE_PERSIST_PENDING_ACTIONS` is set,
- the agent registry (which machine belongs to which user).

So: **no horizontal autoscaling, no multiple replicas.** Run one instance
(min replicas 1) and let clients retry across restarts — the agent's
long-poll loop *is* its retry. Also check the platform's request timeout:
the longest held request is a dispatched agent job (the action's own
timeout plus a 10s grace, ~40s for the default 30s command), so a proxy
that cuts requests sooner surfaces as a failed tool call, not a crash.

---

## 6. Users, access, and operations

- **Registration is open.** Any visitor to `/auth/register` becomes a plain
  user; there is no invite gate and no operator/admin role — the operator's
  powers are the environment variables and the CLI. Treat every public
  deployment as multi-tenant by strangers and keep
  `INVINCIBLE_AGENT_ROUTING=1`.
- Each user manages their own projects, `inv_` API keys, provider
  credentials, sessions, memory, and MCP clients on the dashboard.
- **Password reset is operator-side only** (`invincible users
  reset-password <email>` against the deployment's `INVINCIBLE_DB_URL`) —
  there is no mail infrastructure by design.
- Host recovery/debug tools that need database access: `invincible users
  list`, `invincible api-key create|list|revoke --user …`, `invincible oauth
  list|revoke <client_id>`, `invincible doctor` (always prints the DSN
  masked). GitHub-only accounts set a first password from the account
  settings page, and any password change invalidates that user's other
  browser sessions.

---

## 7. Go-live checklist

Deployment acceptance criteria live in [ROADMAP.md](ROADMAP.md) §Phase 7;
this is the operational version.

**Before cutover**

- [ ] Managed, persistent PostgreSQL provisioned; backups enabled and a
      restore tested once.
- [ ] Two roles created (`invincible_migrate`, `invincible_app`) with the
      grants from `docker/db-init/01-roles.sh`; password auth enforced.
- [ ] All secrets generated for this environment and stored in the
      platform's env settings (never reused from dev).
- [ ] `INVINCIBLE_AGENT_ROUTING=1` set.
- [ ] `invincible db upgrade` run once against the new database as the
      migrate role (the image does this at startup when
      `INVINCIBLE_MIGRATE_DB_URL` is set).

**After cutover**

- [ ] `GET /health` → `{"service":"Invincible","status":"ok",…}`.
- [ ] `https://<domain>/` serves a valid certificate; `HEAD /` → `200`.
- [ ] Register a throwaway account, log in, confirm the session cookie is
      marked `Secure` in the browser.
- [ ] Connect a BYOK provider for that account and complete a real
      `/v1/chat/completions` round-trip; confirm `x-invincible-*` headers
      name the serving credential.
- [ ] Garbage `Authorization: Bearer` on `/v1/models` → `401` (auth gate
      armed; there is no anonymous path).
- [ ] `/mcp` without a token → `401` with the RFC 9728 challenge pointed at
      your domain.
- [ ] Pair `invincible agent` from a laptop against the deployment, approve
      a staged command, and confirm the dashboard shows **Agent: online**.
- [ ] `invincible doctor` against the production DSN: secrets present,
      schema at head, DSN masked.
- [ ] Delete the throwaway account's data if you do not want to keep it.

**Ongoing**

- [ ] Keep one instance running (no autoscaling), and re-run
      `invincible db upgrade` on every release that ships a migration.
- [ ] Back up `INVINCIBLE_CREDENTIAL_KEY` and `INVINCIBLE_OWNER_SECRET`
      somewhere you can restore them from.
- [ ] Revoke stale MCP clients with `invincible oauth list` /
      `oauth revoke <client_id>`.

---

## 8. Related docs

- [SECURITY.md](SECURITY.md) — threat model, auth realms, the two-role
  database model (§8), the agent-routing mandate (§10), known limits.
- [CONFIGURATION.md](CONFIGURATION.md) — every environment variable and the
  CLI reference.
- [MCP_PROTOCOL.md](MCP_PROTOCOL.md) — connecting MCP clients to the
  deployed URL.
- [MIGRATION-AZURE.md](MIGRATION-AZURE.md) — the concrete host-to-host
  migration checklist in progress for `invincible-ai.me`.

