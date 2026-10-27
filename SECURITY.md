# Security Policy

Invincible is a gateway for provider credentials and a remote tool-execution
surface, so security reports are taken seriously and handled privately.

> This file is the **reporting policy**. The full threat model — auth realms,
> credential encryption, denylists, approval flow, sandbox limits, and known
> limitations — lives in [docs/SECURITY.md](docs/SECURITY.md).

---

## Supported versions

| Version | Supported |
|---|---|
| `0.6.x` (latest release) | ✅ security fixes |
| `0.5.x` and older | ❌ upgrade first |

The project is pre-1.0. Fixes land on `main` and in the next patch release;
there are no backports to older minor versions. Self-hosters should run
`pip install -U invincible-ai` followed by `invincible db upgrade`.

---

## Reporting a vulnerability

**Do not open a public issue, discussion, or pull request for a security
problem.**

Preferred channel — GitHub private vulnerability reporting:

<https://github.com/tareq199923/invincible/security/advisories/new>

Fallback — email **jahidulislam199923@gmail.com** with the subject
`[SECURITY] invincible`.

Please include:

- affected version (`invincible --version`) and deployment type (hosted,
  self-hosted, local agent),
- a description of the issue and the impact you believe it has,
- reproduction steps or a proof of concept,
- any logs or requests, with secrets and API keys removed.

Do **not** test against other people's deployments, and do not run destructive
commands on machines you do not own.

### What to expect

| Stage | Target |
|---|---|
| Acknowledgement of your report | within 3 working days |
| Initial triage and severity assessment | within 7 working days |
| Fix or mitigation plan | within 30 days for high/critical, best effort otherwise |
| Public disclosure | coordinated, usually 90 days after the report |

We will credit you in the advisory unless you ask to stay anonymous.

---

## Scope

In scope:

- authentication and authorization across the three realms — `inv_` API keys
  (`/v1/*`), account session cookies (dashboard, `/auth/*`, OAuth consent),
  and OAuth bearer tokens (`/mcp`);
- cross-user access to sessions, memory, continuity state, projects, API keys,
  provider credentials, or runs;
- BYOK credential storage and the `INVINCIBLE_CREDENTIAL_KEY` handling;
- the MCP tool pipeline: denylist bypass that reaches execution without
  approval, approval-token reuse or forgery, and agent-routing escapes that
  execute on the server instead of the user's machine;
- the local agent sandbox (`INVINCIBLE_AGENT_ROOT`) — reading or writing
  outside the configured root;
- the built-in OAuth 2.1 server: PKCE bypass, redirect-URI handling, token
  leakage;
- SSRF protections for user-supplied provider URLs;
- the dashboard and docs site: XSS, CSRF, template injection, open redirects.

Out of scope, by design and documented in [docs/SECURITY.md](docs/SECURITY.md):

- denylist evasion through shell wrappers (`powershell -Command`, `bash -c`,
  …). The denylist is a text-pattern filter, not a parser; the approval token
  is the real boundary. A bypass that skips the **approval** step is in scope;
  a bypass that merely evades the pattern list is not.
- anything that requires a valid account plus the ability to approve your own
  tool calls on your own machine;
- self-hosting misconfiguration (exposed `.env`, missing
  `INVINCIBLE_AGENT_ROUTING=1` on a public multi-user deploy, a public
  database port). See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md);
- denial of service, rate-limit complaints, and spam;
- vulnerabilities in upstream providers or third-party MCP clients;
- findings from automated scanners without a demonstrated impact.

---

## Safe harbor

We will not pursue or support legal action against researchers who:

- act in good faith and follow this policy,
- test only against their own deployment or account,
- avoid privacy violations, data destruction, and service disruption,
- give us reasonable time to fix the issue before public disclosure.

---

## Hardening checklist for operators

If you run Invincible for other people, these are required, not optional:

- `INVINCIBLE_AGENT_ROUTING=1` — otherwise confirmed tool calls execute on the
  server host;
- a single instance — provider cooldowns, staged approvals, and the agent
  registry are in-process;
- PostgreSQL over TLS, CRUD-only role for the app, separate schema-owner role
  for migrations;
- HTTPS terminated in front of the app, with proxy headers trusted;
- `INVINCIBLE_OWNER_SECRET` and `INVINCIBLE_CREDENTIAL_KEY` backed up and never
  committed;
- `invincible doctor` clean, with no stale or unmanaged schema.

The go-live checklist is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
