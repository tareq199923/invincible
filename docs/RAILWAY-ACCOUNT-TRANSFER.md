# Railway Account Ownership Transfer

## Purpose

Production remains on Railway + Neon at `invincible-ai.me`. The current
operational task is to transfer Railway project ownership to a different
Railway account. Neon remains the production database for the two-year term;
there is no current Azure or AWS migration.

This is an ownership and operations handoff, not a database migration. The
Neon database, database roles, domain, and application deployment should
remain in place unless a separate change is explicitly planned.

## Current production

| Component | Current owner/setup | Notes |
|---|---|---|
| Application | Railway, `invincible-gateway` service | Container deployment using `railway.json`; `/health` healthcheck |
| Domain | `invincible-ai.me` | Cloudflare CNAME → Railway, DNS-only mode; keep the current DNS target unless Railway requires a service-host change |
| Database | Neon, `ep-bold-field-azoysw7g`, ap-southeast-1 | `invincible_app` runtime role and `invincible_migrate` schema role |
| Secrets | Railway service variables | `INVINCIBLE_DB_URL`, `INVINCIBLE_OWNER_SECRET`, `INVINCIBLE_CREDENTIAL_KEY`, `INVINCIBLE_AGENT_ROUTING=1`, optional migration/persistence/OAuth variables |

There is no shared gateway key. The server does not hold provider keys:
each user connects their own provider through BYOK. Never paste secret values
into this runbook or into commit messages.

## Transfer checklist

### 1. Prepare the destination account

- [ ] Confirm the destination Railway account and its owner are available.
- [ ] Record the current Railway project, service, deployment method, region,
  healthcheck, and environment-variable names without copying secret values
  into documentation.
- [ ] Confirm the destination account will preserve the existing project,
  service, deployment settings, and access to the production environment.
- [ ] Confirm the current Railway account remains available during the
  handoff and verification window.

### 2. Transfer Railway project ownership

- [ ] Use Railway's current project/account ownership-transfer controls to
  move the project to the destination account.
- [ ] Confirm the destination owner can view and manage the service,
  deployments, variables, logs, and environment settings.
- [ ] Confirm the deployment source repository and branch are still correct.
- [ ] Do not change the Neon project, database roles, or database DSN as part
  of the account transfer.
- [ ] Do not change DNS merely because the Railway account changed. If the
  transfer changes the Railway service hostname, update DNS only after the
  destination deployment is healthy and record the old target for rollback.

### 3. Verify the destination deployment

- [ ] `GET /health` returns the expected service, status, and version.
- [ ] `HEAD /` returns `200`.
- [ ] `https://invincible-ai.me` serves a valid certificate and reaches the
  destination Railway deployment.
- [ ] `invincible doctor` reports the expected secrets, masked database URL,
  and schema revision.
- [ ] The production environment still has `INVINCIBLE_AGENT_ROUTING=1`.
- [ ] Register or use a test account, mint a per-user `inv_` key, connect a
  BYOK credential, and complete one real `/v1/chat/completions` round-trip.
- [ ] Pair a local `invincible harness connect` and confirm the dashboard reports the
  expected agent online.
- [ ] Confirm `INVINCIBLE_AGENT_ROUTING=1` remains set after the transfer; an
  unset flag on a public deployment would route confirmed tools to the
  server host.

### 4. Close the handoff

- [ ] Deploy or restart from the destination account using the normal Railway
  workflow.
- [ ] Wait for the build and healthcheck to complete; the old version may
  remain visible briefly during a Railway build-and-swap.
- [ ] Re-run the production smoke checks from the public domain.
- [ ] Keep the previous Railway account access until all checks pass.
- [ ] Remove old-account access according to the organization's ownership
  policy.
- [ ] Retain the current Neon project and backups. Do not delete Neon as part
  of this account handoff.

## Rollback

Until ownership transfer and verification are complete, keep the current
Railway deployment available as the rollback path. If the destination account
is unhealthy, stop further promotion there and restore access/deployment
control through Railway's normal project and deployment controls. Do not
change the database or delete the current deployment during rollback.

If Railway changes the service hostname during the transfer and the public
domain fails, restore the previous Railway hostname at Cloudflare only after
confirming the old deployment is still healthy.

## Scope boundary

This transfer does **not** include:

- moving from Railway to Azure, AWS, or another application host;
- migrating away from Neon;
- changing database data, roles, or schema;
- changing the product's authentication or deployment configuration;
- changing the local development/test PostgreSQL clusters.

Those remain separate future decisions. The host-agnostic deployment and
security requirements remain in [DEPLOYMENT.md](DEPLOYMENT.md) and
[SECURITY.md](SECURITY.md).

## References

- [DEPLOYMENT.md](DEPLOYMENT.md) — required production variables, database,
  single-instance posture, and go-live checklist.
- [SECURITY.md](SECURITY.md) — authentication realms, BYOK isolation, agent
  routing, and deployment posture.
- [ROADMAP.md](ROADMAP.md) — Phase 7 deployment status and ownership-transfer
  direction.
- `railway.json` — the shipped Railway deployment configuration.
