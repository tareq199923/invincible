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

### `/v1/*` — `GATEWAY_API_KEY`

| Aspect | Value |
|---|---|
| Header | `Authorization: Bearer <key>` |
| Comparison | `hmac.compare_digest` — timing-safe (hardened in Phase 12) |
| If unset | **Endpoint is open** — no auth enforced at all |
| Failure | HTTP 401, body `{"error": {"message": "...", "type": "auth_error"}}` |

Implemented in `invincible/main.py::require_auth`. Note the open-if-unset
behavior: on a dev box without a key, anyone who can reach the port can use
your provider credits. Set the key.

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
  opted in**: `PendingActionStore` writes through to the same SQLite file
  as sessions (`INVINCIBLE_DB_PATH`, `sessions.db` in the working
  directory) only when the `INVINCIBLE_PERSIST_PENDING_ACTIONS`
  environment variable is set. By default it is **memory-only** — a
  restart orphans every staged action and confirmations fail with
  *Unknown or expired*, the original clean-slate design. Persistence
  means staged shell commands sit in plaintext on disk pre-approval, so
  it is deliberately off unless requested. There is no audit log of who
  approved what.
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
| `sessions.db` | The session database — which also holds the OAuth grant tables |
| `invincible/` (any file under it) | Invincible's own source code |
| `tests/` (any file under it) | The test suite |
| `.git/` (any file under it) | Git internals |

> The OAuth store deliberately lives in the **same SQLite file** as
> conversations (`sessions.db`) so the denylist entries above protect tokens
> and client registrations too. Tokens are stored **SHA-256 hashed** — a
> read of the file yields nothing usable anyway.

### 2.3 `read_file` denylist — full inventory

Narrower than the write list **on purpose**: allowing a cloud AI to *see* the
source code is the entire point of the tool, and `providers.yaml` only holds
`api_key_env` **names**, not actual key values, so it is not a secret. Only
things that would leak an actual credential or sensitive local state over the
tunnel are blocked:

| Pattern (relative, case-insensitive) | Reason |
|---|---|
| `.env` / `.env.*` | Invincible's own secrets file |
| `sessions.db` | The session database (plaintext history **and** the OAuth grant tables) |
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
  `INVINCIBLE_PERSIST_PENDING_ACTIONS`, tokens are written to the session
  SQLite file (`INVINCIBLE_DB_PATH`) so they survive restarts
  (`PendingActionStore` on `app.state`); otherwise the store is
  memory-only and restarts orphan staged actions.
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
  **hashed (SHA-256)** in `sessions.db`. **Refresh tokens** live ~30 days
  and are **rotated on every use** — a leaked old refresh token stops
  working the moment the new pair is issued (required for public clients).
- **Revocation** (`POST /oauth/revoke`, plus `invincible oauth revoke
  <client_id>`) invalidates tokens server-side immediately.

---

## 5. The chat endpoint's security posture

- **Auth**: `GATEWAY_API_KEY` (see above). Unset = open.
- **Sessions**: `session_id` (from `X-Session-Id`) is a **partition key, not
  a credential**. Anyone authenticated to the endpoint can read/write any
  session id. History is stored as **plaintext JSON in SQLite**
  (`sessions.db`, gitignored).
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
2. **Approval is remote, and "the operator" is whoever holds a live access
   token.** There is no separate human-approval surface, no per-approver
   identity, and no audit log. Pending actions are persisted to the session
   SQLite file (`INVINCIBLE_DB_PATH`) **only when
   `INVINCIBLE_PERSIST_PENDING_ACTIONS` is set** — the default is
   memory-only, so a restart orphans them — and there is no record of who
   approved what. Revocation is the control:
   `invincible oauth revoke`.
3. **The bearer token is the secret in flight.** Leaking an access token
   gives `/mcp` access until it expires (~1h) or is revoked via
   `invincible oauth revoke <client_id>`. A leaked **refresh** token is
   useful only until the next rotation or revocation. Treat the output of
   client tooling that echoes tokens as sensitive. (Contrast with the old
   model: the shared secret never expired at all.)
4. **Owner-login rate limiting is per-IP, in-memory, and restarts reset
   it.** The `/oauth/authorize` login form compares the owner secret with a
   timing-safe digest and, after `LOGIN_MAX_ATTEMPTS` (5) wrong guesses
   inside `LOGIN_WINDOW_SECONDS` (15 minutes), rejects further attempts
   from that IP until the oldest of those failures ages out of the window.
   That is brute-force friction, not an audit log: it is process-local (a
   restart clears it), it can be bypassed by rotating IPs, and it does not
   protect the consent page from other abuse. Use a high-entropy secret
   (`invincible setup` generates one) and keep the service on
   localhost/tunnel HTTPS.
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
7. **`/v1/*` is unauthenticated if `GATEWAY_API_KEY` is unset.** Forgetting
   the key opens your provider credits to anyone who can reach the port.
8. **Sessions and grants persist plaintext (except tokens, which are
   hashed).** `sessions.db` contains full conversation history unencrypted
   and the OAuth client/code/refresh rows; the `.env` and `sessions.db`
   denylist entries exist precisely so a remote AI cannot exfiltrate them.
9. **Chat-key threat scope.** `GATEWAY_API_KEY` (compared timing-safely
   since Phase 12) protects provider credits, not tool execution — but
   prefer long random tokens anyway (the CLI generates
   `token_urlsafe(32)`).
10. **Continuity payloads render into prompts.** Content written through
   the MCP continuity tools is stored verbatim and injected as a system
   message for later requests. It carries exactly the trust level of the
   facts-memory injection: whoever holds an MCP token can shape future
   prompts in their own session. Payloads are size-capped and never
   treated as instructions by Invincible itself.
11. **Graph API shows raw snippets.** `/api/v1/sessions/{id}/graph`
   includes first-message JSON snippets per turn — admin-realm only
   (`INVINCIBLE_ADMIN_KEY`), same exposure class as reading the session
   via other management endpoints.