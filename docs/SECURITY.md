# Security Model

Invincible exposes two attack-relevant surfaces: a chat proxy that calls
upstream AI providers, and an MCP tool server that can **run shell commands
and write files on the host machine**. This document describes exactly what
guards what, where the boundaries are, and — explicitly — where they are not.

---

## 1. Two independent auth realms

The two surfaces authenticate **independently**. Rotating one never affects
the other, and a leaked tunnel URL alone is not enough to reach tool
execution.

> **Renamed in this release:** the old per-request MCP secret
> `MCP_SHARED_SECRET` is now the owner-login secret `INVINCIBLE_OWNER_SECRET`
> and is no longer sent on `/mcp` at all. See
> [§1.2](#12-mcp--oauth-21--pkce-bearer-tokens). Your old `.env` value still
> works (the legacy key is read as a fallback), but its role has changed.

### `/v1/*` — dual-realm auth (Platform Phase 1)

Requests resolve a Principal in this fixed order (implemented in
`invincible/endpoints/auth.py::require_auth`):

| Step | Realm | Result |
|---|---|---|
| 1 | `Authorization: Bearer <GATEWAY_API_KEY>` or `x-api-key`, timing-safe compared (`hmac.compare_digest`) | System *local* owner (`kind="legacy"`) |
| 2 | `Bearer inv_…` matching an **API key** (SHA-256 hash lookup; revoked keys excluded) | That key's user + its default project (`kind="api_key"`) |
| 3 | Gateway key **unset** | Documented fail-open local identity (`kind="anonymous"`) |
| 4 | anything else | HTTP 401, body `{"detail": {"error": {"message": "...", "type": "auth_error"}}}` |

API-key properties:

- Raw values are shown **once**, at creation (`invincible api-key create`);
  storage keeps only a SHA-256 hash plus a visible prefix for listings.
- Resolution is unambiguous: the legacy realm is checked first, so even a
  deliberately crafted hash collision between the two realms resolves as
  legacy (pinned by test).
- Sessions created under an API-key principal are stored under that user's
  ownership triple (`user_id`, `project_id`, `client_session_id`) — the
  same client session string under two principals yields two distinct
  session rows. Enforcement of isolation on every read path lands in
  Phase 2.

Note the open-if-unset behavior (step 3): on a dev box without a gateway
key, anyone who can reach the port can use your provider credits. Set the
key. Hosted mode retires fail-open entirely (Phase 8).

### `/mcp` — OAuth 2.1 + PKCE Bearer tokens

`/mcp` no longer takes a shared secret header. It accepts **short-lived
access tokens** (`Authorization: Bearer <token>`, ~1h TTL) issued by
Invincible's own, built-in authorization server (`/oauth/*`) after a
browser-based owner-login and per-client consent.

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
| Management alt-realm | A user's own `inv_` API key also works on `/api-keys`; MCP bearer tokens and `GATEWAY_API_KEY` are rejected by construction (`ApiKeyStore.resolve` matches only `inv_` hashes) |

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
  `password.set` / `password.changed` audit rows. Changing a password
  does NOT revoke existing signed cookies — see limit 14 below.

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
1. owner-login (once, browser)    2. consent (per client)     3. bearer token (per call)
INVINCIBLE_OWNER_SECRET on  →      Approve on consent page  →  access token sent on every
/oauth/authorize + signed,         issues a single-use        /mcp call; ~1h TTL,
30-day session cookie              authorization code          revocable, hash-stored
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
denylist is the only gate after auth.

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
only exists after the owner logs in and approves the client on the consent
page, this is a **named trust boundary** in a way the shared secret never
was: a grant is scoped to one registered client, is revocable, and expires
on its own. This mirrors the earlier `confirm_action` trust-boundary change
and is documented here explicitly.

Implications, stated plainly:

- Anyone holding a live access token can approve a staged action — there is
  no separation between "AI client" and "operator" at the protocol level.
  The operator's lever is **revocation**: `invincible oauth revoke
  <client_id>` kills every outstanding token for a client instantly.
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

Invincible ships a small, single-operator OAuth 2.1 + PKCE authorization
server (RFC 7591 dynamic client registration, RFC 8414 and RFC 9728
metadata) so MCP-compatible clients can connect the way the ecosystem
expects — no external identity provider, no hosted relay, everything in the
operator's own process.

- **Owner login** (`INVINCIBLE_OWNER_SECRET`) is entered **once per
  browser** on `/oauth/authorize`, then exchanges a **signed, HMAC-protected
  session cookie** (HttpOnly, SameSite=Lax, 30-day "remember this browser"
  TTL, `Secure` when served over HTTPS). The only place the owner secret is
  ever transmitted is that login form.
- **Client registration** (`POST /oauth/register`) is open by design —
  dynamic registration is supposed to be. The actual gate is the consent
  page: only a registered `client_id`/`redirect_uri` pair is ever redirected
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

- **Auth**: dual-realm (legacy gateway key vs per-user API keys — see
  [§1.1](#v1--dual-realm-auth-platform-phase-1)). Unset gateway key with no
  matching API key = fail-open local identity.
- **Sessions**: the client session string (`X-Session-Id`) is a
  **partition key, not a credential**. Since Phase 2 every store read and
  write is predicated on the caller's ownership triple: two principals
  using the same string get fully independent sessions, task chains,
  checkpoints, and runs, and a foreign string reads exactly like a
  nonexistent one (anti-enumeration). The one exception is the operator:
  `INVINCIBLE_ADMIN_KEY` may resolve any session on the graph surface —
  documented out-of-band operator trust. History is stored as **plaintext
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
- **Upstream keys**: API keys are read from the environment by *name*
  (`api_key_env`), never stored in `providers.yaml`.
- **Failure data**: a provider's `401/403` response body is never forwarded
  to the client (the provider is silently disabled instead); other upstream
  errors are forwarded verbatim.

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
4. **Owner-login rate limiting is per-IP with a fixed window.** The
   `/oauth/authorize` login form compares the owner secret with a
   timing-safe digest and, after `LOGIN_MAX_ATTEMPTS` (5) wrong guesses
   inside `LOGIN_WINDOW_SECONDS` (15 minutes), rejects further attempts
   from that IP until the window ages out. Since Phase 2 the counter is
   persisted (`login_attempts`), so restarts no longer clear it; it can
   still be bypassed by rotating IPs and does not protect the consent page
   from other abuse. Use a high-entropy secret (`invincible setup`
   generates one) and keep the service on localhost/tunnel HTTPS.
5. **Owner-secret exposure.** If `INVINCIBLE_OWNER_SECRET` is ever
   accidentally pasted somewhere or otherwise exposed, rotate it immediately
   with `invincible secret rotate` — it regenerates the value inside `.env`
   in place (never echoed) so no manual editing is needed. Note what
   rotation does **not** do: it does not invalidate OAuth grants/tokens
   already issued to approved clients (they keep working until they expire
   or are revoked — rotation only affects future browser logins). Cutting a
   client off is `invincible oauth revoke <client_id>`, a separate lever.
6. **Dynamic registration is open to the port.** Anyone who can reach
   `/oauth/register` can create a client, but the consent page still gates
   every grant — an unregistered or mismatched redirect is never followed.
   The exposure is spam/annoyance, not access.
7. **`/v1/*` fails open to the local identity if `GATEWAY_API_KEY` is
   unset.** Forgetting the key opens your provider credits to anyone who
   can reach the port (API keys, when minted, still authenticate as their
   own users — but the anonymous fallback also remains open).
8. **Sessions and grants persist plaintext (except tokens, which are
   hashed).** The PostgreSQL database holds full conversation history
   unencrypted and the OAuth client/code/refresh rows — protect it with
   credentials and network position, since the security boundary moved
   from a filename to the DSN (masked in `doctor` output). The `.env`
   denylist entry still stops exfiltration of secrets; the `sessions.db`
   entries remain purely as leftover-file guards.
9. **Chat-key threat scope.** `GATEWAY_API_KEY` (compared timing-safely
   since Phase 12) protects provider credits, not tool execution — but
   prefer long random tokens anyway (the CLI generates
   `token_urlsafe(32)`).
10. **Continuity payloads render into prompts.** Content written through
   the MCP continuity tools is stored verbatim and injected as a system
   message for later requests. It carries exactly the trust level of the
   scoped-memory injection: whoever holds an MCP token can shape future
   prompts in their own session. Payloads are size-capped and never
   treated as instructions by Invincible itself.
11. **Graph API shows raw snippets.** `/api/v1/sessions/{id}/graph`
    includes first-message JSON snippets per turn — admin-realm only
    (`INVINCIBLE_ADMIN_KEY`), same exposure class as reading the session
    via other management endpoints.
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
    a first password from Dashboard settings (Phase 5); a forgotten-password
    RESET flow still does not exist.
14. **Password change is not a logout-everywhere control.** `/auth/password`
    swaps the argon2id hash, but signed browser cookies stay valid until
    natural expiry or `INVINCIBLE_OWNER_SECRET` rotation — sessions are
    stateless HMAC values, so there is no server-side session table to
    sweep. A hijacked session therefore survives its victim changing their
    password; rotate the owner secret (which invalidates every browser
    session at once) and treat cookie theft as key theft.

---

## 8. Production database permission model

Required for any deployment beyond an isolated dev loopback. The shipped
compose pair enforces all four points; hosted-mode acceptance is
[Phase 7](ROADMAP.md):

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