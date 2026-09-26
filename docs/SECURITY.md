# Security Model

Invincible exposes three attack-relevant surfaces: a chat proxy that calls
upstream AI providers, a browser dashboard managing accounts and provider
credentials, and an MCP tool server that can **run shell commands
and write files on the host machine**. This document describes exactly what
guards what, where the boundaries are, and — explicitly — where they are not.

---

## 1. Three independent auth realms

The three surfaces authenticate **independently**. Rotating one realm's
credential never affects the others, and no credential is accepted outside
its own realm.

> **Renamed earlier, re-scoped in Phase 2:** the old per-request MCP secret
> `MCP_SHARED_SECRET` became `INVINCIBLE_OWNER_SECRET`, then Phase 2 removed
> the owner-secret *login* entirely. The variable survives with one job
> only: the HMAC key source signing account browser sessions
> (`core.accounts SessionManager`). It is never sent on `/mcp` and never
> typed into any form. The pre-rename `MCP_SHARED_SECRET` alias was
> retired after the Phase 8 consent retirement: only
> `INVINCIBLE_OWNER_SECRET` signs sessions now, and a stale
> `MCP_SHARED_SECRET` value is inert (rename it to keep browser
> sessions working).

### `/v1/*` — per-user API keys only (fail closed)

Every chat request (`/v1/chat/completions`, `/v1/messages`, `/v1/responses`,
`/v1/models`, and the session-graph API) authenticates with a per-user
`inv_` API key — implemented in `invincible/endpoints/auth.py::require_auth`:

| Step | Realm | Result |
|---|---|---|
| 1 | `Authorization: Bearer inv_…` or `x-api-key`, matching an **API key** (SHA-256 hash lookup; revoked keys excluded) | That key's user + its default project (`kind="api_key"`) |
| 2 | anything else | HTTP 401, body `{"detail": {"error": {"message": "...", "type": "auth_error"}}}` |

There is no shared gateway key, no operator realm, and no anonymous
fallback: the endpoint always fails closed. API-key properties:

- Raw values are shown **once**, at creation (dashboard API-keys page, or
  `invincible api-key create --user …` on the host); storage keeps only a
  SHA-256 hash plus a visible prefix for listings.
- Every authenticated request routes **only through that user's own
  connected BYOK credentials** (see the BYOK section below) — with zero
  credentials connected the request fails fast with a clear 400, and there
  is no shared pool to fall back to in either direction.
- Sessions created under an API-key principal are stored under that user's
  ownership triple (`user_id`, `project_id`, `client_session_id`) — the
  same client session string under two users yields two distinct session
  rows, and a foreign string reads exactly like a nonexistent one
  (anti-enumeration).

### `/mcp` — OAuth 2.1 + PKCE Bearer tokens

`/mcp` does not take a shared secret header. It accepts **short-lived
access tokens** (`Authorization: Bearer <token>`, ~1h TTL) issued by
Invincible's own, built-in authorization server (`/oauth/*`) after a
browser-based login and per-client consent. `inv_` API keys are
deliberately **not** accepted here: MCP grants must always pass the
browser gate, so a leaked API key can never run
`execute_bash`/`write_file`.

**Consent identity (Phase 2, self-service; sole-identity path retired in
Phase 8).** A valid dashboard session cookie (`invincible_session`) grants
consent **as that logged-in user** — the consent page names the identity,
and tokens minted from the approval act as that user's subject. Every user
approves their OWN clients; there is no operator gate and no owner-secret
login anymore (the old owner-secret cookie path was removed in Phase 2).
Since Phase 8 the subject is mandatory at issuance
(`OAuthStore.create_code` / `issue_token_pair` require it; legacy
subject-less refresh rows are refused) — pre-existing subject-less rows
fail closed at `require_mcp_auth`, which has always resolved through
`subject_user_id`. The owner secret itself stays, with one job only:
the HMAC key source signing account browser sessions.

**Why self-approval is safe here.** Approval mints MCP bearer tokens, and
on a server with `INVINCIBLE_AGENT_ROUTING=1` confirmed tool execution
routes to the approver's own paired agent — never the server host — so
approving a client exposes only the approver's own machine. The
deployment-posture rule (§10) makes that flag a hard requirement on any
public multi-user deployment. Approving/denying is only possible via the
POST forms — a GET carrying an `action` is rejected, so a cross-site
navigation can never grant consent (the SameSite=Lax session cookie is
sent on top-level GETs, which made GET links CSRF-able). Session
resolution on the consent endpoints is the full principal check —
signature, expiry, live user row, and `session_version` match — so a
password-orphaned or deleted-account cookie behaves exactly like a forged
one, and an anonymous browser bounces to `/login` with the authorize URL
as the same-origin `next` target.

| Aspect | Value |
|---|---|
| Auth | `Authorization: Bearer <access_token>` |
| Token source | built-in `/oauth` server (RFC 7591 / 8414 / 9728, PKCE public client) |
| Access token TTL | ~1 hour; refresh token ~30 days (rotated on every use) |
| Discovery | `/.well-known/oauth-authorization-server` + `/.well-known/oauth-protected-resource` |
| Failure | HTTP 401 with `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"` — MCP-compatible clients auto-discover the authorization server from this instead of failing silently |
| If no tokens exist | 401 (never open) — a fresh grant requires the browser gate |

Implemented in `invincible/endpoints/mcp.py::require_mcp_auth` (resource
server) and `invincible/endpoints/oauth.py` (authorization server).

### Accounts — browser sessions + per-user management (Platform Phase 3)

| Property | Value |
|---|---|
| Surface | `/auth/*`, `/projects*`, `/api-keys*`, `/sessions` (Phase 3) |
| Auth | `invincible_session` cookie: `v1.<uid>.<expiry>.<HMAC-SHA256>`, HttpOnly, SameSite=Lax; key derived from `INVINCIBLE_OWNER_SECRET` |
| Failure mode | **Fail closed** — with no owner secret configured the HMAC key would be publicly computable, so no session is ever issued or accepted (503) |
| Management alt-realm | A user's own `inv_` API key also works on `/api-keys`; MCP bearer tokens are rejected by construction (`ApiKeyStore.resolve` matches only `inv_` hashes) |

Properties of this realm:

- Passwords are argon2id-hashed. Login failures are enumeration-safe
  (unknown email ≡ wrong password) and feed a persistent per-IP lockout in
  its own `login_attempts` scope (`auth-login`) so hammering one form never
  locks the other.
- Registration is an explicit duplicate-email 409; only the login path
  stays silent about account existence.
- **GitHub login** uses an OAuth App authorization-code flow. GitHub OAuth
  Apps have no PKCE, so CSRF is handled with a signed single-use state
  cookie. Only *verified* primary emails may auto-link to an existing local
  account or auto-register a new one (GitHub-only accounts keep
  `password_hash` NULL). Once an account owns a GitHub identity, a second,
  different GitHub identity claiming the same verified email is refused
  instead of silently attached.
- Device pairing (`/auth/device/*`, used by `invincible login`) stores only
  the SHA-256 hash of the device code; the short human-typed user_code must
  be approved by a logged-in browser session via POST forms; approval is
  single-winner and the minted API key raw value appears exactly once, in
  the successful token poll.
- **Dashboard memory management** (`/dashboard/memory`, `/memories*`;
  Phase 5) lives in this same cookie realm. Every path takes a mandatory
  ownership predicate (`user_id` on the row - there is no local-owner
  fallback), deletes are id-addressed with foreign and unknown ids
  returning byte-identical 404 bodies (existence never leaks across
  users), and both creation and deletion are written to the audit log.
  The `INVINCIBLE_MEMORY` kill-switch gates only *creation*: browse and
  delete stay available so toggling off can never trap already-saved
  data. Search reuses the retrieval tsvector path scoped to the single
  owner; the AND→OR fallback therefore cannot widen scope, only recall.
- **Password set/change** (`POST /auth/password`; Phase 5) follows the
  STORED account state, never caller-chosen fields. An account whose
  `password_hash` is NULL (GitHub-only today) may set a FIRST password
  with no current required — and nothing else can be overwritten through
  that path (`set_password` guards on the NULL hash inside the UPDATE).
  Every other account must present its correct current password;
  failures collapse into one bounded `wrong_password` shape. Both flows
  share registration's minimum length, surface HTML errors as fixed
  `pw_error` codes (attacker-controlled text is never echoed), and write
  `password.set` / `password.changed` audit rows. Both flows bump
  `users.session_version` **inside the same UPDATE** as the hash; `v2`
  session cookies carry the version they were minted against and
  Principal resolution rejects mismatches — so every OTHER device's
  session dies with the password, while the acting browser is re-issued
  a live cookie on success. `inv_*` API keys are deliberately untouched
  (see limit 14).

### BYOK provider connections — cookie realm (Platform Phase 9)

| Property | Value |
|---|---|
| Surface | `/dashboard/providers` (HTML), `/providers/mine*` (JSON API) |
| Auth | `invincible_session` cookie ONLY — `inv_*` API keys are rejected (401), mirroring the settings surface |
| Failure mode | **Fail closed twice**: no session → 401; no usable `INVINCIBLE_CREDENTIAL_KEY` → 503 on every route, before any credential is read or written |

- Stored API keys are Fernet-encrypted at rest under
  `INVINCIBLE_CREDENTIAL_KEY` (encryption model and limits: §9). The
  plaintext exists only in the create-request body and, transiently, in
  the per-attempt decrypt path (§9). Responses, templates, audit rows,
  and logs carry only the one-way `key_masked` hint (first 3 + last 4;
  keys shorter than 12 chars are fully masked).
- Deleting/testing is ownership-predicated: foreign and unknown
  credential ids return byte-identical 404 bodies (no enumeration).
- **Routing (per-user only):** every `/v1/*` request builds its attempt
  list ENTIRELY from the authenticated user's connected credentials — with
  zero credentials the request fails fast with a clear 400 — and there is
  no shared operator pool to fall back to in either direction. Health
  cooldowns are keyed per-credential (`byok:<id>`), so users can never
  poison each other's cooldown state, and the per-attempt key resolver
  re-runs the SSRF guard and decrypts lazily, one credential at a time.
- `base_url` for non-catalog (user-typed) providers must be `https://`
  and resolve to a public address; the SSRF guard re-checks on EVERY
  later test use, not just at create (details: §9). Catalog base URLs
  are packaged constants and skip the check only while unedited.
- Audit rows (`byok.credential.created/tested/deleted`) carry
  `provider_name` + `catalog_key` + id only — never the key, and never
  the base URL (a URL may embed auth parameters).

### The layering principle

`tool_executor.py` (the code that actually runs commands and writes files)
**assumes the caller is already authenticated**. It decides only whether a
specific action is safe and approved — never who is allowed to ask. Auth is
entirely the endpoint dependency's job, one layer up. The OAuth swap does
not touch the denylist or pending-approval logic; the only change is *which
credential* proves you are authenticated.

---

## 2. The `/mcp` gate order

For `/mcp`, the auth model is **three gates in order**:

```
1. account login (once, browser)   2. consent (per client)     3. bearer token (per call)
Email + password on /login   →     Approve on consent page  →  access token sent on every
(or GitHub); signed                 as that user; issues        /mcp call; ~1h TTL,
invincible_session cookie          a single-use               revocable, hash-stored
                                   authorization code
        │                                  │                          │
        └─────────────── OAuth 2.1 + PKCE ───────────────────────────┘
```

For `execute_bash` and `write_file`, two further gates run after auth:

```
authenticated caller (valid bearer access token)
        │
        ▼
1. Denylist  ── matches? ──► ToolBlocked  → "Blocked: <reason>"  (no token issued)
        │ no
        ▼
2. Approval ── staged as pending action with an unpredictable token
        │    ── caller must call confirm_action(token, approve) over /mcp
        │    ── approve=false / unknown / expired token → nothing runs
        │ true
        ▼
3. Execution (with 30s timeout for commands)
```

`read_file` has no approval step (reading is non-destructive); its
denylist is the only gate after auth. The H6a read-only tools inherit
that posture: `code_search` shares `read_file`'s sandbox (server read
roots, agent home when routed; binaries, secret/state names, and files
over 256KB skipped), `process_list` carries no path and runs wherever
tools execute — server-local output on a shared host is operator-visible
by design, which is why public deploys must route execution to paired
machines (`INVINCIBLE_AGENT_ROUTING=1`, §10). `screenshot` is agent-only:
the server never fetches caller-supplied URLs, so a token that can call
tools cannot turn the server into an SSRF fetcher (no cloud-metadata or
intranet reads); only `http(s)` URLs render, and only on the approver's
own machine.

### 2.0 Approval remains remote and token-based — the trust boundary

The old flow blocked on a synchronous y/N prompt at the server's terminal,
so only someone with **physical access to the machine** could approve. That
was *replaced, not supplemented*: an `execute_bash`/`write_file` call that
survives the denylist is staged in an in-process `PendingActionStore` and
returns immediately with a `pending_confirmation` response carrying an
unpredictable token (`secrets.token_urlsafe(16)`). Nothing runs until a
second `tools/call` for `confirm_action` arrives with that token,

- `approve: true` → the staged action performs for real.
- `approve: false` → the pending entry is discarded, `Declined.` is
  returned, nothing executes.
- Unknown, expired (10-minute TTL), or already-used token → `Unknown or
  expired confirmation token.`, nothing executes.

**Where the boundary is now.** Approval of a pending action is decided by
whatever the calling client reports back through a second `/mcp` call — the
boundary is **"whoever holds a valid bearer access token"**. Since a token
only exists after its owner — the logged-in account that approved the
client on the consent page — passes the browser gate, this is a **named
trust boundary** in a way the shared secret never
was: a grant is scoped to one registered client, is revocable, and expires
on its own. This mirrors the earlier `confirm_action` trust-boundary change
and is documented here explicitly.

Implications, stated plainly:

- Anyone holding a live access token can approve a staged action — there is
  no separation between "AI client" and "owner" at the protocol level.
  The user's lever is **revocation**: `invincible oauth revoke
  <client_id>` (or the dashboard's MCP page) kills every outstanding
  token for a client instantly.
- A `confirm_action` sent as a **notification** (no `id`) also executes —
  JSON-RPC notifications still run their side effects.
- Pending entries are **persisted across restarts only when explicitly
  opted in**: when the `INVINCIBLE_PERSIST_PENDING_ACTIONS` environment
  variable is set, `PendingActionStore` writes through to the PostgreSQL
  database (`INVINCIBLE_DB_URL`, `pending_actions` table). By default it
  is **memory-only** — a restart orphans every staged action and
  confirmations fail with *Unknown or expired*, the original clean-slate
  design. Persistence means staged shell commands sit in plaintext in the
  database pre-approval, so it is deliberately off unless requested. Since
  Phase 2, staged actions are bound to the staging subject and every
  approval/denial writes an audit row (metadata only - never the raw
  command/path, which could carry secrets).
- The server still prints an informational visibility line for each pending
  action to its own stdout (`[MCP] Pending <token>: …`) — **informational
  only**, it is not a gate.

### 2.0b Memory tools are data-plane — no approval gate

`memory_save` / `memory_search` / `memory_list` touch the caller's own
rows in the `memories` table, not the machine, so they run after auth
with **no denylist and no `confirm_action` staging** — the same risk
class as chat-side "remember this", which is also ungated. What keeps
them safe:

- **Ownership-predicated**: every query carries the OAuth subject's
  `user_id`; a foreign user's rows are indistinguishable from absent
  ones (anti-enumeration, same as every other store path).
- **Audited**: each save writes an audit row
  (`mcp.memory_save.saved`, metadata only — never the content, which
  could carry secrets).
- **Kill-switch**: `INVINCIBLE_MEMORY=0` blocks saving (reads keep
  working so data is never trapped).
- **Bounded responses**: search is capped at 10 results, list at 20 —
  results land in the caller's context window, so they obey the same
  token discipline as prompt injection.
- **No deletion**: there is deliberately no `memory_delete` over MCP —
  erasing history stays a human, dashboard-only action.

### 2.0c Policy gate + runtime spine (harness H2)

`core/harness_policy.py::before_tool_call` is the single pre-execution
entry point for every machine-plane tool: `execute_bash` runs the command
denylist (§2.1), `write_file` the path denylist (§2.2), `read_file` the
read-roots check (§2.3) — the same functions `tool_executor` has always
run, now orchestrated in one place so the MCP dispatcher and the future
supervisor fan-out (H4) cannot drift apart. It adds **no new patterns and
changes no wire shape**: a denial raises the same `ToolBlocked`, mapped to
the same `Blocked: <reason>` result with no token issued. The one routing
rule lives here explicitly: when agent routing is on, the server skips its
own read-roots check for `read_file` (server roots describe the wrong
machine's filesystem) and the agent's home sandbox (`agent/sandbox.py`,
Wall 3) is the gate instead — enforced locally by the runner before
execution. `core/harness_runtime.py::run_workflow` is the event-wrapped
loop spine (policy → emit → execute → checkpoint); policy denials inside
it become structured `tool.failed` results the agent can self-correct
from, never workflow crashes.

### 2.0d Durable approvals — the slow path (harness H5)

Alongside the 10-minute fast path above, `core/harness_approvals.py`
provides a **slow path** for human decisions that take hours or days
(their human-in-the-loop lesson): `suspend` parks a confirmed action as
a `pending_actions` row carrying `suspended_workflow_id` + `deadline`
(default 24h), and `resolve` resumes it later — across process restarts.
Separation is structural, not a flag check: `suspend` is the only writer
of slow-path rows, `resolve` refuses rows with NULL `suspended_workflow_id`
(fast-path tokens), and the fast path's `load_persisted` skips slow-path
rows — neither path can resolve the other's tokens. Unknown, expired,
already-used, wrong-subject, and cross-path tokens all answer
identically (nothing runs), and every definitive outcome deletes its row
(single-use). Resolution returns the staged record to the executor, so
callers must log metadata only — never raw commands/paths. The durable
workflow timeline itself (`workflow_events`, metadata only) is the audit
trail a suspended workflow resumes against.

### 2.1 `execute_bash` denylist — full inventory

Matched against the **full command string**, case-insensitive
(`re.I`). These are text-pattern matches, **not** shell parsing — see
[Known limits](#6-known-limits).

| Pattern (abridged) | Reason |
|---|---|
| `rm` with `-r`+`-f` flags targeting `/`, `~`, or `$HOME` | Recursive force-delete of home or root |
| `rm -r` targeting `/` alone | Recursive delete starting at filesystem root |
| `:(){ :|:& };:` | Fork bomb |
| `dd ... of=/dev/...` | Raw write to a block device |
| `mkfs` / `mkfs.ext4` / any `mkfs.*` | Filesystem format command |
| `> /dev/sd*|nvme*|hd*|disk*` | Redirect writing directly to a disk device |
| `shutdown`, `reboot`, `halt`, `poweroff` (word-boundary) | System power/shutdown command |
| `sudo` (word-boundary) | Privilege escalation via sudo |
| `chmod -R 777 /` (or `chmod 777 /`) | World-writable permissions on filesystem root |
| `chown -R <user> /` | Recursive ownership change on filesystem root |
| `curl|wget ... \| (sudo )?sh|bash|zsh` | Piping a remote download straight into a shell |
| `kill -9 -1` | Kill all processes |
| `> /etc/passwd|shadow|sudoers` | Overwrite of a core system credentials file |
| `rd`/`rmdir`/`del`/`erase` with `/s` flag **and** a drive-root target (`C:\`, `C:\*`, `C:\*.*`) | Recursive delete targeting a Windows drive root |
| `format <letter>:` | Formatting a Windows drive |

Windows notes: flags can appear in either order around the target (`del /s /q
C:\*.*` vs `del /q /s C:\*.*`) — the regexes use lookaheads that scan the
whole command rather than anchoring to a fixed position. A **subdirectory**
target (`rd /s C:\build`, `rm -rf ./build`, `rm -rf /home/user`) deliberately
does **not** match — that is the Windows/Unix equivalent of a local cleanup
and is left to the approval flow, same as any other command.

### 2.2 `write_file` path denylist — full inventory

Blocks writes outright (approval never reached — no token is issued) to
paths that resolve **inside the repo root** and match:

| Pattern (relative, case-insensitive) | Reason |
|---|---|
| `.env` / `.env.*` | Invincible's own secrets file |
| `providers.yaml` | Provider configuration |
| `sessions.db` | Legacy local store file — still denied so leftover/pre-migration files can't be touched (live state is PostgreSQL) |
| `invincible/` (any file under it) | Invincible's own source code |
| `tests/` (any file under it) | The test suite |
| `.git/` (any file under it) | Git internals |

> Live state — conversations **and** OAuth grants — sits in **PostgreSQL**
> (`INVINCIBLE_DB_URL`). The security boundary moved from a filename to the
> database credentials: treat the DSN like a secret, and note that
> `invincible doctor` always prints it **password-masked**. Tokens are
> stored **SHA-256 hashed**, so a leaked database dump still yields no
> usable bearer tokens. The `sessions.db` denylist entries remain so
> leftover pre-Phase-16 files can never be read or written by the tools.

### 2.3 `read_file` denylist — full inventory

Narrower than the write list **on purpose**: allowing a cloud AI to *see* the
source code is the entire point of the tool, and `providers.yaml` only holds
`api_key_env` **names**, not actual key values, so it is not a secret. Only
things that would leak an actual credential or sensitive local state over the
tunnel are blocked:

| Pattern (relative, case-insensitive) | Reason |
|---|---|
| `.env` / `.env.*` | Invincible's own secrets file |
| `sessions.db` | Legacy local store file (plaintext history pre-Phase-16) — still blocked as a leftover guard |
| `.git/` (any file under it) | Git internals (history may contain secrets) |

Everything else — including `invincible/`, `tests/`, and `providers.yaml` —
**is** readable without approval.

### 2.4 Path resolution rules

- The repo root is resolved from `tool_executor.py`'s own location (three
  `dirname()` calls up), so it works from a checkout, an editable install, or
  a wheel.
- A candidate path is `os.path.abspath()`-ed and relativized to the repo
  root:
  - **Inside the repo** → patterns matched against the relative path.
  - **Outside the repo** (relpath starts with `..`) → not denied; for
    writes, the approval step is the gate (explicitly a different risk
    profile).
  - **Different Windows drive** (`ValueError` from `relpath`) → not inside
    the repo, not denied.
- Matching is case-insensitive on purpose: Windows treats `.env` and `.ENV`
  as the same file, so a differently-cased target must not slip past.
- A trailing `/` on a pattern like `invincible/` only matters for the
  relative path prefix — `invincible\main.py` works because the relativized
  path has separators normalized to `/` first.

---

## 3. The approval flow (`confirm_action`)

Every `execute_bash` and `write_file` call that survives the denylist is
**staged, not run**. The server prints an informational line to its own
stdout and returns a token to the caller:

```
[MCP] Pending 3fKq...Wx9: execute_bash "rm -rf ./build"
[MCP] Pending 9aZt...Qw2: write_file C:\Users\me\project\scratch\notes.txt (12345 bytes)
```

The caller must then make a second `/mcp` call, `confirm_action`, with the
exact token:

| `approve` | What happens | Response |
|---|---|---|
| `true` | Action performs for real (30s timeout for commands; on timeout the process is killed, `returncode: -1`, timeout message in `stderr`). | The real result — same shape `execute_bash`/`write_file` returned synchronously before (`stdout`/`stderr`/`returncode`, or `status`/`path`/`bytes`). |
| `false` | Pending entry discarded. Nothing runs or writes. | `isError: true`, text `Declined.` |
| token unknown, expired (10 min TTL), or already used | Nothing runs or writes. The entry (if any) is purged. | `isError: true`, text `Unknown or expired confirmation token.` |

Details:

- Tokens are `secrets.token_urlsafe(16)` — unpredictable, issued one per
  action, valid for **10 minutes** (wall-clock expiry, correct across
  restarts). When persistence is opted in via
  `INVINCIBLE_PERSIST_PENDING_ACTIONS`, tokens are written to the
  PostgreSQL database (`INVINCIBLE_DB_URL`, `pending_actions` table) so
  they survive restarts (`PendingActionStore` on `app.state`); otherwise
  the store is memory-only and restarts orphan staged actions.
- A token is **single-use**: the first `confirm_action` that resolves it
  pops the entry, so replaying a token can never execute the action twice.
- Only a real JSON boolean `true` approves — a string `"true"` or a number
  is treated as deny.
- The execution timeout applies only during execution, i.e. only after
  approval — staging someone else's command never blocks the server.
- Denylist hits short-circuit **before** any token is issued (verified by
  tests).

---

## 4. The OAuth authorization server

Invincible ships a small OAuth 2.1 + PKCE authorization server (RFC 7591
dynamic client registration, RFC 8414 and RFC 9728 metadata) so
MCP-compatible clients can connect the way the ecosystem expects — no
external identity provider, no hosted relay, everything in the host's
own process.

- **Consent requires a logged-in account** (Phase 2, self-service). The
  approver is the dashboard session (`invincible_session` cookie:
  HttpOnly, SameSite=Lax, HMAC-signed, `Secure` when served over HTTPS);
  the owner-secret login form is gone, and `INVINCIBLE_OWNER_SECRET`
  survives only as the session-signing key source. Tokens minted from
  the approval act as that user's subject.
- **Client registration** (`POST /oauth/register`) is open by design —
  dynamic registration is supposed to be, and it is per-IP rate-capped
  (MEDIUM-4) so one address cannot bloat `oauth_clients`. The actual
  gate is the consent page: only a registered `client_id`/`redirect_uri`
  pair is ever redirected
  to; anything else gets an error page, never a redirect. Redirect URIs must
  be `https://` or loopback `http://localhost`/`http://127.0.0.1` (OAuth 2.1
  communication-security rule, enforced at registration).
- **Authorization codes** are single-use, bound to the exact client /
  redirect URI / PKCE challenge, and expire after ~5 minutes.
- **Access tokens** live ~1 hour, are valid only for `/mcp`, and are stored
  **hashed (SHA-256)** in PostgreSQL. **Refresh tokens** live ~30 days
  and are **rotated on every use** — a leaked old refresh token stops
  working the moment the new pair is issued (required for public clients).
- **Revocation** (`POST /oauth/revoke`, plus `invincible oauth revoke
  <client_id>`) invalidates tokens server-side immediately.

---

## 5. The chat endpoint's security posture

- **Auth**: per-user `inv_` API keys only — fail closed (see
  [§1](#1-three-independent-auth-realms)). No shared gateway key, no
  anonymous path.
- **Sessions**: the client session string (`X-Session-Id`) is a
  **partition key, not a credential**. Every store read and
  write is predicated on the caller's ownership triple: two users
  using the same string get fully independent sessions, task chains,
  checkpoints, and runs, and a foreign string reads exactly like a
  nonexistent one (anti-enumeration). History is stored as **plaintext
  JSON in PostgreSQL** (`INVINCIBLE_DB_URL`) — the database credentials are
  the security boundary, and `invincible doctor` always prints the DSN
  password-masked so it never leaks into terminal output or CI logs.
- **Scoped memories are user-partitioned (Phase 4).** Memory rows carry a
  real `user_id` FK; retrieval predicates on it server-side, plus an
  owner-scoped project filter — a query can only ever surface rows the
  principal already owns, so memory cannot leak across users even when two
  clients send identical text. Explicit "remember this" saves land in the
  saver's own user scope; provenance records the originating session.
  Injection is budget-capped and rendered as system messages that are
  never persisted into history.
- **Upstream keys**: each user's provider API keys are Fernet-encrypted
  at rest under `INVINCIBLE_CREDENTIAL_KEY` (§9); `providers.yaml` is a
  static test fixture and carries no live secrets.
- **Failure data**: a provider's `401/403` response body is never forwarded
  to the client (the credential is skipped and marked disabled in-memory
  for the process lifetime, and the next credential is tried); other
  upstream errors are forwarded verbatim.

---

## 6. Operational hardening (JSON-RPC layer)

- Malformed JSON body → `-32700 Parse error` (id `null`).
- Non-object body → `-32600 Invalid Request`.
- Non-dict `params` → `-32602 Invalid params`.
- Unknown method/tool → `-32601`.
- Requests **without an `id`** are JSON-RPC *notifications*: the side effect
  (if any) still runs, but the server replies `204 No Content` with no body —
  even on error. See [docs/MCP_PROTOCOL.md](MCP_PROTOCOL.md).

---

## 7. Known limits

These are design decisions, documented so nobody mistakes the controls for a
sandbox:

1. **The denylist is a text match, not a shell parser.** `powershell -Command
   "..."`, `cmd /c "..."`, encoding tricks, or any wrapper can smuggle an
   arbitrary command past every pattern. The denylist exists to catch the
   obvious, high-blast-radius cases without a token — **the approval step
   is the genuine safety boundary. Whatever approves a token decides what
   runs.**
2. **Approval is remote, and "the approver" is the subject behind a live
   access token.** There is no separate human-approval surface. Since
   Phase 2 a staged action can only be confirmed by its own staging
   subject — another user's confirm attempt reads as an unknown token and
   leaves the action intact — and every resolution writes an audit row.
   Pending actions are persisted to the PostgreSQL database
   (`INVINCIBLE_DB_URL`) **only when `INVINCIBLE_PERSIST_PENDING_ACTIONS`
   is set** — the default is memory-only, so a restart orphans them.
   Revocation is the control: `invincible oauth revoke`.
3. **The bearer token is the secret in flight.** Leaking an access token
   gives `/mcp` access until it expires (~1h) or is revoked via
   `invincible oauth revoke <client_id>`. A leaked **refresh** token is
   useful only until the next rotation or revocation. Treat the output of
   client tooling that echoes tokens as sensitive. (Contrast with the old
   model: the shared secret never expired at all.)
4. **Login rate limiting is per-IP with a fixed window.** The
   `/auth/login` form (and device-pairing code entry) counts attempts in a
   persisted per-IP store; after `5` wrong guesses
   inside `15 minutes`, further attempts from that IP are rejected until
   the window ages out. The counters are persisted (`login_attempts`), so
   restarts no longer clear them; they can
   still be bypassed by rotating IPs. Keep the service on
   localhost/tunnel HTTPS.
5. **Session-signing secret exposure.** `INVINCIBLE_OWNER_SECRET` now has
   exactly one job: the HMAC key source for account browser sessions
   (`SessionManager`). If it is ever exposed, rotate it immediately
   with `invincible secret rotate` — it regenerates the value inside `.env`
   in place (never echoed) so no manual editing is needed. Rotation
   invalidates **every** browser session at once (including OAuth-consent
   sessions), which is exactly what you want after a leak. Note what
   rotation does **not** do: it does not invalidate OAuth grants/tokens
   already issued to approved clients (they keep working until they expire
   or are revoked). Cutting a
   client off is `invincible oauth revoke <client_id>`, a separate lever.
6. **Dynamic registration is open to the port.** Anyone who can reach
   `/oauth/register` can create a client (per-IP rate cap aside), but the
   consent page still gates every grant — only a logged-in account may
   approve, and an unregistered or mismatched redirect is never followed.
   The exposure is spam/annoyance, not access.
7. **401/403 disables a credential for the process lifetime.** When an
   upstream provider answers 401/403, that credential is skipped and
   marked disabled in-memory (keyed `byok:<credential-id>`); it stays
   disabled until the process restarts. Re-connecting the credential on
   the dashboard (delete + re-add) is the user-side fix. Cooldowns from
   429/5xx follow the exponential curve instead and self-heal.
8. **Sessions and grants persist plaintext (except tokens, which are
   hashed).** The PostgreSQL database holds full conversation history
   unencrypted and the OAuth client/code/refresh rows — protect it with
   credentials and network position, since the security boundary moved
   from a filename to the DSN (masked in `doctor` output). The `.env`
   denylist entry still stops exfiltration of secrets; the `sessions.db`
   entries remain purely as leftover-file guards.
9. **Chat-key threat scope.** An `inv_` key protects that user's provider
   credits and data — not tool execution (`/mcp` needs an OAuth token).
   Keys are random `token_urlsafe` values, SHA-256-hashed at rest, and
   revocable from the dashboard or `invincible api-key revoke`.
10. **Continuity payloads render into prompts.** Content written through
   the MCP continuity tools is stored verbatim and injected as a system
   message for later requests. It carries exactly the trust level of the
   scoped-memory injection: whoever holds an MCP token can shape future
   prompts in their own session. Payloads are size-capped and never
   treated as instructions by Invincible itself. Chat auto-extraction is
   narrower still: only user-role messages are mined, so assistant replies
   and tool results (a fetched page, a file read) can never mint durable
   memories that later prompts re-inject.
11. **Graph API shows raw snippets.** `/api/v1/sessions/{id}/graph`
    includes first-message JSON snippets per turn — owner-scoped to the
    `inv_` key's user, same exposure class as reading the session via
    other management endpoints.
12. **Account sessions inherit the owner-secret key.** The Phase 3 cookie
    realm is signed with a key derived from `INVINCIBLE_OWNER_SECRET`, so
    rotating that secret (deliberately) logs every browser out — including
    account sessions, not just OAuth-consent sessions. GitHub login is off
    until `INVINCIBLE_GITHUB_CLIENT_ID`/`_SECRET` are set; the redirect URI
    to register on the GitHub app is `<public base URL>/auth/github/callback`.
13. **GitHub auto-link trusts GitHub's verified-email assertion.** Linking
    an incoming identity to an existing local account requires GitHub to
    report that email as verified AND primary. A second GitHub identity
    reusing the same verified email is rejected (`identity_conflict`) rather
    than attached. Password-less accounts created through GitHub can adopt
    a first password from Dashboard settings (Phase 5); a forgotten
    password is reset by the server host:
    `invincible users reset-password <email>` (audited as
    `auth.password_reset`). There is deliberately no email-based
    self-service reset - a self-hosted gateway has no mail
    infrastructure, and database access is the host's proof of
    authority. The reset bumps `session_version`, so every existing
    browser cookie dies with the old password; `inv_` keys and MCP
    tokens are untouched (separate realms).
14. **Password change invalidates browser sessions — and only browser
    sessions.** `/auth/password` bumps `users.session_version` in the
    same UPDATE as the argon2id hash, `v2` session cookies embed the
    version they were minted against, and resolution rejects
    signature-valid-but-version-mismatched cookies exactly like forged
    ones (`resolve_session` — the shared resolver every session consumer,
    including the OAuth consent flow, goes through). The acting
    browser is re-issued a live cookie on success; every other device
    must log back in. Deliberately NOT affected by a password change:
    `inv_*` API keys (minted as random tokens, SHA-256-hashed at rest,
    never derived from the password — that independence is the point of
    a separate realm), OAuth/MCP bearer tokens, and device-pairing keys.
    Those follow their own levers: `invincible api-key revoke`,
    `invincible oauth revoke`, and owner-secret rotation (which still
    invalidates ALL browser sessions at once). Deploy note: pre-0006
    `v1` cookies are rejected outright, so browsers logged in before
    this change re-login exactly once at rollout.

15. **BYOK credential encryption is env-key, not HSM; rotation is not
    built yet.** User-connected API keys are Fernet-encrypted under
    `INVINCIBLE_CREDENTIAL_KEY` (§9). A compromise of the database
    ALONE yields only ciphertext; a compromise of BOTH the database and
    the environment (the `.env` file or process env) decrypts
    everything, same honest framing as every other env-held secret
    above. There is NO re-encryption/rotation flow in Phase 9: rotating
    the master key strands previously stored credentials (they fail
    decryption with a caught, user-visible "re-connect the provider"
    error — never plaintext, never a crash). Recovery is manual:
    re-connect each provider under the new key. See also the dashboard
    Providers page and limit 13's host-side password reset.

16. **The agent trusts the machine it runs on (Phase 10).** With agent
    routing on, confirmed tool execution happens on the user's own PC
    via `invincible harness connect`, authenticating with the `inv_` key from
    device pairing (`invincible login`, or first-run
    self-pairing — same device flow, same minted key). Malware on that
    PC can impersonate the agent —
    the same trust level as any local dev tool. Mitigations: the key is
    per-user, stored `0600` in `~/.invincible/`, revocable with
    `invincible api-key revoke`, and every dispatch/resolution is
    audited server-side. The server never trusts agent-supplied *input*
    — jobs flow out, results flow in, and a forged result can only
    resolve a job the forger's own key was dispatched (submit_result
    refuses cross-user job ids indistinguishably from unknown ones).

17. **The agent registry is in-memory and single-instance (Phase 10).**
    Same trade-off class as the default `PendingActionStore`: a restart
    orphans in-flight jobs (the holding `/mcp` request gets a
    connection reset; clients retry end-to-end) and every agent
    re-registers on its next poll. Long-poll transport (plain HTTPS,
    httpx, no WebSocket) survives flaky WiFi by design — the loop IS
    the retry. Deploy note: the worst-case held request is the action's
    own timeout plus 10s grace (~40s for the default 30s command) —
    verify the platform's proxy timeout after first deploy.

---

## 8. Production database permission model

Required for any deployment beyond an isolated dev loopback. The shipped
compose pair enforces all four points; hosted-mode acceptance is
[Phase 7](ROADMAP.md), with the operational steps and checklist in
[DEPLOYMENT.md](DEPLOYMENT.md) §4 and §7:

1. **Least-privilege roles — never a superuser app connection.** Two
   non-superuser roles: a schema owner for migrations
   (`invincible_migrate`, the only role that may run
   `invincible db upgrade`) and a runtime role (`invincible_app`) holding
   SELECT/INSERT/UPDATE/DELETE plus sequence USAGE and nothing else —
   DDL is denied. Reference grants ship in
   `docker/db-init/01-roles.sh`; the bootstrap `postgres` superuser is
   used once at init and never again.
2. **Password auth enforced.** `scram-sha-256` on every TCP connection.
   `trust` is acceptable only on an isolated dev loopback: under `trust`
   a wrong password AND an empty password both connect, which makes the
   DSN password decorative — it was verified empirically on a dev
   cluster and must never reach a shared host.
3. **Durable storage.** A managed/persistent database with a backup
   story — not a temp-directory cluster that vanishes on reboot or
   cleanup.
4. **Fresh per-environment secrets.** The compose passwords
   (`*-dev-change-me`) are localhost conveniences; every target
   environment generates its own credentials and `INVINCIBLE_*` secrets.

`invincible dev-db` intentionally relaxes 1–2 for loopback dev
ergonomics; it is a development provisioner, not a production path.
---

## 9. BYOK credential encryption model (Platform Phase 9)

What user-connected provider API keys are, and are not, protected from.

**Primitive.** `cryptography.fernet.Fernet` — AES-128-CBC with an
HMAC-SHA256 authenticator (encrypt-then-MAC), chosen over raw AES-GCM
for fewer sharp edges (nonce/AD handling is internal and cannot be
misused by call sites). One master key (`INVINCIBLE_CREDENTIAL_KEY`, a
Fernet key; `invincible secret credential-key` generates it into
`.env`) encrypts every stored credential. Ciphertext lives in the
`user_provider_credentials.encrypted_api_key` BYTEA column; plaintext
is never stored, logged, audited, or returned after the create request.

**What is protected.**

- Database-only compromise (SQL injection, stolen backup, leaked DSN)
  yields Fernet ciphertext that is inert without the master key — an
  attacker must ALSO exfiltrate the environment to decrypt.
- Tampering is authenticated: any ciphertext modified or encrypted
  under a different key fails HMAC verification and surfaces as a
  caught `CredentialDecryptError` (user-visible "re-connect" guidance),
  never as decrypted garbage.
- Missing/malformed master key disables the entire BYOK surface (503,
  fail closed) — credentials are never written or read as plaintext.

**What is NOT protected (stated plainly).**

- Compromise of DB **and** environment decrypts everything: the master
  key sits in the same `.env`/process env as every other Invincible
  secret. This is the same trust ceiling as `INVINCIBLE_OWNER_SECRET`.
- The master key has no hardware/KMS backing; it is a static Fernet
  key in env.
- **No rotation flow exists in Phase 9** (limit 15): key rotation
  strands stored credentials until each is re-connected by its owner.
- The plaintext key necessarily transits request memory (create body,
  decrypt-at-attempt path, the probe's `Authorization` header). It is
  never included in logs — the router's attempt logging carries sizes
  and outcomes only — but a memory dump of the process sees keys, as
  it would for any env-held secret in the server process.

**The SSRF guard (`core/url_safety.py`).** User-typed `base_url`s are
the one new server-side fetch surface in Phase 9, so every non-catalog
URL must be `https://`, must not embed credentials, and must resolve
(literal or via every DNS answer) to a public address — RFC1918,
loopback, CGNAT, link-local (incl. the 169.254.169.254 cloud-metadata
address), unspecified, multicast, IPv6 ULA/link-local, and
IPv4-mapped forms are all rejected; `localhost` and dotless names are
rejected outright. The check runs at create AND before every later
test/chat use, so a DNS rebind after "add" cannot bypass it. Catalog
base URLs are packaged constants and skip the check only while
unedited. Known residual: the probe/streaming client follows only the
validated URL's host; it does not pin the resolved IP for the
connection itself, so a same-request rebind (TOCTOU between check and
connect) remains theoretically possible — the standard mitigation is
egress filtering at the network layer for hosted deployments.

---

## 10. Agent routing: three walls, not one (Phase 10)

`INVINCIBLE_AGENT_ROUTING=1` moves *execution* to the user's paired
agent while every *decision* stays on the server:

- **Wall 1 — server-side checks (unchanged).** Denylist, staging +
  confirm tokens, audit, per-user binding. A malicious agent cannot
  skip it: the server refuses to stage anything denylisted no matter
  what the agent says.
- **Wall 2 — the agent re-runs the same denylist locally**
  (`tool_executor.check_denylist` for commands; the agent sandbox for
  paths) before executing anything. Defense in depth: a command
  crafted to hit something bad on a user's PC that the server's
  patterns missed is caught locally and reported back as a `blocked`
  result — never silently dropped.
- **Wall 3 — PC-side scoping** (`invincible/agent/sandbox.py`). Reads
  and writes both stay under the user's home
  (`INVINCIBLE_AGENT_ROOT` to override) with `.env*`, `.git`, `.ssh`,
  `id_rsa*`, `id_ed25519*`, `*.pem`, `*credentials*` blocked by name
  on **every path component, both verbs**. This wall is new code on
  purpose: the server's write denylist is repo-root-relative and
  matches nothing outside the server repo — reusing it on a user's
  machine would protect nothing. The agent process runs as the
  logged-in user with exactly their privileges, never elevated.

Isolation is structural, not a policy check: registry queues and job
futures are keyed by `user_id`, an `inv_` key resolves to exactly one
user, and `POST /agent/result` refuses results for another user's
jobs. User 1's confirmed commands cannot reach user 2's PC because no
code path tries.

**The consent-routing coupling (Phase 2 resolved it, keep the reasoning):**
before Phase 2, self-service consent was allowed *only* when routing was
on, because with routing off a confirmed tool call executed on the server
host — a self-registered session minting host-shell MCP tokens was
indefensible. Phase 2 removed that gate by making every consent
self-approval, which is safe **precisely because** the deployment posture
below makes `INVINCIBLE_AGENT_ROUTING=1` mandatory on public multi-user
deployments: approving a client exposes only the approver's own machine.
The old relaxation switch (`oauth.py`'s `_non_operator_response` and its
`test_oauth_consent_relaxation.py` pin) is gone with the operator gate
itself.

**Deployment posture (2026-09-07 audit, Step 3):** on any PUBLIC
multi-user deployment, `INVINCIBLE_AGENT_ROUTING=1` is a hard
requirement, not an optimization. With routing unset, confirmed MCP
tool calls execute on the *server host* under the server's privileges —
correct for a one-person `invincible start` self-host, indefensible
when strangers can register. The current production deploy
(invincible-ai.me, Railway) keeps the flag set in its environment
variables; losing it would silently revert every confirmed tool call
to server-host execution. The flag stays opt-in rather than
defaulting on at multi-user detection (considered and rejected in the
audit): settings are live env reads with no startup user-count gate, and
local dev workflows depend on the off default. Verify the flag after any
platform or account ownership transfer via the deploy checklist:
`invincible doctor` output plus the dashboard's MCP clients page showing
per-user agents online.

---
