# Security Model

Invincible exposes two attack-relevant surfaces: a chat proxy that calls
upstream AI providers, and an MCP tool server that can **run shell commands
and write files on the host machine**. This document describes exactly what
guards what, where the boundaries are, and — explicitly — where they are not.

---

## 1. Two independent auth realms

The two endpoints authenticate **independently**, with separate secrets.
Rotating one never affects the other, and a leaked tunnel URL alone is not
enough to reach tool execution.

### `/v1/*` — `GATEWAY_API_KEY`

| Aspect | Value |
|---|---|
| Header | `Authorization: Bearer <key>` |
| Comparison | plain `==` string comparison (not timing-safe) |
| If unset | **Endpoint is open** — no auth enforced at all |
| Failure | HTTP 401, body `{"error": {"message": "...", "type": "auth_error"}}` |

Implemented in `invincible/main.py::require_auth`. Note the open-if-unset
behavior: on a dev box without a key, anyone who can reach the port can use
your provider credits. Set the key.

### `/mcp` — `MCP_SHARED_SECRET`

| Aspect | Value |
|---|---|
| Header | `X-MCP-Secret: <secret>` |
| Comparison | `secrets.compare_digest` (timing-safe — no byte-by-byte guessing) |
| If unset | **HTTP 503 — endpoint disabled** (never open) |
| Failure | HTTP 401, body `{"error": {"message": "...", "type": "auth_error"}}` |

Implemented in `invincible/endpoints/mcp.py::require_mcp_auth`.

### The layering principle

`tool_executor.py` (the code that actually runs commands and writes files)
**assumes the caller is already authenticated**. It decides only whether a
specific action is safe and approved — never who is allowed to ask. Auth is
entirely the endpoint dependency's job, one layer up.

---

## 2. The MCP tool security stack

For `execute_bash` and `write_file`, three gates run in order:

```
authenticated caller (MCP_SHARED_SECRET)
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
denylist is the only gate.

### 2.0 Approval is remote and token-based — a deliberate trust-boundary change

The old flow blocked on a synchronous y/N prompt at the server's own
terminal, so only someone with **physical access to the machine** could
approve an action. That has been **replaced, not supplemented**: an
`execute_bash`/`write_file` call that survives the denylist is staged in an
in-process `PendingActionStore` and returns immediately with a
`pending_confirmation` response carrying an unpredictable token
(`secrets.token_urlsafe(16)`). Nothing runs until a second `tools/call`
for `confirm_action` arrives with that token:

- `approve: true` → the staged action performs for real (command runs /
  file is written) and the real result is returned.
- `approve: false` → the pending entry is discarded, `Declined.` is
  returned, nothing executes.
- Unknown, expired (10-minute TTL), or already-used token → `Unknown or
  expired confirmation token.`, nothing executes.

**The trust boundary is intentionally different now.** Approval is
whatever the calling AI/client reports back through a second `/mcp` call —
the boundary is now **"whoever holds `MCP_SHARED_SECRET`"**, the same
boundary as every other request on `/mcp`. If you hold the secret, you can
approve (or deny) any pending action remotely; the operator no longer
needs to be anywhere near the machine. This is a real security property
change and is documented here explicitly — do not silently assume the
terminal prompt still protects anything.

Implications, stated plainly:

- Anyone who can authenticate to `/mcp` can approve a staged action — no
  separation between "AI client" and "operator" exists at the protocol
  level.
- A `confirm_action` sent as a **notification** (no `id`) also executes —
  JSON-RPC notifications still run their side effects, so don't rely on
  notifications being "just pings".
- Pending entries live **in memory only**: a server restart forgets every
  staged action (tokens become invalid — confirmations fail with
  *Unknown or expired*), and there is no audit log of who approved what.
- The server still prints an informational visibility line for each
  pending action to its own stdout (`[MCP] Pending <token>: …`) so someone
  watching the local terminal can see what is staged — **informational
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
| `sessions.db` | The session database |
| `invincible/` (any file under it) | Invincible's own source code |
| `tests/` (any file under it) | The test suite |
| `.git/` (any file under it) | Git internals |

### 2.3 `read_file` denylist — full inventory

Narrower than the write list **on purpose**: allowing a cloud AI to *see* the
source code is the entire point of the tool, and `providers.yaml` only holds
`api_key_env` **names**, not actual key values, so it is not a secret. Only
things that would leak an actual credential or sensitive local state over the
tunnel are blocked:

| Pattern (relative, case-insensitive) | Reason |
|---|---|
| `.env` / `.env.*` | Invincible's own secrets file |
| `sessions.db` | The session database (contains plaintext conversation history) |
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
  action, valid for **10 minutes**, stored **in memory only**
  (`PendingActionStore` on `app.state`, same lifetime as the process).
- A token is **single-use**: the first `confirm_action` that resolves it
  pops the entry, so replaying a token can never execute the action twice.
- Only a real JSON boolean `true` approves — a string `"true"` or a number
  is treated as deny.
- The execution timeout applies only during execution, i.e. only after
  approval — staging someone else's command never blocks the server.
- Denylist hits short-circuit **before** any token is issued (verified by
  tests).

---

## 4. The chat endpoint's security posture

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

## 5. Operational hardening (JSON-RPC layer)

- Malformed JSON body → `-32700 Parse error` (id `null`).
- Non-object body → `-32600 Invalid Request`.
- Non-dict `params` → `-32602 Invalid params`.
- Unknown method/tool → `-32601`.
- Requests **without an `id`** are JSON-RPC *notifications*: the side effect
  (if any) still runs, but the server replies `204 No Content` with no body —
  even on error. See [docs/MCP_PROTOCOL.md](MCP_PROTOCOL.md).

---

## 6. Known limits

These are design decisions, documented so nobody mistakes the denylist for a
sandbox:

1. **The denylist is a text match, not a shell parser.** `powershell -Command
   "..."`, `cmd /c "..."`, encoding tricks, or any wrapper can smuggle an
   arbitrary command past every pattern. The denylist exists to catch the
   obvious, high-blast-radius cases without a token — **the approval step
   is the genuine safety boundary. Whatever approves a token decides what
   runs.**
2. **Approval is remote, and "the operator" is whoever holds the MCP
   secret.** The synchronous terminal prompt is gone. There is no separate
   human-approval surface, no per-approver identity, and no audit log —
   the pending store is in-memory (a restart invalidates every outstanding
   token), and there is no record of who approved what, only what the
   server's own stdout shows while an action sits pending.
3. **`/v1/*` is unauthenticated if `GATEWAY_API_KEY` is unset.** Forgetting
   the key opens your provider credits to anyone who can reach the port.
4. **Auth is a shared secret, not identity.** No per-user model; anyone with
   the MCP secret is the operator for every session.
5. **Sessions persist plaintext.** `sessions.db` contains full conversation
   history unencrypted; the `.env` and `sessions.db` denylist entries exist
   precisely so a remote AI cannot exfiltrate them.
6. **Provider disable is process-scoped.** A provider disabled by a 401/403
   stays disabled until the process restarts; cooldowns are in-memory only.
7. **`/v1` auth comparison is not timing-safe.** `GATEWAY_API_KEY` uses plain
   `==`; the MCP secret uses `secrets.compare_digest`. The chat key protects
   provider credits, not tool execution — but if you want defense in depth,
   prefer long random tokens (the CLI generates `token_urlsafe(32)`).
