# Migration Plan — Azure for Students (Railway/Neon trial ends ~2026-10-02)

> Created 2026-09-04. The Railway + Neon production setup that went live
> 2026-09-02 runs on free trials that expire around **October 2, 2026**.
> This is the countdown checklist for moving `invincible-ai.me` to
> Azure for Students before that deadline.
>
> **2026-09-21 update:** the auth and env facts below were corrected against
> the current code — the shared `GATEWAY_API_KEY` no longer exists (removed
> with the operator role; `/v1/*` takes per-user `inv_` keys only), and the
> server holds **no provider keys** (users connect their own, BYOK). The
> host-agnostic runbook for any remote target is
> [DEPLOYMENT.md](DEPLOYMENT.md).

## Deadline

- **~2026-10-02**: Railway trial credits and Neon free-tier limits end.
- After expiry the gateway goes dark unless migrated or paid.
- Start the migration **at least a week early** (~2026-09-25) — DNS
  cutover and database transfer take longer than expected.

## Current production setup (what must move)

| Component | Current host | Notes |
|---|---|---|
| App (uvicorn/FastAPI) | Railway, `invincible-gateway` service | Dockerfile deploy, healthcheck `/health` |
| Domain | `invincible-ai.me` | Cloudflare CNAME, DNS-only mode |
| Database | Neon ap-southeast-1, `ep-bold-field-azoysw7g` | Two roles: `invincible_app` (runtime), `invincible_migrate` (schema) |
| Env vars | Railway service variables | `INVINCIBLE_DB_URL` (runtime role), `INVINCIBLE_OWNER_SECRET`, `INVINCIBLE_CREDENTIAL_KEY`, `INVINCIBLE_AGENT_ROUTING=1`, plus optional `INVINCIBLE_MIGRATE_DB_URL`, `INVINCIBLE_PERSIST_PENDING_ACTIONS`, and the GitHub OAuth pair — read them with `railway variables --kv`. There is **no gateway key** (removed) and **no provider keys on the server**: every user connects their own (BYOK). Canonical list: [DEPLOYMENT.md](DEPLOYMENT.md) §2 |

## Migration checklist

### 1. Azure for Students signup (~Sept 25)
- [ ] Sign up with the student email at azure.microsoft.com/free/students
- [ ] Verify the $100 credit + 12-month free services are active
- [ ] Note: no credit card required for the Students tier

### 2. New database (target: Azure Database for PostgreSQL — Flexible Server)
- [ ] Provision PostgreSQL Flexible Server (region close to ap-southeast-1 if possible; else pick lowest latency)
- [ ] Create the two roles (mirroring the Neon setup):
      `invincible_migrate` (schema owner) and `invincible_app` (CRUD-only)
- [ ] Dump the Neon database:
      `pg_dump` as `neondb_owner` → restore into Azure PG as `invincible_migrate`
- [ ] Update `INVINCIBLE_DB_URL` to the Azure DSN
      (asyncpg driver — no `?sslmode=` param; Neon's pooled DSN quirk does
      not apply, Azure wants `ssl=require` handled per asyncpg defaults)
- [ ] Run `invincible db upgrade` against Azure PG to confirm migrations match

### 3. App hosting (target: Azure Container Apps or App Service)
- [ ] Container Apps (recommended): deploy the same Dockerfile, min replicas 1
- [ ] Set the required env vars (copy from `railway variables --kv` before
      the Railway trial dies — after that they are unreachable):
      `INVINCIBLE_DB_URL`, `INVINCIBLE_OWNER_SECRET`,
      `INVINCIBLE_CREDENTIAL_KEY`, `INVINCIBLE_AGENT_ROUTING=1`, and
      `INVINCIBLE_MIGRATE_DB_URL` (schema owner) — see
      [DEPLOYMENT.md](DEPLOYMENT.md) §2
- [ ] Healthcheck: `/health` (already in `railway.json`, replicate it)
- [ ] Confirm autoscale min instances = 1 so the site stays up on zero traffic

### 4. DNS cutover (`invincible-ai.me`)
- [ ] Cloudflare: retarget the CNAME from Railway to the Azure app domain
- [ ] Keep DNS-only mode (Cloudflare proxy stays off, same as now)
- [ ] TLS: Azure provides certs for its default domain; verify
      `https://invincible-ai.me` serves a valid cert after cutover
- [ ] Allow a few minutes of DNS propagation, then test:
      `/health`, `/v1/models` (with gateway key), and a full
      `/v1/chat/completions` round-trip

### 5. Verification (same smoke tests as Phase 7)

Full checklist: [DEPLOYMENT.md](DEPLOYMENT.md) §7.

- [ ] `GET /health` → `{"service":"Invincible","status":"ok",…}`
- [ ] Register a throwaway account, log in, and mint an `inv_` key on the
      Account page
- [ ] `GET /v1/models` with `Authorization: Bearer inv_…` (that account's
      key) → 200 after the account has connected its own provider credential
- [ ] Garbage key → 401 (auth gate armed; per-user `inv_` keys only — the
      shared gateway key no longer exists)
- [ ] One BYOK provider round-trip through `/v1/chat/completions`
- [ ] `INVINCIBLE_AGENT_ROUTING=1` present in the new host's variables, and
      a paired `invincible agent` shows online on the dashboard MCP page

### 6. Cleanup
- [ ] Shut down the Railway service (avoid surprise charges if it converts to paid)
- [ ] Export/backup the Neon data one final time before deleting the project
- [ ] Update `docs/ARCHITECTURE.md` and the project memory file
      (dev PG on 5433 stays — it's dev/test-only)

## Rollback

If Azure misbehaves before Oct 2, the Railway deployment still runs —
point DNS back and investigate. After Oct 2 there is no rollback; only
fresh redeploys.

## Reference

- Env source of truth: `railway variables --kv` (while the trial lives)
- Deploy config: `railway.json` (Dockerfile builder, healthcheck settings)
- DB topology: two-role least-privilege design from Phase 7
  (runtime role owns nothing, schema migrations run as `invincible_migrate`)
